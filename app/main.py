import asyncio
import os
from urllib.parse import urlsplit
import json
import hashlib
import base64
import io
import secrets
import tempfile
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional, Literal
from datetime import date
import sqlite3
import threading
import fitz
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .db import FILES, connect, init_db
from .mail import sync_account, refresh_email_html, reprocess_email, MailReprocessError
from .recognition import recognize
from .formats import validate
from .email_html import proxy_html_images
from .download import public_image
from .service import DUPLICATE_REASON, UNVERIFIED_REASON, assist, cipher, duplicate_invoice, duplicate_reason, export_filename, import_file, now
from .ai import save_settings as save_ai_settings, settings as get_ai_settings
from . import ai
from .backup import create_backup, inspect_backup, recover_interrupted_restore, restore_backup
from .storage_lock import storage_lock, storage_lock_sync
from .config import ensure_config, mail_sync_interval
from . import auth
from .deletion import delete_invoices, recover_deletions

with storage_lock_sync(exclusive=True):
    recover_interrupted_restore()
    init_db()
    recover_deletions()
    ensure_config()
    auth.ensure_auth()

async def scheduler():
    while True:
        await asyncio.sleep(mail_sync_interval())
        async with storage_lock():
            with connect() as db:
                ids = [r['id'] for r in db.execute('SELECT id FROM accounts WHERE enabled=1')]
            await asyncio.gather(*(asyncio.to_thread(sync_account, account_id) for account_id in ids), return_exceptions=True)

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

@app.middleware('http')
async def protection(request: Request, call_next):
    if request.method in ('POST','PUT','PATCH','DELETE') and request.headers.get('x-piaoxu-request') != '1':
        return JSONResponse({'detail':'请求缺少安全标记'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc.lower() != request.headers.get('host','').lower():
        return JSONResponse({'detail':'请求来源不匹配'},status_code=403)
    if request.url.path.startswith('/api/'):
        exclusive = request.url.path in ('/api/backup/export','/api/backup/restore','/api/invoices/delete')
        async with storage_lock(exclusive=exclusive):
            if request.url.path != '/api/auth/login' and not auth.current_user(request.cookies.get('piaoxu_session', '')):
                return JSONResponse({'detail':'请先登录'}, status_code=401)
            operation=asyncio.create_task(call_next(request))
            try:
                response=await asyncio.shield(operation)
            except asyncio.CancelledError:
                try: await operation
                except Exception: pass
                raise
    else:
        response = await call_next(request)
    if request.method in ('GET','HEAD') and not request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-cache'
    return response

class Account(BaseModel):
    name: str = Field(min_length=1,max_length=80)
    email: str = Field(pattern=r'^[^\s@]+@qq\.com$',max_length=254)
    auth_code: str = Field(min_length=1,max_length=200)

class AccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    auth_code: Optional[str] = Field(default=None, min_length=1, max_length=200)

class InvoiceUpdate(BaseModel):
    buyer: str = ''
    seller: str = ''
    amount_cents: Optional[int] = Field(default=None, ge=0)
    invoice_date: Optional[date] = None
    invoice_number: str = ''
    export_name: str = ''

class Ids(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=500)

class DeleteItem(BaseModel):
    id: int
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

class DeleteInvoices(BaseModel):
    items: list[DeleteItem] = Field(min_length=1, max_length=500)

class ExportIds(Ids):
    pdf_only: bool = False

class GroupCreate(Ids):
    name: str = Field(min_length=1,max_length=100)

class GroupStatus(BaseModel):
    reimbursed: bool

class AISettings(BaseModel):
    base_url: str = ''
    model: str = ''
    api_key: str = ''
    auto_enabled: bool = False
    clear_key: bool = False

class Template(BaseModel):
    fields: list[str] = Field(min_length=1,max_length=4)

class DuplicateDecision(BaseModel):
    decision: Literal['confirm','distinct','reopen']

class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)

class EmailImage(BaseModel):
    url: str = Field(min_length=1,max_length=4096)

class ChangeLogin(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(default='', max_length=128)

def set_session(response, request, token):
    response.set_cookie('piaoxu_session', token, max_age=auth.SESSION_SECONDS, httponly=True, secure=request.url.scheme == 'https', samesite='strict', path='/')

@app.post('/api/auth/login')
def login(data: Login, request: Request, response: Response):
    client = request.client.host if request.client else 'unknown'
    if not auth.allow_attempt(client):
        raise HTTPException(429, '尝试次数过多，请一分钟后再试')
    if not auth.verify(data.username, data.password):
        raise HTTPException(401, '账号或密码错误')
    auth.clear_attempts(client)
    set_session(response, request, auth.new_session())
    return {'username':data.username}

@app.get('/api/auth/me')
def login_status(request: Request):
    return {'username':auth.current_user(request.cookies.get('piaoxu_session', ''))}

@app.post('/api/auth/logout')
def logout(request: Request, response: Response):
    auth.revoke(request.cookies.get('piaoxu_session', ''))
    response.delete_cookie('piaoxu_session', path='/')
    return {'ok':True}

@app.put('/api/auth/account')
def change_login(data: ChangeLogin, request: Request, response: Response):
    username = auth.current_user(request.cookies.get('piaoxu_session', ''))
    if not username or not auth.verify(username, data.current_password):
        raise HTTPException(400, '当前密码不正确')
    new_username = data.username.strip()
    if not new_username:
        raise HTTPException(422, '账号不能为空')
    auth.change_user(new_username, data.password or data.current_password)
    set_session(response, request, auth.new_session())
    return {'username':new_username}

@app.get('/api/backup/export')
async def export_backup():
    output=tempfile.TemporaryFile()
    try:
        await asyncio.to_thread(create_backup,output)
    except ValueError as exc:
        output.close()
        raise HTTPException(422,str(exc)) from None
    except Exception:
        output.close()
        raise HTTPException(500,'备份失败，请检查存储空间及目录权限') from None
    def stream():
        try:
            while block:=output.read(1024*1024): yield block
        finally: output.close()
    return StreamingResponse(stream(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="piaoxu-backup.zip"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@app.post('/api/backup/inspect')
async def inspect_backup_file(file: UploadFile = File(...)):
    try:
        return await asyncio.to_thread(inspect_backup,file.file)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    except Exception:
        raise HTTPException(500,'无法检查备份文件') from None
    finally:
        await file.close()

@app.post('/api/backup/restore')
async def restore_backup_file(file: UploadFile = File(...)):
    try:
        result=await asyncio.to_thread(restore_backup,file.file)
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(ensure_config)
        await asyncio.to_thread(auth.ensure_auth)
        await asyncio.to_thread(auth.revoke_all)
        response=JSONResponse({'ok':True,'reauthenticate':True,**result})
        response.delete_cookie('piaoxu_session', path='/')
        return response
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    except Exception:
        raise HTTPException(500,'恢复失败，原有数据已保留或可从恢复前副本找回') from None
    finally:
        await file.close()

@app.get('/api/invoices')
def invoices(limit: Optional[int]=Query(None,ge=1,le=100), offset: int=Query(0,ge=0), query: str='', reimbursement: Literal['all','unreimbursed','reimbursed']='all', view: Literal['all','review']='all'):
    with connect() as db:
        base='''FROM invoices i LEFT JOIN group_invoices gi ON gi.invoice_id=i.id LEFT JOIN groups g ON g.id=gi.group_id LEFT JOIN emails e ON e.id=i.email_id LEFT JOIN accounts a ON a.id=e.account_id'''
        conditions=[];args=[]
        if view=='review': conditions.append("(i.recognition_status='识别失败' OR i.duplicate_state='confirmed' OR i.id IN (SELECT duplicate_of FROM invoices WHERE duplicate_of IS NOT NULL AND (recognition_status='识别失败' OR duplicate_state='confirmed')))")
        if limit is not None: conditions.append("i.recognition_status='已识别'")
        if reimbursement!='all': conditions.append('i.reimbursed=?');args.append(int(reimbursement=='reimbursed'))
        if query:
            escaped=query.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            conditions.append("LOWER(COALESCE(i.buyer,'')||' '||COALESCE(i.seller,'')||' '||COALESCE(i.original_name,'')||' '||COALESCE(i.invoice_number,'')) LIKE ? ESCAPE '\\'")
            args.append('%'+escaped+'%')
        where=(' WHERE '+' AND '.join(conditions)) if conditions else ''
        total=db.execute('SELECT count(*) '+base+where,args).fetchone()[0] if limit is not None else None
        sql='SELECT i.*,g.name group_name,a.name account_name '+base+where+' ORDER BY i.imported_at DESC,i.id DESC'
        rows = [dict(r) for r in db.execute(sql+(' LIMIT ? OFFSET ?' if limit is not None else ''),(*args,limit,offset) if limit is not None else args)]
        by_id = {row['id']:row for row in rows}
        for row in rows: row['mail_sources'] = []
        source_sql='''SELECT ie.invoice_id,e.id email_id,a.id account_id,a.name account_name,a.email account_email FROM invoice_emails ie JOIN emails e ON e.id=ie.email_id JOIN accounts a ON a.id=e.account_id'''
        if limit is not None and not by_id: return {'items':[],'total':total,'limit':limit,'offset':offset}
        if limit is not None: source_sql+=' WHERE ie.invoice_id IN ('+','.join('?'*len(by_id))+')'
        for source in db.execute(source_sql+' ORDER BY e.id',tuple(by_id) if limit is not None else ()):
            if source['invoice_id'] in by_id:
                by_id[source['invoice_id']]['mail_sources'].append({key:source[key] for key in ('email_id','account_id','account_name','account_email')})
        return {'items':rows,'total':total,'limit':limit,'offset':offset} if limit is not None else rows

@app.post('/api/invoices/delete')
def delete_selected_invoices(ids: DeleteInvoices):
    try:
        return delete_invoices(ids.items)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(500, '无法删除发票原件，请检查存储权限') from None

def inspect_invoice_file(name, content):
    ext = validate(name, content)
    digest = hashlib.sha256(content).hexdigest()
    with connect() as db:
        existing = db.execute('SELECT id,original_name,buyer,seller,amount_cents,invoice_date,invoice_number,recognition_status,failure_reason FROM invoices WHERE sha256=?',(digest,)).fetchone()
    if existing:
        return digest, dict(existing), existing['failure_reason'], dict(existing)
    with tempfile.TemporaryDirectory(prefix='piaoxu-inspect-') as directory:
        path = Path(directory) / ('invoice'+ext)
        path.write_bytes(content)
        fields, reason = recognize(path)
    from .service import cache_preview
    cache_preview(digest, fields, reason)
    return digest, fields, reason, None

def inspect_thumbnail(name, content):
    try:
        ext = Path(name).suffix.lower()
        if ext == '.pdf':
            with fitz.open(stream=content,filetype='pdf') as document:
                page = document[0]
                scale = min(1, 360/max(page.rect.width,page.rect.height))
                image = page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).tobytes('jpg',jpg_quality=70)
        elif ext in ('.jpg','.jpeg','.png'):
            from PIL import Image
            with Image.open(io.BytesIO(content)) as original:
                original.thumbnail((360,360))
                output = io.BytesIO()
                original.convert('RGB').save(output,format='JPEG',quality=70)
                image = output.getvalue()
        else:
            return ''
        return base64.b64encode(image).decode('ascii')
    except Exception:
        return ''

@app.post('/api/invoices/inspect')
async def inspect_invoices(files: list[UploadFile] = File(...)):
    results, hashes, numbers = [], {}, {}
    for file in files:
        name = file.filename or '未命名文件'
        try:
            content = await file.read(30 * 1024 * 1024 + 1)
            digest, fields, reason, existing = await asyncio.to_thread(inspect_invoice_file,name,content)
            number = str(fields.get('invoice_number') or '').strip()
            duplicate, matched_name = '', ''
            if existing:
                duplicate, matched_name = 'existing_file', existing['original_name']
            elif digest in hashes:
                duplicate, matched_name = 'batch_file', hashes[digest]
            elif number:
                with connect() as db:
                    match = db.execute('SELECT original_name FROM invoices WHERE invoice_number=? LIMIT 1',(number,)).fetchone()
                if match:
                    duplicate, matched_name = 'existing_invoice', match['original_name']
                elif number in numbers:
                    duplicate, matched_name = 'batch_invoice', numbers[number]
            hashes.setdefault(digest,name)
            if number: numbers.setdefault(number,name)
            thumbnail = await asyncio.to_thread(inspect_thumbnail,name,content)
            results.append({'name':name,'size':len(content),'sha256':digest,'fields':{key:fields.get(key) for key in ('buyer','seller','amount_cents','invoice_date','invoice_number')},'reason':reason,'duplicate':duplicate,'matched_name':matched_name,'thumbnail':thumbnail})
        except ValueError as exc:
            results.append({'name':name,'error':str(exc)})
        except Exception:
            results.append({'name':name,'error':'文件检查失败，请重试'})
        finally:
            await file.close()
    return results

@app.post('/api/invoices/upload')
async def upload(files: list[UploadFile] = File(...)):
    results = []
    for file in files:
        try:
            content = await file.read(30 * 1024 * 1024 + 1)
            ident, created = await asyncio.to_thread(import_file, file.filename or '发票.pdf', content, '本地上传')
            with connect() as db:
                invoice = db.execute('SELECT recognition_status,failure_reason FROM invoices WHERE id=?',(ident,)).fetchone()
            results.append({'name':file.filename,'id':ident,'created':created,'recognition_status':invoice['recognition_status'],'failure_reason':invoice['failure_reason']})
        except ValueError as exc:
            results.append({'name':file.filename,'error':str(exc)})
        except Exception:
            results.append({'name':file.filename,'error':'导入失败，请重试'})
        finally:
            await file.close()
    return results

def refresh_email_status(db, invoice_id):
    email_ids = [r['email_id'] for r in db.execute('SELECT email_id FROM invoice_emails WHERE invoice_id=?',(invoice_id,))]
    for email_id in email_ids:
        statuses = db.execute('SELECT i.recognition_status,i.duplicate_state FROM invoices i JOIN invoice_emails ie ON i.id=ie.invoice_id WHERE ie.email_id=?',(email_id,)).fetchall()
        if statuses:
            state = '识别失败' if any(r['recognition_status']=='识别失败' and r['duplicate_state']!='confirmed' for r in statuses) else ('重复发票' if any(r['duplicate_state']=='confirmed' for r in statuses) else '已识别')
            db.execute("UPDATE emails SET status=? WHERE id=? AND status NOT IN ('导入失败','待处理','发票链接待处理')",(state,email_id))

def duplicate_for_update(db, row, number):
    same_number = number == row['invoice_number']
    if same_number and row['duplicate_state']=='dismissed': return None,'dismissed'
    if not number: return None,'unverified'
    duplicate = duplicate_invoice(db,number,row['id'])
    if not duplicate: return None,''
    state = row['duplicate_state'] if same_number and row['duplicate_state'] in ('pending','confirmed') else 'pending'
    return duplicate,state

@app.patch('/api/invoices/{invoice_id}')
def update_invoice(invoice_id: int, value: InvoiceUpdate):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not row: raise HTTPException(404,'发票不存在')
        if row['reimbursed']: raise HTTPException(409,'已报销发票不能修改，请先撤销报销标记')
        acknowledged = row['duplicate_state']=='dismissed' and value.invoice_number.strip()==row['invoice_number']
        if value.invoice_number.strip() and not acknowledged and db.execute('SELECT 1 FROM group_invoices WHERE invoice_id=?',(invoice_id,)).fetchone():
            if db.execute('SELECT 1 FROM invoices i JOIN group_invoices gi ON gi.invoice_id=i.id WHERE i.invoice_number=? AND i.id<>?',(value.invoice_number.strip(),invoice_id)).fetchone():
                raise HTTPException(409,'已分组发票的号码与其他发票重复')
        missing = [name for name, v in [('抬头',value.buyer.strip()),('商户',value.seller.strip()),('金额',value.amount_cents),('开票日期',value.invoice_date)] if v is None or v == '']
        duplicate,duplicate_state = duplicate_for_update(db,row,value.invoice_number.strip())
        reason = '未填写：'+'、'.join(missing) if missing else ''
        if duplicate: reason = duplicate_reason(reason)
        if duplicate_state=='unverified': reason = (reason + '；' if reason else '') + UNVERIFIED_REASON
        db.execute('UPDATE invoices SET buyer=?,seller=?,amount_cents=?,invoice_date=?,invoice_number=?,export_name=?,recognition_status=?,failure_reason=?,duplicate_of=?,duplicate_state=? WHERE id=?', (value.buyer.strip(),value.seller.strip(),value.amount_cents,value.invoice_date.isoformat() if value.invoice_date else '',value.invoice_number.strip(),value.export_name.strip(),'识别失败' if reason else '已识别',reason,duplicate['id'] if duplicate else None,duplicate_state,invoice_id))
        refresh_email_status(db, invoice_id)
        if row['invoice_number'] != value.invoice_number.strip():
            affected = db.execute('SELECT * FROM invoices WHERE duplicate_of=?', (invoice_id,)).fetchall()
            for other in affected:
                match = duplicate_invoice(db, other['invoice_number'], other['id'])
                parts = [part for part in other['failure_reason'].split('；') if part and part != DUPLICATE_REASON]
                if match: parts.append(DUPLICATE_REASON)
                reason = '；'.join(parts)
                db.execute('UPDATE invoices SET duplicate_of=?,duplicate_state=?,failure_reason=?,recognition_status=? WHERE id=?',
                           (match['id'] if match else None, 'pending' if match else '', reason, '识别失败' if reason else '已识别', other['id']))
                refresh_email_status(db, other['id'])
    return {'ok':True}

@app.patch('/api/invoices/{invoice_id}/duplicate')
def resolve_duplicate(invoice_id: int, value: DuplicateDecision):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not row: raise HTTPException(404,'发票不存在')
        if row['reimbursed']: raise HTTPException(409,'已报销发票不能修改')
        if row['duplicate_state'] not in ('pending','confirmed','unverified') or (row['duplicate_state']!='unverified' and not row['duplicate_of']):
            raise HTTPException(409,'该发票没有待处理的重复提示')
        if row['duplicate_state']=='unverified' and value.decision!='distinct':
            raise HTTPException(409,'未识别票号的发票只能确认后继续')
        if value.decision=='confirm':
            db.execute("UPDATE invoices SET duplicate_state='confirmed' WHERE id=?",(invoice_id,))
        elif value.decision=='reopen':
            db.execute("UPDATE invoices SET duplicate_state='pending' WHERE id=?",(invoice_id,))
        else:
            reason = '；'.join(part for part in row['failure_reason'].split('；') if part and part not in (DUPLICATE_REASON,UNVERIFIED_REASON))
            missing = [label for key,label in [('buyer','抬头'),('seller','商户'),('amount_cents','金额'),('invoice_date','开票日期')] if row[key] is None or row[key]=='']
            if missing and not reason: reason = '未填写：'+'、'.join(missing)
            db.execute("UPDATE invoices SET duplicate_of=NULL,duplicate_state='dismissed',failure_reason=?,recognition_status=? WHERE id=?",(reason,'识别失败' if reason else '已识别',invoice_id))
        refresh_email_status(db,invoice_id)
    return {'ok':True}

_ai_busy = set()
_ai_busy_lock = threading.Lock()

@contextmanager
def recognition_attempt(invoice_id):
    with _ai_busy_lock:
        if invoice_id in _ai_busy: raise HTTPException(409,'该发票的识别正在进行中')
        _ai_busy.add(invoice_id)
    try:
        yield
    finally:
        with _ai_busy_lock: _ai_busy.discard(invoice_id)

def check_invoice_unchanged(db, invoice_id, before):
    db.execute('BEGIN IMMEDIATE')
    latest = db.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
    keys = ('buyer','seller','amount_cents','invoice_date','invoice_number','recognition_status','failure_reason','ai_suggestion','duplicate_of','duplicate_state')
    if not latest or latest['reimbursed'] or any(latest[k] != before[k] for k in keys):
        raise HTTPException(409,'识别期间发票已发生变更，未覆盖现有内容')

@app.post('/api/invoices/{invoice_id}/retry')
def retry(invoice_id: int):
    with recognition_attempt(invoice_id):
        with connect() as db:
            row = db.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not row: raise HTTPException(404,'发票不存在')
        if row['reimbursed']: raise HTTPException(409,'已报销发票不能重新识别')
        fields, reason = recognize(FILES / row['stored_name'])
        fields, reason, suggestion = assist(FILES / row['stored_name'], fields, reason)
        with connect() as db:
            check_invoice_unchanged(db, invoice_id, row)
            number = row['invoice_number'] or fields.get('invoice_number','')
            duplicate,duplicate_state = duplicate_for_update(db,row,number)
            if duplicate: reason = duplicate_reason(reason)
            if duplicate_state=='unverified': reason = (reason + '；' if reason else '') + UNVERIFIED_REASON
            db.execute('UPDATE invoices SET buyer=?,seller=?,amount_cents=?,invoice_date=?,invoice_number=?,recognition_status=?,failure_reason=?,ai_suggestion=?,duplicate_of=?,duplicate_state=? WHERE id=?',(row['buyer'] or fields.get('buyer',''),row['seller'] or fields.get('seller',''),row['amount_cents'] if row['amount_cents'] is not None else fields.get('amount_cents'),row['invoice_date'] or fields.get('invoice_date',''),number,'识别失败' if reason else '已识别',reason,json.dumps(suggestion,ensure_ascii=False) if suggestion else row['ai_suggestion'],duplicate['id'] if duplicate else None,duplicate_state,invoice_id))
            refresh_email_status(db, invoice_id)
        return {'ok':True, 'reason':reason}

@app.post('/api/invoices/{invoice_id}/ai')
def ai_invoice(invoice_id: int):
    with recognition_attempt(invoice_id):
        with connect() as db:
            row = db.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not row: raise HTTPException(404,'发票不存在')
        if row['reimbursed']: raise HTTPException(409,'已报销发票不能重新识别')
        before = dict(row)
        current = {key: before[key] for key in ('buyer','seller','amount_cents','invoice_date','invoice_number')}
        try:
            fields, reason, suggestion = assist(FILES / row['stored_name'], current, before['failure_reason'], force=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502,'AI 辅助失败，请检查模型配置后重试') from None
        with connect() as db:
            check_invoice_unchanged(db, invoice_id, before)
            duplicate,duplicate_state = duplicate_for_update(db,before,fields.get('invoice_number',''))
            if duplicate: reason = duplicate_reason(reason)
            if duplicate_state=='unverified': reason = (reason + '；' if reason else '') + UNVERIFIED_REASON
            db.execute('UPDATE invoices SET buyer=?,seller=?,amount_cents=?,invoice_date=?,invoice_number=?,recognition_status=?,failure_reason=?,ai_suggestion=?,duplicate_of=?,duplicate_state=? WHERE id=?',
                       (fields.get('buyer',''),fields.get('seller',''),fields.get('amount_cents'),fields.get('invoice_date',''),fields.get('invoice_number',''),'识别失败',reason,json.dumps(suggestion,ensure_ascii=False),duplicate['id'] if duplicate else None,duplicate_state,invoice_id))
            refresh_email_status(db, invoice_id)
        return {'ok':True, 'message':reason}


@app.get('/api/invoices/{invoice_id}/original')
def original(invoice_id: int):
    with connect() as db:
        row = db.execute('SELECT stored_name,original_name FROM invoices WHERE id=?',(invoice_id,)).fetchone()
    if not row or not (FILES / row['stored_name']).is_file(): raise HTTPException(404,'原件不存在')
    ext = Path(row['stored_name']).suffix.lower()
    media = {'.pdf':'application/pdf','.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png'}.get(ext,'application/octet-stream')
    return FileResponse(FILES / row['stored_name'], media_type=media,filename=row['original_name'],content_disposition_type='inline' if ext in ('.pdf','.jpg','.jpeg','.png') else 'attachment',headers={'X-Content-Type-Options':'nosniff'})

@app.get('/api/invoices/{invoice_id}/preview')
def preview_invoice(invoice_id: int, page: int = Query(1, ge=1), info: bool = False):
    with connect() as db:
        row = db.execute('SELECT stored_name FROM invoices WHERE id=?',(invoice_id,)).fetchone()
    if not row or not (FILES / row['stored_name']).is_file(): raise HTTPException(404,'原件不存在')
    path = FILES / row['stored_name']
    if path.suffix.lower() != '.pdf': raise HTTPException(415,'此格式暂不支持预览')
    try:
        with fitz.open(path) as document:
            if info: return {'pages':document.page_count}
            if page > document.page_count: raise HTTPException(404,'预览页码不存在')
            pdf_page = document[page-1]
            scale = min(2, 1200 / max(pdf_page.rect.width, pdf_page.rect.height))
            image = pdf_page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).tobytes('png')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422,'无法预览原件，请打开原文件') from None
    return Response(content=image,media_type='image/png',headers={'X-Content-Type-Options':'nosniff'})

@app.get('/api/invoices/{invoice_id}/text-preview')
def preview_invoice_text(invoice_id: int):
    from .formats import open_ofd, read_xml
    from xml.etree import ElementTree
    with connect() as db:
        row = db.execute('SELECT stored_name FROM invoices WHERE id=?',(invoice_id,)).fetchone()
    if not row or not (FILES / row['stored_name']).is_file(): raise HTTPException(404,'原件不存在')
    path = FILES / row['stored_name']
    ext = path.suffix.lower()
    if ext not in ('.xml','.ofd'): raise HTTPException(415,'此格式不支持文字预览')
    try:
        if ext == '.xml':
            root = read_xml(path.read_bytes())
            ElementTree.indent(root, space='  ')
            text = ElementTree.tostring(root,encoding='unicode')
        else:
            page_text, fields = [], []
            with open_ofd(path.read_bytes()) as archive:
                for entry in archive.infolist():
                    if not entry.filename.lower().endswith('.xml') or 'sign' in entry.filename.lower(): continue
                    try: root = read_xml(archive.read(entry))
                    except ValueError: continue
                    for node in root.iter():
                        if not isinstance(node.tag,str) or not node.text or not node.text.strip(): continue
                        tag, value = node.tag.split('}')[-1], node.text.strip()
                        if tag == 'TextCode': page_text.append(value)
                        elif len(node) == 0 and len(value) <= 500: fields.append(f'{tag}: {value}')
                    if sum(map(len,page_text)) + sum(map(len,fields)) >= 40000: break
            text = '\n'.join(page_text or fields)
    except (ValueError,OSError,KeyError,RecursionError,zipfile.BadZipFile):
        raise HTTPException(422,'无法读取原件内容') from None
    return {'text':text[:40000], 'truncated':len(text)>40000}

@app.post('/api/invoices/export')
def export(ids: ExportIds):
    with connect() as db:
        rows = db.execute('SELECT * FROM invoices WHERE id IN ('+','.join('?'*len(ids.ids))+')',ids.ids).fetchall()
        setting = db.execute("SELECT value FROM settings WHERE key='filename_fields'").fetchone()
    if len(rows)!=len(set(ids.ids)): raise HTTPException(404,'部分发票不存在')
    if ids.pdf_only and any(Path(row['stored_name']).suffix.lower() != '.pdf' for row in rows):
        raise HTTPException(422,'所选发票中包含 OFD 或 XML 或图片原件，没有 PDF 原件；请选择原格式导出')
    fields = json.loads(setting['value']) if setting else ['抬头','商户','金额','开票日期']
    output = tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        used = set()
        for row in rows:
            path = FILES / row['stored_name']
            if not path.is_file(): raise HTTPException(404,'部分原件不存在')
            name = export_filename(row, fields)
            ext = Path(row['stored_name']).suffix.lower()
            stem = name[:-len(ext)]
            idx = 2
            while name.casefold() in used:
                name = f'{stem}_{idx}{ext}'; idx += 1
            used.add(name.casefold())
            z.write(path, name)
    output.seek(0)
    def stream():
        try:
            while chunk := output.read(1024*1024): yield chunk
        finally: output.close()
    return StreamingResponse(stream(), media_type='application/zip',headers={'Content-Disposition':'attachment; filename="invoices.zip"'})

@app.get('/api/groups')
def groups():
    with connect() as db:
        result = []
        for g in db.execute('SELECT * FROM groups ORDER BY id DESC').fetchall():
            item = dict(g)
            item['invoices'] = [dict(r) for r in db.execute('SELECT i.* FROM invoices i JOIN group_invoices gi ON i.id=gi.invoice_id WHERE gi.group_id=? ORDER BY i.id',(g['id'],))]
            result.append(item)
        return result

@app.post('/api/groups')
def create_group(group: GroupCreate):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not group.name.strip(): raise HTTPException(422,'请输入报销组名称')
        ids = list(dict.fromkeys(group.ids))
        rows = db.execute('SELECT id,reimbursed,recognition_status,invoice_number,duplicate_state FROM invoices WHERE id IN ('+','.join('?'*len(ids))+')',ids).fetchall()
        if len(rows) != len(ids): raise HTTPException(404,'部分发票不存在')
        if any(r['reimbursed'] for r in rows): raise HTTPException(409,'已报销发票不能再次分组')
        if any(r['recognition_status'] != '已识别' for r in rows): raise HTTPException(409,'请先处理识别失败的发票')
        numbers = [r['invoice_number'] for r in rows if r['invoice_number'] and r['duplicate_state']!='dismissed']
        if len(numbers) != len(set(numbers)): raise HTTPException(409,'选择的发票存在相同发票号码，请核对')
        for number in numbers:
            existing = db.execute("SELECT 1 FROM invoices i JOIN group_invoices gi ON gi.invoice_id=i.id WHERE i.invoice_number=? AND i.duplicate_state<>'dismissed' AND i.id NOT IN ("+','.join('?'*len(ids))+') LIMIT 1',[number,*ids]).fetchone()
            if existing: raise HTTPException(409,'存在已分组的相同发票号码，请核对')
        if db.execute('SELECT 1 FROM group_invoices WHERE invoice_id IN ('+','.join('?'*len(ids))+')',ids).fetchone(): raise HTTPException(409,'部分发票已在其他报销组中')
        group_id = db.execute('INSERT INTO groups(name,created_at) VALUES(?,?)',(group.name.strip(),now())).lastrowid
        db.executemany('INSERT INTO group_invoices(group_id,invoice_id) VALUES(?,?)',[(group_id,i) for i in ids])
    return {'id':group_id}

@app.patch('/api/groups/{group_id}')
def set_group(group_id: int, data: GroupStatus):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT id FROM groups WHERE id=?',(group_id,)).fetchone(): raise HTTPException(404,'报销组不存在')
        if data.reimbursed and db.execute("SELECT 1 FROM invoices i JOIN group_invoices gi ON gi.invoice_id=i.id WHERE gi.group_id=? AND i.recognition_status<>'已识别' LIMIT 1",(group_id,)).fetchone():
            raise HTTPException(409,'组内有待处理发票，请先补充完整')
        db.execute('UPDATE groups SET reimbursed=?,reimbursed_at=? WHERE id=?',(int(data.reimbursed),now() if data.reimbursed else None,group_id))
        db.execute('UPDATE invoices SET reimbursed=? WHERE id IN (SELECT invoice_id FROM group_invoices WHERE group_id=?)',(int(data.reimbursed),group_id))
    return {'ok':True}

@app.get('/api/accounts')
def accounts():
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT id,name,email,enabled,last_sync,last_error FROM accounts ORDER BY id')]

@app.post('/api/accounts')
def add_account(data: Account):
    try:
        with connect() as db:
            account_id = db.execute('INSERT INTO accounts(name,email,secret) VALUES(?,?,?)',(data.name.strip(),data.email.lower(),cipher().encrypt(data.auth_code.encode()).decode())).lastrowid
        return {'id':account_id}
    except sqlite3.IntegrityError: raise HTTPException(409,'该邮箱已添加')

@app.patch('/api/accounts/{account_id}')
def update_account(account_id: int, data: AccountUpdate):
    with connect() as db:
        if not db.execute('SELECT id FROM accounts WHERE id=?',(account_id,)).fetchone(): raise HTTPException(404,'邮箱不存在')
        if data.enabled is not None:
            db.execute('UPDATE accounts SET enabled=? WHERE id=?',(int(data.enabled),account_id))
        if data.auth_code is not None:
            db.execute('UPDATE accounts SET secret=?,last_error=NULL WHERE id=?',(cipher().encrypt(data.auth_code.encode()).decode(),account_id))
    return {'ok':True}

async def run_sync(account_id):
    async with storage_lock():
        try: await asyncio.to_thread(sync_account,account_id)
        except Exception: pass

@app.post('/api/accounts/{account_id}/sync')
def sync(account_id: int, background: BackgroundTasks):
    with connect() as db:
        if not db.execute('SELECT id FROM accounts WHERE id=?',(account_id,)).fetchone(): raise HTTPException(404,'邮箱不存在')
    background.add_task(run_sync,account_id)
    return {'ok':True}

@app.get('/api/summary')
def summary():
    with connect() as db:
        failed=db.execute("SELECT count(*) FROM invoices WHERE recognition_status='识别失败' AND duplicate_state<>'confirmed'").fetchone()[0]
        pending=db.execute("SELECT count(*) FROM emails WHERE status IN ('导入失败','发票链接待处理')").fetchone()[0]
        counts=db.execute("SELECT count(*) total,COALESCE(sum(CASE WHEN reimbursed=0 THEN 1 ELSE 0 END),0) unpaid,COALESCE(sum(CASE WHEN reimbursed=0 THEN amount_cents ELSE 0 END),0) unpaid_amount FROM invoices WHERE recognition_status='已识别'").fetchone()
    return {'failed_invoices':failed,'pending_emails':pending,'invoices':counts['total'],'unreimbursed':counts['unpaid'],'unreimbursed_amount_cents':counts['unpaid_amount']}

@app.get('/api/emails')
def emails(account_id: Optional[int]=None, limit: Optional[int]=Query(None,ge=1,le=100), offset: int=Query(0,ge=0), invoice_status: Literal['all','invoice','has','suspected','none','recognized','failed','deleted','duplicate','pending','processing_failed','link_pending']='all', attention: bool=False):
    with connect() as db:
        query='''SELECT e.id,e.account_id,e.sender,e.subject,e.received_at,e.status,e.folder,a.name account_name,
                        COALESCE(c.invoice_count,0) invoice_count,COALESCE(c.recognized_count,0) recognized_count,
                        COALESCE(c.failed_count,0) failed_count,COALESCE(c.duplicate_count,0) duplicate_count
                 FROM emails e JOIN accounts a ON e.account_id=a.id
                 LEFT JOIN (SELECT ie.email_id,COUNT(*) invoice_count,
                            SUM(CASE WHEN i.recognition_status='已识别' THEN 1 ELSE 0 END) recognized_count,
                            SUM(CASE WHEN i.recognition_status='识别失败' AND i.duplicate_state<>'confirmed' THEN 1 ELSE 0 END) failed_count,
                            SUM(CASE WHEN i.duplicate_state='confirmed' THEN 1 ELSE 0 END) duplicate_count
                            FROM invoice_emails ie JOIN invoices i ON i.id=ie.invoice_id GROUP BY ie.email_id) c ON c.email_id=e.id'''
        where=[];args=[]
        if account_id: where.append('e.account_id=?');args.append(account_id)
        if attention: where.append("e.status IN ('导入失败','发票链接待处理')")
        filters={'invoice':'COALESCE(c.invoice_count,0)>0',
                 'has':'COALESCE(c.recognized_count,0)>0 OR COALESCE(c.duplicate_count,0)>0',
                 'suspected':'COALESCE(c.invoice_count,0)>0 AND COALESCE(c.recognized_count,0)=0 AND COALESCE(c.duplicate_count,0)=0',
                 'none':"COALESCE(c.invoice_count,0)=0 AND e.status NOT IN ('发票已删除','待处理','导入失败','发票链接待处理','识别失败')",
                 'recognized':'COALESCE(c.recognized_count,0)>0','failed':'COALESCE(c.failed_count,0)>0',
                 'deleted':"COALESCE(c.invoice_count,0)=0 AND e.status='发票已删除'",
                 'duplicate':'COALESCE(c.duplicate_count,0)>0',
                 'pending':"e.status='待处理' OR (COALESCE(c.invoice_count,0)>0 AND COALESCE(c.recognized_count,0)=0 AND COALESCE(c.failed_count,0)=0 AND COALESCE(c.duplicate_count,0)=0)",
                 'processing_failed':"e.status='导入失败' OR (e.status='识别失败' AND COALESCE(c.invoice_count,0)=0)",
                 'link_pending':"e.status='发票链接待处理'"}
        if invoice_status in filters: where.append('('+filters[invoice_status]+')')
        clause=(' WHERE '+' AND '.join(where)) if where else ''
        sql=query+clause
        if limit is None: return [dict(r) for r in db.execute(sql+' ORDER BY e.id DESC',args)]
        total=db.execute('SELECT count(*) FROM ('+sql+')',args).fetchone()[0]
        rows=db.execute(sql+' ORDER BY e.id DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        return {'items':[dict(r) for r in rows],'total':total,'limit':limit,'offset':offset}

@app.get('/api/emails/{email_id}')
def email_detail(email_id: int):
    with connect() as db:
        row=db.execute('SELECT e.*,a.name account_name FROM emails e JOIN accounts a ON e.account_id=a.id WHERE e.id=?',(email_id,)).fetchone()
        if not row: raise HTTPException(404,'邮件不存在')
        result=dict(row)
        result['attachments']=[dict(r) for r in db.execute('SELECT name,content_type,size FROM email_attachments WHERE email_id=?',(email_id,))]
        result['invoices']=[dict(r) for r in db.execute('SELECT i.id,i.original_name,i.recognition_status,i.duplicate_state FROM invoices i JOIN invoice_emails ie ON ie.invoice_id=i.id WHERE ie.email_id=?',(email_id,))]
        result['invoice_count']=len(result['invoices'])
        result['recognized_count']=sum(i['recognition_status']=='已识别' for i in result['invoices'])
        result['failed_count']=sum(i['recognition_status']=='识别失败' and i['duplicate_state']!='confirmed' for i in result['invoices'])
        result['duplicate_count']=sum(i['duplicate_state']=='confirmed' for i in result['invoices'])
        images=[]; cached={}
        def placeholder(url):
            if url in cached: return cached[url]
            marker='https://images.invalid/'+secrets.token_urlsafe(18)
            cached[url]=marker
            if len(images)<64: images.append({'marker':marker,'url':url})
            return marker
        if result['html_body']:
            result['html_body']=proxy_html_images(result['html_body'],placeholder)
        result['remote_images']=images
        return result

@app.post('/api/email-image')
def email_image(data: EmailImage):
    try:
        content,media=public_image(data.url)
        return Response(content=content,media_type=media,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    except Exception:
        raise HTTPException(422,'无法加载邮件图片') from None

@app.post('/api/emails/{email_id}/refresh-html')
def refresh_html(email_id: int):
    try:
        refresh_email_html(email_id)
    except ValueError:
        raise HTTPException(404,'邮件不存在') from None
    except Exception:
        raise HTTPException(502,'暂时无法从邮箱读取原始排版') from None
    return email_detail(email_id)

@app.post('/api/emails/{email_id}/reprocess')
def reprocess_mail(email_id: int):
    try:
        result=reprocess_email(email_id)
    except MailReprocessError as exc:
        raise HTTPException(409,str(exc)) from None
    except Exception:
        raise HTTPException(502,'无法重新读取邮件，请检查邮箱授权和网络') from None
    retried=retry_failed=0
    for ident in result['existing_ids']:
        with connect() as db:
            row=db.execute('SELECT recognition_status,duplicate_state,reimbursed FROM invoices WHERE id=?',(ident,)).fetchone()
        if row and row['recognition_status']=='识别失败' and row['duplicate_state'] not in ('pending','confirmed') and not row['reimbursed']:
            try:
                retry(ident)
                retried+=1
            except Exception:
                retry_failed+=1
    with connect() as db:
        row=db.execute('SELECT status FROM emails WHERE id=?',(email_id,)).fetchone()
    return {'created':len(result['created_ids']), 'existing':len(result['existing_ids']),
            'retried':retried,'retry_failed':retry_failed,'errors':result['errors'],
            'files_found':result['files_found'],'status':row['status'] if row else result['status']}

@app.get('/api/ai-settings')
def ai_settings():
    return get_ai_settings()

@app.put('/api/ai-settings')
def update_ai_settings(data: AISettings):
    try:
        return save_ai_settings(data.base_url,data.model,data.api_key,data.auto_enabled,data.clear_key)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None

@app.post('/api/ai-settings/test-connection')
def test_ai_connection(data: AISettings):
    try:
        config=ai.test_settings(data.base_url,data.model,data.api_key,data.clear_key)
        return ai.test_connection(config)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None

@app.post('/api/ai-settings/test-recognition')
def test_ai_recognition(data: AISettings):
    try:
        config=ai.test_settings(data.base_url,data.model,data.api_key,data.clear_key)
        return ai.test_recognition(config)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None


@app.get('/api/settings')
def settings():
    with connect() as db:
        row=db.execute("SELECT value FROM settings WHERE key='filename_fields'").fetchone()
    return {'fields':json.loads(row['value']) if row else ['抬头','商户','金额','开票日期']}

@app.put('/api/settings')
def update_settings(data: Template):
    if len(set(data.fields))!=len(data.fields) or any(x not in ['抬头','商户','金额','开票日期'] for x in data.fields): raise HTTPException(400,'命名字段无效')
    with connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES('filename_fields',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(data.fields,ensure_ascii=False),))
    return {'ok':True}

app.mount('/',StaticFiles(directory=Path(__file__).resolve().parent.parent/'static',html=True),name='static')
