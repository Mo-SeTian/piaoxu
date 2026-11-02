import base64
import email
import imaplib
import io
import zipfile
from pathlib import PurePosixPath
import re
import threading
from html.parser import HTMLParser
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from .db import connect
from .download import candidates, download
from .email_html import HTML_VERSION, prepare_html
from .service import cipher, import_file, now

locks_guard = threading.Lock()
account_locks = {}

class MailReprocessError(Exception):
    pass

def account_lock(account_id):
    with locks_guard:
        return account_locks.setdefault(account_id, threading.Lock())

def decode(value):
    try:
        parts = []
        for part, charset in decode_header(value or ''):
            if isinstance(part, bytes) and charset == 'unknown-8bit':
                for encoding in ('utf-8', 'gb18030'):
                    try:
                        part = part.decode(encoding)
                        break
                    except UnicodeDecodeError: pass
                if isinstance(part, bytes): part = part.decode('utf-8', errors='replace')
                charset = None
            parts.append((part, charset))
        return str(make_header(parts))
    except Exception: return str(value or '')

def decode_folder(raw):
    def part(match):
        value = match.group(1)
        if not value: return '&'
        try:
            data = value.replace(',', '/')
            return base64.b64decode(data + '=' * (-len(data) % 4)).decode('utf-16-be')
        except Exception: return '&' + value + '-'
    return re.sub(r'&([^-]*)-', part, raw)

def folders(mail):
    status, data = mail.list()
    if status != 'OK': raise RuntimeError('无法读取邮箱文件夹')
    result = []
    for line in data or []:
        if not isinstance(line, bytes) or b'\\noselect' in line.lower():
            continue
        match = re.match(rb'^\([^)]*\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)$', line, re.I)
        if not match: continue
        token = match.group(1).strip()
        raw = token[1:-1].replace(b'\\"',b'"').replace(b'\\\\',b'\\') if token.startswith(b'"') and token.endswith(b'"') else token
        try: display = decode_folder(raw.decode('ascii'))
        except UnicodeDecodeError: display = raw.decode('utf-8',errors='replace')
        result.append((token,display))
    return result

class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('style','script'): self.hidden+=1
        elif tag in ('p','br','div','tr'): self.parts.append('\n')
        elif tag == 'a':
            url = dict(attrs).get('href') or ''
            if url.startswith(('https://','http://')): self.parts.append(' 链接：' + url + ' ')
    def handle_endtag(self,tag):
        if tag in ('style','script') and self.hidden: self.hidden-=1
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)

def text_part(part):
    payload = part.get_payload(decode=True) or b''
    try: return payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
    except LookupError: return payload.decode('utf-8', errors='replace')

def unpack_attachments(content):
    if len(content)>30*1024*1024: raise ValueError('附件过大')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos=archive.infolist()
            if len(infos)>100 or sum(i.file_size for i in infos)>60*1024*1024:
                raise ValueError('附件内容过大')
            result=[]
            for info in infos:
                path=PurePosixPath(info.filename)
                if info.flag_bits & 1 or path.is_absolute() or '..' in path.parts or '\\' in info.filename:
                    raise ValueError('附件结构无效')
                if not info.is_dir() and path.suffix.lower() in ('.pdf','.ofd','.xml','.jpg','.jpeg','.png'):
                    if info.file_size > 30*1024*1024: raise ValueError('附件过大')
                    result.append((path.name,archive.read(info)))
            return result
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ValueError('无法读取压缩附件') from exc

def process_message(mail, account_id, uid, namespace, folder, old=None, force=False):
    uid_bytes=uid.encode()
    status, payload=mail.uid('fetch',uid_bytes,'(BODY.PEEK[])')
    raw=next((p[1] for p in (payload or []) if isinstance(p,tuple) and isinstance(p[1],bytes)),None)
    if status != 'OK' or not raw:
        if force: raise MailReprocessError('邮箱中找不到原邮件，可能已被删除或移动')
        raise RuntimeError('获取邮件失败')
    message=email.message_from_bytes(raw)
    subject,sender=decode(message.get('Subject')),decode(message.get('From'))
    try: received=parsedate_to_datetime(message.get('Date')).isoformat()
    except (TypeError,ValueError,OverflowError): received=''
    body,html,attachments,metadata=[],[],[],[]
    archive_errors=[]
    image_attachments=[]
    for part in message.walk():
        if part.is_multipart(): continue
        filename=decode(part.get_filename())
        if filename:
            content=part.get_payload(decode=True) or b''
            metadata.append((filename,part.get_content_type(),len(content)))
            if filename.lower().endswith(('.pdf','.ofd','.xml')): attachments.append((filename,content))
            elif filename.lower().endswith(('.jpg','.jpeg','.png')) and part.get_content_disposition() == 'attachment':
                image_attachments.append((filename,content))
            elif filename.lower().endswith('.zip'):
                try:
                    for entry in unpack_attachments(content):
                        (image_attachments if entry[0].lower().endswith(('.jpg','.jpeg','.png')) else attachments).append(entry)
                except ValueError: archive_errors.append('压缩附件处理失败')
        elif not filename and part.get_content_type()=='text/plain': body.append(text_part(part))
        elif not filename and part.get_content_type()=='text/html': html.append(text_part(part))
    parser=PlainText()
    try: parser.feed('\n'.join(html)); html_text=''.join(parser.parts)
    except Exception: html_text=''
    if not body and html_text: body=[html_text]
    body_text='\n'.join(body)[:100000]
    invoice_hint=force or bool(re.search(r'发票|票据|invoice',subject+'\n'+body_text+'\n'+html_text,re.I))
    attachments.extend((name,content) for name,content in image_attachments if invoice_hint or re.search(r'发票|票据|invoice',name,re.I))
    rich_html=prepare_html(message,html)
    links=candidates(subject+'\n'+body_text, '\n'.join(html)) if force or '发票' in (subject+body_text+html_text) else []
    with connect() as db:
        if old: email_id=old[0]
        else:
            email_id=db.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status,folder,html_body,html_checked,html_version) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)',(account_id,namespace,uid,sender,subject,received,body_text,'待处理',folder,rich_html,HTML_VERSION)).lastrowid
            db.executemany('INSERT INTO email_attachments(email_id,name,content_type,size) VALUES(?,?,?,?)',[(email_id,*item) for item in metadata])
        if old:
            db.execute('UPDATE emails SET html_body=?,html_checked=1,html_version=? WHERE id=? AND (html_checked=0 OR html_version<?)',(rich_html,HTML_VERSION,email_id,HTML_VERSION))
    errors=archive_errors
    created_ids, existing_ids = set(), set()
    for link in links:
        try: attachments.append(download(link))
        except Exception: errors.append('链接下载失败')
    for name,content in attachments:
        try:
            ident, created=import_file(name,content,'邮件',email_id)
            if created: created_ids.add(ident)
            elif ident not in created_ids: existing_ids.add(ident)
        except Exception: errors.append('附件导入失败')
    with connect() as db:
        statuses=db.execute('SELECT i.recognition_status,i.duplicate_state FROM invoices i JOIN invoice_emails ie ON ie.invoice_id=i.id WHERE ie.email_id=?',(email_id,)).fetchall()
        needs_review=any(r['recognition_status']=='识别失败' and r['duplicate_state']!='confirmed' for r in statuses)
        confirmed=any(r['duplicate_state']=='confirmed' for r in statuses)
        state=('发票链接待处理' if links and not attachments else '导入失败') if errors else ('识别失败' if needs_review else ('重复发票' if confirmed else ('已识别' if statuses else ('发票链接待处理' if '发票' in subject and 'http' in body_text else '普通邮件'))))
        db.execute('UPDATE emails SET status=? WHERE id=?',(state,email_id))
    return {'email_id':email_id,'created_ids':sorted(created_ids),'existing_ids':sorted(existing_ids),'files_found':len(attachments),'links_found':len(links),'errors':len(errors),'status':state}

def sync_folder(mail, account_id, token, folder):
    status, _ = mail.select(token, readonly=True)
    if status != 'OK': raise RuntimeError('无法读取文件夹')
    validity = (mail.response('UIDVALIDITY')[1] or [b''])[0].decode()
    if not validity: raise RuntimeError('邮箱未返回 UIDVALIDITY')
    namespace = folder + '\x00' + validity
    with connect() as db:
        existing = {r['uid']:(r['id'],r['status']) for r in db.execute('SELECT id,uid,status FROM emails WHERE account_id=? AND uidvalidity=?', (account_id, namespace))}
        failed = {r['uid'] for r in db.execute('SELECT uid FROM email_sync_failures WHERE account_id=? AND uidvalidity=?',(account_id,namespace))}
    highest = max((int(uid) for uid in existing if uid.isdigit()), default=0)
    query = ('UID', f'{highest+1}:*') if highest else ('ALL',)
    status, data = mail.uid('search', None, *query)
    if status != 'OK': raise RuntimeError('无法搜索邮件')
    discovered = data[0].split() if data and data[0] else []
    uids = sorted({int(uid) for uid in discovered if uid.isdigit() and int(uid) > highest} |
                  {int(uid) for uid in failed if uid.isdigit()} |
                  {int(uid) for uid,(_,state) in existing.items() if uid.isdigit() and state in ('待处理','导入失败','发票链接待处理')})
    processed=0; errors=0
    for uid_number in uids:
        uid=str(uid_number)
        old=existing.get(uid)
        if old and old[1] not in ('待处理','导入失败','发票链接待处理') and uid not in failed: continue
        try:
            process_message(mail,account_id,uid,namespace,folder,old)
            with connect() as db: db.execute('DELETE FROM email_sync_failures WHERE account_id=? AND uidvalidity=? AND uid=?',(account_id,namespace,uid))
            processed+=1
        except Exception:
            errors+=1
            with connect() as db:
                db.execute('INSERT OR IGNORE INTO email_sync_failures(account_id,uidvalidity,uid) VALUES(?,?,?)',(account_id,namespace,uid))
                if old: db.execute("UPDATE emails SET status='导入失败' WHERE id=?",(old[0],))
    if errors: raise RuntimeError('部分邮件同步失败，稍后重试')
    return processed

def reprocess_email(email_id):
    with connect() as db:
        entry=db.execute('SELECT account_id FROM emails WHERE id=?',(email_id,)).fetchone()
    if not entry: raise MailReprocessError('邮件不存在')
    with account_lock(entry['account_id']):
        with connect() as db:
            row=db.execute('''SELECT e.id,e.uid,e.uidvalidity,e.folder,e.status,a.id account_id,a.email,a.secret
                              FROM emails e JOIN accounts a ON a.id=e.account_id WHERE e.id=?''',(email_id,)).fetchone()
        if not row: raise MailReprocessError('邮件不存在')
        mail=None
        try:
            mail=imaplib.IMAP4_SSL('imap.qq.com',993,timeout=30)
            status,_=mail.login(row['email'],cipher().decrypt(row['secret'].encode()).decode())
            if status != 'OK': raise RuntimeError('邮箱登录失败')
            token=next((token for token,name in folders(mail) if name==row['folder']),None)
            if token is None: raise MailReprocessError('原邮件所在文件夹不存在，可能已被删除或移动')
            status,_=mail.select(token,readonly=True)
            if status != 'OK': raise MailReprocessError('无法读取原邮件所在的文件夹')
            validity=(mail.response('UIDVALIDITY')[1] or [b''])[0].decode()
            if not validity or row['uidvalidity'] != row['folder']+'\x00'+validity:
                raise MailReprocessError('邮箱文件夹已变化，无法安全定位原邮件')
            if not row['uid'].isdigit(): raise MailReprocessError('邮件标识无效，无法重新识别')
            return process_message(mail,row['account_id'],row['uid'],row['uidvalidity'],row['folder'],(row['id'],row['status']),force=True)
        finally:
            if mail:
                try: mail.logout()
                except Exception: pass

def refresh_email_html(email_id):
    with connect() as db:
        row = db.execute('''SELECT e.id,e.uid,e.uidvalidity,e.folder,e.html_checked,e.html_version,a.id account_id,a.email,a.secret
                            FROM emails e JOIN accounts a ON a.id=e.account_id WHERE e.id=?''',(email_id,)).fetchone()
    if not row: raise ValueError('邮件不存在')
    if row['html_checked'] and row['html_version']>=HTML_VERSION: return
    with account_lock(row['account_id']):
        with connect() as db:
            current=db.execute('SELECT html_checked,html_version FROM emails WHERE id=?',(email_id,)).fetchone()
        if not current: raise ValueError('邮件不存在')
        if current['html_checked'] and current['html_version']>=HTML_VERSION: return
        mail = None
        try:
            mail = imaplib.IMAP4_SSL('imap.qq.com',993,timeout=30)
            mail.login(row['email'],cipher().decrypt(row['secret'].encode()).decode())
            token = next((token for token,name in folders(mail) if name==row['folder']),None)
            if token is None: raise RuntimeError('邮件文件夹不存在')
            status,_ = mail.select(token,readonly=True)
            validity = (mail.response('UIDVALIDITY')[1] or [b''])[0].decode()
            if status != 'OK' or row['uidvalidity'] != row['folder']+'\x00'+validity: raise RuntimeError('邮件文件夹已变化')
            status,payload = mail.uid('fetch',row['uid'].encode(),'(BODY.PEEK[])')
            raw = next((p[1] for p in (payload or []) if isinstance(p,tuple) and isinstance(p[1],bytes)),None)
            if status != 'OK' or not raw: raise RuntimeError('无法读取邮件')
            message = email.message_from_bytes(raw)
            parts = [text_part(part) for part in message.walk() if not part.is_multipart() and not part.get_filename() and part.get_content_type()=='text/html']
            rich_html = prepare_html(message,parts)
            with connect() as db:
                db.execute('UPDATE emails SET html_body=?,html_checked=1,html_version=? WHERE id=? AND (html_checked=0 OR html_version<?)',(rich_html,HTML_VERSION,email_id,HTML_VERSION))
        finally:
            if mail:
                try: mail.logout()
                except Exception: pass

def sync_account(account_id):
    with account_lock(account_id):
        with connect() as db:
            row=db.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
            if not row: raise ValueError('邮箱不存在')
            account=dict(row)
        mail=None
        try:
            mail=imaplib.IMAP4_SSL('imap.qq.com',993,timeout=30)
            mail.login(account['email'],cipher().decrypt(account['secret'].encode()).decode())
            mailboxes=folders(mail)
            if not mailboxes: raise RuntimeError('没有可读取的邮箱文件夹')
            count, failed=0,0
            for token,folder in mailboxes:
                try: count+=sync_folder(mail,account_id,token,folder)
                except Exception: failed+=1
            with connect() as db:
                db.execute('UPDATE accounts SET last_sync=?,last_error=? WHERE id=?',(now(),f'{failed} 个文件夹同步失败，稍后重试' if failed else None,account_id))
            if failed==len(mailboxes): raise RuntimeError('所有文件夹同步失败')
            return count
        except Exception as exc:
            with connect() as db: db.execute('UPDATE accounts SET last_error=? WHERE id=?',('连接或同步失败，请检查邮箱授权及网络',account_id))
            raise RuntimeError('邮箱同步失败，请检查邮箱授权及网络') from exc
        finally:
            if mail:
                try: mail.logout()
                except Exception: pass
