import io
import json
import os
import zipfile
import asyncio

import pytest
from fastapi.testclient import TestClient

def delete_payload(ids):
    from app.db import connect
    with connect() as db:
        items = []
        for ident in ids:
            row = db.execute('SELECT sha256 FROM invoices WHERE id=?', (ident,)).fetchone()
            items.append({'id':ident, 'sha256':row['sha256'] if row else '0'*64})
    return {'items':items}

@pytest.fixture
def isolated_storage(tmp_path,monkeypatch):
    import app.backup as backup
    import app.config as settings_config
    import app.db as db
    import app.main as main
    import app.service as service
    import app.deletion as deletion
    import app.storage_lock as locking
    config=tmp_path/'config';data=tmp_path/'data';files=data/'files'
    config.mkdir();files.mkdir(parents=True)
    for module in (db,backup):
        monkeypatch.setattr(module,'CONFIG',config)
        monkeypatch.setattr(module,'DATA',data)
        monkeypatch.setattr(module,'FILES',files)
        monkeypatch.setattr(module,'DB',config/'piaoxu.sqlite3')
    monkeypatch.setattr(service,'CONFIG',config)
    monkeypatch.setattr(service,'FILES',files)
    monkeypatch.setattr(deletion,'FILES',files)
    monkeypatch.setattr(main,'FILES',files)
    monkeypatch.setattr(locking,'CONFIG',config)
    monkeypatch.setattr(settings_config,'CONFIG',config)
    db.init_db()
    main.auth.ensure_auth()
    client=TestClient(main.app,headers={'X-Piaoxu-Request':'1'})
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==200
    return config,data,client,db,service,backup


def invoice_xml(number):
    return f'<Invoice><Fphm>{number}</Fphm><Kprq>2026-10-15</Kprq><Gmfmc>示例购买方</Gmfmc><Xsfmc>示例销售方</Xsfmc><Jshj>200.35</Jshj></Invoice>'.encode()


def test_native_export_download_supports_ranges_and_requires_the_same_session(isolated_storage):
    from app.main import app
    config,data,client,db,service,backup=isolated_storage
    content=invoice_xml('99000000000000000001')
    item=client.post('/api/invoices/upload',files={'files':('invoice.xml',content,'application/xml')}).json()[0]
    import hashlib,re
    pdf=b'%PDF-1.7\nsynthetic original PDF\n%%EOF'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive: archive.writestr('OFD.xml',content)
    with db.connect() as conn:
        for name,original in [('invoice.pdf',pdf),('invoice.ofd',stream.getvalue())]:
            digest=hashlib.sha256(original).hexdigest();stored=digest+os.path.splitext(name)[1]
            (data/'files'/stored).write_bytes(original)
            conn.execute('INSERT INTO invoice_originals(invoice_id,sha256,original_name,stored_name) VALUES(?,?,?,?)',(item['id'],digest,name,stored))
    prepared=client.post('/api/invoices/export/prepare',json={'ids':[item['id']]})
    assert prepared.status_code==200
    url='/api/invoices/export/download/'+prepared.json()['download_id']
    browser=TestClient(app,cookies=client.cookies)
    response=browser.get(url)
    assert response.status_code==200
    assert response.headers['content-type']=='application/zip'
    assert response.headers['accept-ranges']=='bytes'
    from urllib.parse import unquote
    filename=prepared.json()['filename']
    assert re.fullmatch(r'发票_1张_\d{8}_\d{6}\.zip',filename)
    assert unquote(response.headers['content-disposition'].split("filename*=utf-8''")[1])==filename
    assert response.headers['cache-control']=='no-store'
    assert int(response.headers['content-length'])==len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.read(name) for name in archive.namelist())==sorted([content,pdf,stream.getvalue()])
    head=browser.head(url)
    assert head.status_code==200 and head.content==b''
    assert head.headers['content-length']==response.headers['content-length']
    assert head.headers['content-disposition']==response.headers['content-disposition']
    part=browser.get(url,headers={'Range':'bytes=0-31','If-Range':response.headers['etag']})
    assert part.status_code==206 and part.content==response.content[:32]
    assert part.headers['content-range']==f'bytes 0-31/{len(response.content)}'
    assert browser.get(url,headers={'Range':'bytes=-22'}).content==response.content[-22:]
    assert browser.get(url,headers={'Range':'bytes=999999999-'}).status_code==416
    assert TestClient(app).get(url).status_code==401
    other=TestClient(app,headers={'X-Piaoxu-Request':'1'})
    assert other.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==200
    assert other.get(url).status_code==404
    assert client.post('/api/auth/logout').status_code==200
    assert browser.get(url).status_code==401


def test_prepared_exports_expire_and_failed_writes_leave_no_files(isolated_storage,monkeypatch):
    import time
    from app import export_downloads
    config,data,client,db,service,backup=isolated_storage
    root=data/'export-cache';root.mkdir(mode=0o700)
    monkeypatch.setattr(export_downloads,'ROOT',root)
    item=client.post('/api/invoices/upload',files={'files':('invoice.xml',invoice_xml('99000000000000000002'),'application/xml')}).json()[0]
    payload={'ids':[item['id']]}
    prepared=client.post('/api/invoices/export/prepare',json=payload).json()['download_id']
    path=export_downloads.resolve(prepared,client.cookies.get('piaoxu_session'))
    assert path.stat().st_mode & 0o777==0o600
    expired=time.time()-export_downloads.TTL-1
    os.utime(path,(expired,expired))
    url='/api/invoices/export/download/'+prepared
    assert client.get(url).status_code==404 and not path.exists()
    assert client.post('/api/invoices/export/prepare',json={'ids':[]}).status_code==422
    assert client.post('/api/invoices/export/prepare',json={'ids':[999999999]}).status_code==404
    assert client.post('/api/invoices/export/prepare',json={**payload,'pdf_only':True}).status_code==422
    def full_disk(*args): raise OSError('test disk full')
    monkeypatch.setattr(export_downloads.shutil,'copyfileobj',full_disk)
    failed=client.post('/api/invoices/export/prepare',json=payload)
    assert failed.status_code==500 and '临时存储空间' in failed.json()['detail']
    assert not list(root.glob('*/*.zip'))
    assert client.get(f"/api/invoices/{item['id']}/original").content==invoice_xml('99000000000000000002')


def test_login_protects_data_and_account_change_revokes_sessions(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    from app.main import app
    other=TestClient(app,headers={'X-Piaoxu-Request':'1'})
    assert other.get('/api/invoices').status_code==401
    assert other.get('/api/backup/export').status_code==401
    assert other.get('/api/invoices/1/original').status_code==401
    assert other.post('/api/auth/login',json={'username':'admin','password':'wrong'}).status_code==401
    login=other.post('/api/auth/login',json={'username':'admin','password':'admin'})
    assert login.status_code==200
    assert 'HttpOnly' in login.headers['set-cookie'] and 'SameSite=strict' in login.headers['set-cookie']
    response=client.put('/api/auth/account',json={'username':'新账号','current_password':'admin','password':'new-password'})
    assert response.status_code==200
    assert other.get('/api/invoices').status_code==401
    assert client.get('/api/auth/me').json()['username']=='新账号'
    assert other.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==401
    assert other.post('/api/auth/login',json={'username':'新账号','password':'new-password'}).status_code==200
    with db.connect() as conn:
        row=conn.execute('SELECT password_hash FROM login_user').fetchone()
    assert row['password_hash']!='new-password' and 'new-password' not in row['password_hash']
    assert other.post('/api/auth/logout').status_code==200
    assert other.get('/api/invoices').status_code==401


def test_backup_restores_login_and_invalidates_sessions(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    assert client.put('/api/auth/account',json={'username':'备份账号','current_password':'admin','password':'backup-password'}).status_code==200
    snapshot=client.get('/api/backup/export').content
    assert client.put('/api/auth/account',json={'username':'新账号','current_password':'backup-password','password':'new-password'}).status_code==200
    result=client.post('/api/backup/restore',files={'file':('backup.zip',snapshot,'application/zip')})
    assert result.status_code==200 and result.json()['reauthenticate']
    assert client.get('/api/auth/me').status_code==401
    assert client.post('/api/auth/login',json={'username':'备份账号','password':'backup-password'}).status_code==200


def test_login_limits_repeated_failed_attempts(isolated_storage):
    from app import auth
    config,data,client,db,service,backup=isolated_storage
    try:
        for _ in range(5):
            assert client.post('/api/auth/login',json={'username':'admin','password':'wrong'}).status_code==401
        assert client.post('/api/auth/login',json={'username':'admin','password':'wrong'}).status_code==429
    finally:
        auth.clear_attempts('testclient')


def test_restore_older_backup_initializes_login(isolated_storage):
    from app import auth
    config,data,client,db,service,backup=isolated_storage
    with db.connect() as conn:
        conn.execute('DROP TABLE login_sessions')
        conn.execute('DROP TABLE login_user')
    old_snapshot=io.BytesIO()
    backup.create_backup(old_snapshot)
    auth.ensure_auth()
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==200
    restored=client.post('/api/backup/restore',files={'file':('old.zip',old_snapshot.getvalue(),'application/zip')})
    assert restored.status_code==200
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==200


def test_delete_invoice_updates_group_duplicate_and_email(isolated_storage):
    import app.deletion as deletion
    config,data,client,db,service,backup=isolated_storage
    original=invoice_xml('78123456789012345678')
    invoice_id=client.post('/api/invoices/upload',files=[('files',('original.xml',original,'application/xml'))]).json()[0]['id']
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'delete-test@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status) VALUES(?,?,?,?,?,?,?,?)",(account,'test','1','','测试邮件','','','识别失败')).lastrowid
    assert service.import_file('same.xml',original,'邮件',email_id)==(invoice_id,False)
    duplicate=original.replace(b'</Invoice>',b'<Extra>different file</Extra></Invoice>')
    duplicate_id=service.import_file('duplicate.xml',duplicate,'邮件',email_id)[0]
    with db.connect() as conn:
        name=conn.execute('SELECT stored_name FROM invoices WHERE id=?',(invoice_id,)).fetchone()['stored_name']
        assert conn.execute('SELECT duplicate_of FROM invoices WHERE id=?',(duplicate_id,)).fetchone()['duplicate_of']==invoice_id
    group=client.post('/api/groups',json={'name':'测试报销组','ids':[invoice_id]}).json()['id']
    assert client.patch(f'/api/groups/{group}',json={'reimbursed':True}).status_code==200
    assert client.post('/api/invoices/delete',json=delete_payload([invoice_id])).json()=={'deleted':1,'cleanup_pending':0}
    assert not (data/'files'/name).exists()
    assert client.get(f'/api/invoices/{invoice_id}/original').status_code==404
    assert client.get('/api/groups').json()[0]['invoices']==[]
    row=next(item for item in client.get('/api/invoices').json() if item['id']==duplicate_id)
    assert row['duplicate_of'] is None and row['recognition_status']=='已识别'
    assert client.get(f'/api/emails/{email_id}').json()['status']=='已识别'
    assert client.post('/api/invoices/upload',files=[('files',('again.xml',original,'application/xml'))]).json()[0]['created']


def test_delete_missing_invoice_and_storage_failure_are_atomic(isolated_storage,monkeypatch):
    import app.deletion as deletion
    config,data,client,db,service,backup=isolated_storage
    ids=[client.post('/api/invoices/upload',files=[('files',(f'{n}.xml',invoice_xml(str(n)*20),'application/xml'))]).json()[0]['id'] for n in (1,2)]
    with db.connect() as conn:
        paths=[data/'files'/conn.execute('SELECT stored_name FROM invoices WHERE id=?',(ident,)).fetchone()['stored_name'] for ident in ids]
    assert client.post('/api/invoices/delete',json=delete_payload([ids[0],9999999])).status_code==409
    assert all(path.exists() for path in paths)
    real_replace=deletion.os.replace
    def fail_second(src,dst):
        if str(src)==str(paths[1]): raise PermissionError('simulated')
        return real_replace(src,dst)
    monkeypatch.setattr(deletion.os,'replace',fail_second)
    assert client.post('/api/invoices/delete',json=delete_payload(ids)).status_code==500
    assert len(client.get('/api/invoices').json())==2 and all(path.exists() for path in paths)
    assert not list((data/'files').glob(deletion.PREFIX+'*'))


def test_batch_delete_repairs_multiple_duplicate_references(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    original=invoice_xml('78123456789012345678')
    base=service.import_file('base.xml',original,'本地上传')[0]
    first=service.import_file('one.xml',original.replace(b'</Invoice>',b'<Extra>one</Extra></Invoice>'),'邮件')[0]
    second=service.import_file('two.xml',original.replace(b'</Invoice>',b'<Extra>two</Extra></Invoice>'),'邮件')[0]
    result=client.post('/api/invoices/delete',json=delete_payload([base,first]))
    assert result.status_code==200 and result.json()['deleted']==2
    rows=client.get('/api/invoices').json()
    assert len(rows)==1 and rows[0]['id']==second
    assert rows[0]['duplicate_of'] is None and rows[0]['recognition_status']=='已识别'
    assert client.get('/api/backup/export').status_code==200


def test_deleting_last_mailed_invoice_keeps_email(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'last-delete@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_id=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status) VALUES(?,?,?,?,?,?,?,?)',(account,'test','1','','测试邮件','','','已识别')).lastrowid
    ident=service.import_file('mail.xml',invoice_xml('78123456789012345678'),'邮件',email_id)[0]
    assert client.post('/api/invoices/delete',json=delete_payload([ident])).status_code==200
    message=client.get(f'/api/emails/{email_id}').json()
    assert message['status']=='发票已删除' and message['invoices']==[]


def synthetic_imap(messages, events, validity):
    class Mailbox:
        def __init__(self,*args,**kwargs): pass
        def login(self,address,password): return ('OK',[])
        def list(self): return ('OK',[b'(\\HasNoChildren) "/" "INBOX"'])
        def select(self,token,readonly=False):
            assert token in (b'INBOX',b'"INBOX"') and readonly
            return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[validity[0]])
        def uid(self,command,*args):
            events.append((command,args))
            if command=='search': return ('OK',[b' '.join(messages)])
            assert command=='fetch' and args[1]=='(BODY.PEEK[])'
            raw=messages.get(args[0])
            return ('OK',[(args[0],raw)] if raw else [])
        def logout(self): pass
    return Mailbox


def test_reprocess_one_email_restores_deleted_invoice_without_duplicates(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    invoice=invoice_xml('78123456789012345678')
    first=EmailMessage();first['Subject']='电子发票';first.set_content('请查收附件')
    first.add_attachment(invoice,maintype='application',subtype='xml',filename='invoice.xml')
    second=EmailMessage();second['Subject']='普通邮件';second.set_content('没有票据')
    messages={b'1':first.as_bytes(),b'2':second.as_bytes()};events=[];validity=[b'123']
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap(messages,events,validity))
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'retry-test@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_account(account)==2
    emails=client.get('/api/emails',params={'account_id':account}).json()
    invoice_mail=next(item for item in emails if item['subject']=='电子发票')
    assert (invoice_mail['invoice_count'],invoice_mail['recognized_count'],invoice_mail['failed_count'])==(1,1,0)
    email_id=invoice_mail['id']
    ident=client.get(f'/api/emails/{email_id}').json()['invoices'][0]['id']
    assert client.post('/api/invoices/delete',json=delete_payload([ident])).status_code==200
    deleted=next(item for item in client.get('/api/emails').json() if item['id']==email_id)
    assert deleted['invoice_count']==0 and deleted['status']=='发票已删除'
    assert client.patch(f'/api/accounts/{account}',json={'enabled':False}).status_code==200
    events.clear()
    restored=client.post(f'/api/emails/{email_id}/reprocess')
    assert restored.status_code==200 and restored.json()['created']==1 and restored.json()['existing']==0
    linked=client.get(f'/api/emails/{email_id}').json()['invoices']
    assert len(linked)==1 and client.get(f"/api/invoices/{linked[0]['id']}/original").content==invoice
    refreshed=next(item for item in client.get('/api/emails').json() if item['id']==email_id)
    assert (refreshed['invoice_count'],refreshed['recognized_count'],refreshed['failed_count'])==(1,1,0)
    assert [entry[1][0] for entry in events if entry[0]=='fetch']==[b'1']
    assert not any(entry[0]=='search' for entry in events)
    again=client.post(f'/api/emails/{email_id}/reprocess')
    assert again.status_code==200 and again.json()['created']==0 and again.json()['existing']==1
    assert len(client.get(f'/api/emails/{email_id}').json()['invoices'])==1
    restored_id=linked[0]['id']
    group=client.post('/api/groups',json={'name':'已报销测试组','ids':[restored_id]}).json()['id']
    assert client.patch(f'/api/groups/{group}',json={'reimbursed':True}).status_code==200
    repeated=client.post(f'/api/emails/{email_id}/reprocess')
    assert repeated.status_code==200 and repeated.json()['retried']==0
    assert next(item for item in client.get('/api/invoices').json() if item['id']==restored_id)['reimbursed']==1
    ordinary=next(item['id'] for item in emails if item['subject']=='普通邮件')
    response=client.post(f'/api/emails/{ordinary}/reprocess')
    assert response.status_code==200 and response.json()['files_found']==0


def test_reprocess_retries_failed_invoice_without_overwriting_manual_fields(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    import app.main as main
    config,data,client,db,service,backup=isolated_storage
    number='78123456789012345678'
    message=EmailMessage();message['Subject']='发票';message.set_content('附件')
    message.add_attachment(invoice_xml(number),maintype='application',subtype='xml',filename='invoice.xml')
    events=[];validity=[b'456'];messages={b'3':message.as_bytes()}
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap(messages,events,validity))
    monkeypatch.setattr(service,'recognize',lambda path:({'invoice_number':number},'识别失败'))
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'retry-failed@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_account(account)==1
    email_id=client.get('/api/emails',params={'account_id':account}).json()[0]['id']
    ident=client.get(f'/api/emails/{email_id}').json()['invoices'][0]['id']
    value={'buyer':'人工填写抬头','seller':'','amount_cents':None,'invoice_date':None,'invoice_number':number}
    assert client.patch(f'/api/invoices/{ident}',json=value).status_code==200
    fields={'buyer':'识别抬头','seller':'识别商户','amount_cents':12345,'invoice_date':'2026-11-01','invoice_number':number}
    monkeypatch.setattr(main,'recognize',lambda path:(fields.copy(),''))
    monkeypatch.setattr(main,'assist',lambda path,result,reason:(result,reason,{}))
    response=client.post(f'/api/emails/{email_id}/reprocess')
    assert response.status_code==200 and response.json()['retried']==1
    row=next(item for item in client.get('/api/invoices').json() if item['id']==ident)
    assert row['buyer']=='人工填写抬头' and row['seller']=='识别商户' and row['recognition_status']=='已识别'


def test_reprocess_rejects_changed_folder_or_missing_message(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    message=EmailMessage();message['Subject']='普通邮件';message.set_content('测试')
    events=[];validity=[b'777'];messages={b'9':message.as_bytes()}
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap(messages,events,validity))
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'retry-missing@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_account(account)==1
    email_id=client.get('/api/emails',params={'account_id':account}).json()[0]['id']
    events.clear();validity[0]=b'778'
    changed=client.post(f'/api/emails/{email_id}/reprocess')
    assert changed.status_code==409 and '文件夹已变化' in changed.json()['detail']
    assert not events
    validity[0]=b'777';messages.clear()
    missing=client.post(f'/api/emails/{email_id}/reprocess')
    assert missing.status_code==409 and '找不到原邮件' in missing.json()['detail']
    assert client.get(f'/api/emails/{email_id}').status_code==200


def test_manual_reprocess_checks_direct_links_and_unlabelled_images(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    from PIL import Image
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    linked=EmailMessage();linked['Subject']='Receipt';linked.set_content('https://example.test/invoice.xml')
    picture=io.BytesIO();Image.new('RGB',(20,20),'white').save(picture,format='PNG')
    image=EmailMessage();image['Subject']='Receipt';image.set_content('see attached')
    image.add_attachment(picture.getvalue(),maintype='image',subtype='png',filename='receipt.png')
    events=[];validity=[b'999'];messages={b'1':linked.as_bytes(),b'2':image.as_bytes()}
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap(messages,events,validity))
    monkeypatch.setattr(mail,'download',lambda url:('invoice.xml',invoice_xml('78123456789012345678')))
    def recognize(path):
        number='78123456789012345678' if path.suffix=='.xml' else '78123456789012345679'
        return {'buyer':'测试买方','seller':'测试商户','amount_cents':12345,'invoice_date':'2026-11-01','invoice_number':number},''
    monkeypatch.setattr(service,'recognize',recognize)
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'retry-link@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_account(account)==2
    assert len(client.get('/api/invoices').json()) == 1
    emails=client.get('/api/emails',params={'account_id':account}).json()
    for message in emails:
        response=client.post(f"/api/emails/{message['id']}/reprocess")
        assert response.status_code==200
        assert response.json()['created'] == (1 if message['invoice_count'] == 0 else 0)
    assert len(client.get('/api/invoices').json())==2


def test_reprocess_hides_mailbox_errors(isolated_storage,monkeypatch):
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'retry-error@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_id=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status,folder) VALUES(?,?,?,?,?,?,?,?,?)',(account,'INBOX\x001','1','','测试邮件','','','普通邮件','INBOX')).lastrowid
    class FailedMailbox:
        def __init__(self,*args,**kwargs): pass
        def login(self,address,password): raise RuntimeError('private upstream detail')
        def logout(self): pass
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',FailedMailbox)
    response=client.post(f'/api/emails/{email_id}/reprocess')
    assert response.status_code==502 and 'private' not in response.text
    assert not mail.account_lock(account).locked()


def test_reprocess_reports_busy_mailbox_without_waiting(isolated_storage,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'busy-test@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_id=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,status,folder) VALUES(?,?,?,?,?,?,?)',(account,'INBOX\x001','1','','测试邮件','普通邮件','INBOX')).lastrowid
    def unexpected_connection(*args,**kwargs):
        raise AssertionError('A busy mailbox must not start another connection')
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',unexpected_connection)
    lock=mail.account_lock(account)
    pool=ThreadPoolExecutor(max_workers=1)
    lock.acquire()
    try:
        future=pool.submit(client.post,f'/api/emails/{email_id}/reprocess')
        response=future.result(timeout=2)
        assert response.status_code==409 and '正在同步' in response.json()['detail']
    finally:
        lock.release()
        pool.shutdown(wait=True)


def test_email_invoice_labels_report_mixed_results(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'labels-test@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_id=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status,folder) VALUES(?,?,?,?,?,?,?,?,?)',(account,'INBOX\x001','1','','混合测试','','','识别失败','INBOX')).lastrowid
        conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status,folder) VALUES(?,?,?,?,?,?,?,?,?)',(account,'INBOX\x001','2','','普通邮件','','','普通邮件','INBOX'))
    ids=[service.import_file(f'{n}.xml',invoice_xml(f'{n}8123456789012345678'),'邮件',email_id)[0] for n in (1,2,3)]
    with db.connect() as conn:
        conn.execute("UPDATE invoices SET recognition_status='识别失败',failure_reason='测试失败' WHERE id=?",(ids[1],))
        conn.execute("UPDATE invoices SET recognition_status='识别失败',duplicate_state='confirmed' WHERE id=?",(ids[2],))
    listed=client.get('/api/emails',params={'account_id':account}).json()
    mixed=next(item for item in listed if item['id']==email_id)
    assert (mixed['invoice_count'],mixed['recognized_count'],mixed['failed_count'],mixed['duplicate_count'])==(3,1,1,1)
    detail=client.get(f'/api/emails/{email_id}').json()
    assert all(detail[key]==mixed[key] for key in ('invoice_count','recognized_count','failed_count','duplicate_count'))
    ordinary=next(item for item in listed if item['subject']=='普通邮件')
    assert ordinary['invoice_count']==0 and ordinary['recognized_count']==0 and ordinary['status']=='普通邮件'


def test_invoice_update_keeps_pending_mail_link_status(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'pending-test@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_id=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,received_at,body,status,folder) VALUES(?,?,?,?,?,?,?,?,?)',(account,'INBOX\x001','1','','测试邮件','','','发票链接待处理','INBOX')).lastrowid
    ident=service.import_file('invoice.xml',invoice_xml('78123456789012345678'),'邮件',email_id)[0]
    value={'buyer':'测试买方','seller':'测试商户','amount_cents':12345,'invoice_date':'2026-11-01','invoice_number':'78123456789012345678'}
    assert client.patch(f'/api/invoices/{ident}',json=value).status_code==200
    assert client.get(f'/api/emails/{email_id}').json()['status']=='发票链接待处理'


def test_interrupted_invoice_delete_recovers_originals(isolated_storage):
    import app.deletion as deletion
    config,data,client,db,service,backup=isolated_storage
    ident=client.post('/api/invoices/upload',files=[('files',('invoice.xml',invoice_xml('12345678901234567890'),'application/xml'))]).json()[0]['id']
    with db.connect() as conn:
        name=conn.execute('SELECT stored_name FROM invoices WHERE id=?',(ident,)).fetchone()['stored_name']
    original=data/'files'/name
    staged=data/'files'/(deletion.PREFIX+name)
    os.replace(original,staged)
    deletion.recover_deletions()
    assert original.exists() and not staged.exists()
    os.replace(original,staged)
    with db.connect() as conn: conn.execute('DELETE FROM invoices WHERE id=?',(ident,))
    deletion.recover_deletions()
    assert not staged.exists()


def test_ai_connection_and_visual_sample_use_unsaved_settings_safely(isolated_storage):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    config,data,client,db,service,backup=isolated_storage
    requests=[]
    expected={'is_invoice':True,'buyer':'测试购买方','seller':'测试商户','amount':'123.45','invoice_date':'2026-01-15','invoice_number':'20123456789012345678'}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path,self.headers.get('Authorization'),payload))
            parts=payload['messages'][0]['content']
            answer=json.dumps(expected,ensure_ascii=False) if any(part['type']=='image_url' for part in parts) else 'OK'
            response=json.dumps({'choices':[{'message':{'content':answer}}]},ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response)));self.end_headers()
            self.wfile.write(response)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    base=f'http://127.0.0.1:{server.server_port}/v1'
    saved={'base_url':base,'model':'saved-model','api_key':'YOUR_API_KEY_HERE','auto_enabled':False}
    try:
        assert client.put('/api/ai-settings',json=saved).status_code==200
        draft={'base_url':base,'model':'draft-model','api_key':'','auto_enabled':False}
        connection=client.post('/api/ai-settings/test-connection',json=draft)
        assert connection.status_code==200 and connection.json()['ok']
        sample=client.post('/api/ai-settings/test-recognition',json=draft)
        assert sample.status_code==200 and len(sample.json()['matched'])==5
        assert all(item[1]=='Bearer YOUR_API_KEY_HERE' for item in requests)
        assert requests[0][0]=='/v1/chat/completions' and requests[0][2]['model']=='draft-model'
        assert any(part['type']=='image_url' for part in requests[1][2]['messages'][0]['content'])
        assert 'YOUR_API_KEY_HERE' not in sample.text
        assert client.get('/api/ai-settings').json()['model']=='saved-model'
        draft['base_url']=base+'/other'
        assert client.post('/api/ai-settings/test-connection',json=draft).status_code==200
        assert requests[-1][1] is None
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)


def test_backup_and_restore_all_data_and_credentials(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    original=invoice_xml('78123456789012345678')
    ident=client.post('/api/invoices/upload',files=[('files',('invoice.xml',original,'application/xml'))]).json()[0]['id']
    assert client.post('/api/accounts',json={'name':'示例邮箱','email':'backup-test@qq.com','auth_code':'fake-authorization'}).status_code==200
    assert client.put('/api/ai-settings',json={'base_url':'https://example.test/v1','model':'sample-model','api_key':'fake-api-key','auto_enabled':False}).status_code==200
    assert client.put('/api/settings',json={'fields':['金额','商户']}).status_code==200
    (config/'user-config.txt').write_text('saved config')
    (data/'additional.txt').write_text('saved data')
    exported=client.get('/api/backup/export')
    assert exported.status_code==200 and exported.content.startswith(b'PK')
    contents=exported.content
    assert backup.inspect_backup(io.BytesIO(contents))['counts']['invoices']==1
    checked=client.post('/api/backup/inspect',files={'file':('backup.zip',contents,'application/zip')})
    assert checked.status_code==200,checked.text
    assert checked.json()['counts']['invoices']==1 and checked.json()['counts']['accounts']==1
    client.post('/api/invoices/upload',files=[('files',('new.xml',invoice_xml('78123456789012345679'),'application/xml'))])
    client.put('/api/settings',json={'fields':['抬头']})
    (config/'user-config.txt').write_text('changed')
    (data/'additional.txt').write_text('changed')
    restored=client.post('/api/backup/restore',files={'file':('backup.zip',contents,'application/zip')})
    assert restored.status_code==200 and restored.json()['counts']['invoices']==1
    assert client.get('/api/invoices').status_code==401
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==200
    assert len(client.get('/api/invoices').json())==1
    assert client.get(f'/api/invoices/{ident}/original').content==original
    assert client.get('/api/settings').json()['fields']==['金额','商户']
    assert client.get('/api/ai-settings').json()['has_key']
    assert len(client.get('/api/accounts').json())==1
    assert (config/'user-config.txt').read_text()=='saved config'
    assert (data/'additional.txt').read_text()=='saved data'
    with db.connect() as conn:
        encrypted=conn.execute('SELECT secret FROM accounts').fetchone()['secret']
    assert service.cipher().decrypt(encrypted.encode()).decode()=='fake-authorization'
    recovery=list((config/'.piaoxu-recovery').glob('*.zip'))
    assert len(recovery)==1 and backup.inspect_backup(io.BytesIO(recovery[0].read_bytes()))['counts']['invoices']==2


def test_invalid_backup_does_not_change_existing_storage(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    data_file=data/'keep.txt';data_file.write_text('unchanged')
    exported=client.get('/api/backup/export').content
    bad=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(exported)) as src,zipfile.ZipFile(bad,'w') as dst:
        for info in src.infolist():
            content=src.read(info)
            if info.filename=='data/keep.txt': content=b'tampered'
            dst.writestr(info.filename,content)
    invalid=bad.getvalue()
    assert client.post('/api/backup/inspect',files={'file':('bad.zip',invalid,'application/zip')}).status_code==422
    assert client.post('/api/backup/restore',files={'file':('bad.zip',invalid,'application/zip')}).status_code==422
    assert data_file.read_text()=='unchanged'
    assert not (config/'.piaoxu-recovery').exists()
    traversal=io.BytesIO()
    with zipfile.ZipFile(traversal,'w') as archive:
        archive.writestr('../outside','bad')
        archive.writestr('manifest.json',json.dumps({'format':'piaoxu-backup','version':1,'entries':[{'path':'../outside','size':3,'sha256':'0'*64}]}))
    assert client.post('/api/backup/inspect',files={'file':('bad.zip',traversal.getvalue(),'application/zip')}).status_code==422
    assert not (data.parent/'outside').exists()


def test_interrupted_restore_is_rolled_back(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    (data/'keep.txt').write_text('backup version')
    snapshot=client.get('/api/backup/export').content
    (data/'keep.txt').write_text('current version')
    real_replace=os.replace
    interrupted={'done':False}
    def crash_during_install(src,dst):
        src=os.fspath(src)
        if '/.piaoxu-restore/incoming/' in src and not interrupted['done']:
            interrupted['done']=True
            raise SystemExit('simulated crash')
        return real_replace(src,dst)
    monkeypatch.setattr(backup.os,'replace',crash_during_install)
    with pytest.raises(SystemExit): backup.restore_backup(io.BytesIO(snapshot))
    monkeypatch.setattr(backup.os,'replace',real_replace)
    assert (config/'.piaoxu-restore.json').exists()
    backup.recover_interrupted_restore()
    assert (data/'keep.txt').read_text()=='current version'
    assert (config/'piaoxu.sqlite3').is_file()
    assert not (config/'.piaoxu-restore.json').exists()


def test_backup_lock_waits_for_active_operations(isolated_storage):
    import app.storage_lock as locking
    async def scenario():
        entered=asyncio.Event()
        async def exclusive():
            async with locking.storage_lock(exclusive=True): entered.set()
        async with locking.storage_lock():
            task=asyncio.create_task(exclusive())
            await asyncio.sleep(0.12)
            assert not entered.is_set()
        await asyncio.wait_for(task,2)
        assert entered.is_set()
    asyncio.run(scenario())


def test_local_duplicate_and_missing_number_require_review(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    from concurrent.futures import ThreadPoolExecutor
    fields={'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'CONCURRENT-LOCAL'}
    monkeypatch.setattr(service,'recognize',lambda path:(dict(fields),''))
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids=list(pool.map(lambda content:service.import_file('invoice.pdf',content,'本地上传')[0],(b'%PDF-a',b'%PDF-b')))
    invoices={item['id']:item for item in client.get('/api/invoices').json()}
    assert sorted(invoices[i]['duplicate_state'] for i in ids)==['','pending']
    assert sorted(invoices[i]['recognition_status'] for i in ids)==['已识别','识别失败']
    monkeypatch.setattr(service,'recognize',lambda path:({**fields,'invoice_number':''},''))
    item=client.post('/api/invoices/upload',files={'files':('missing.pdf',b'%PDF-no-number','application/pdf')}).json()[0]
    missing=next(i for i in client.get('/api/invoices').json() if i['id']==item['id'])
    assert missing['duplicate_state']=='unverified' and missing['recognition_status']=='识别失败'
    assert client.post('/api/groups',json={'name':'待核对','ids':[item['id']]}).status_code==409


def test_whitespace_fields_and_failed_import_cleanup(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    fields={'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'WHITESPACE-1'}
    monkeypatch.setattr(service,'recognize',lambda path:(dict(fields),''))
    ident=client.post('/api/invoices/upload',files={'files':('one.pdf',b'%PDF-one','application/pdf')}).json()[0]['id']
    response=client.patch(f'/api/invoices/{ident}',json={**fields,'buyer':'  ','seller':'\t'})
    assert response.status_code==200
    invoice=next(i for i in client.get('/api/invoices').json() if i['id']==ident)
    assert invoice['recognition_status']=='识别失败'
    before=set((data/'files').iterdir())
    def fail(path): raise RuntimeError('synthetic failure')
    monkeypatch.setattr(service,'recognize',fail)
    response=client.post('/api/invoices/upload',files={'files':('bad.pdf',b'%PDF-exception','application/pdf')})
    assert response.json()[0].get('error') and set((data/'files').iterdir())==before


def test_failed_mail_uid_does_not_block_newer_and_is_retried(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'mock','email':'mock-sync@qq.com','auth_code':'fake'}).json()['id']
    message=EmailMessage();message['Subject']='Synthetic';message.set_content('test')
    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'777'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'1 2'])
            return ('OK',[(b'data',message.as_bytes())])
    original=mail.process_message
    failed=[]
    def process(*args,**kwargs):
        if args[2]=='1' and not failed:
            failed.append(True);raise RuntimeError('synthetic temporary error')
        return original(*args,**kwargs)
    monkeypatch.setattr(mail,'process_message',process)
    with pytest.raises(RuntimeError,match='部分邮件同步失败'): mail.sync_folder(Mailbox(),account,b'INBOX','INBOX')
    with db.connect() as conn: assert [r['uid'] for r in conn.execute('SELECT uid FROM emails WHERE account_id=?',(account,))]==['2']
    assert mail.sync_folder(Mailbox(),account,b'INBOX','INBOX')==1
    with db.connect() as conn: assert {r['uid'] for r in conn.execute('SELECT uid FROM emails WHERE account_id=?',(account,))}=={'1','2'}
    with db.connect() as conn: assert conn.execute('SELECT count(*) FROM email_sync_failures').fetchone()[0]==0


def test_invoice_and_email_pagination_filters(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'mock','email':'mock-paging@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        for i in range(55):
            conn.execute("INSERT INTO invoices(sha256,original_name,stored_name,source,imported_at,buyer,seller,amount_cents,invoice_date,invoice_number,recognition_status,reimbursed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(f'hash-{i}',f'{i}.pdf',f'{i}.pdf','本地上传','2026-01-01','买方',f'商户{i}',100,'2026-01-01',f'NUMBER-{i}','已识别',i%2))
            conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,status) VALUES(?,?,?,?,?,?)",(account,'INBOX',str(i),'sender',f'subject {i}','普通邮件'))
    summary=client.get('/api/summary').json()
    assert summary['invoices']==55 and summary['unreimbursed']==28 and summary['unreimbursed_amount_cents']==2800
    first=client.get('/api/invoices',params={'limit':50}).json();second=client.get('/api/invoices',params={'limit':50,'offset':50}).json()
    assert first['total']==second['total']==55 and len(first['items'])==50 and len(second['items'])==5
    assert not {x['id'] for x in first['items']} & {x['id'] for x in second['items']}
    all_ids={x['id'] for x in first['items']+second['items']}
    assert set(first['import_day_ids']['2026-01-01'])==all_ids
    assert set(second['import_day_ids']['2026-01-01'])==all_ids
    assert client.get('/api/invoices',params={'limit':50,'reimbursement':'reimbursed'}).json()['total']==27
    paid=client.get('/api/invoices',params={'limit':50,'reimbursement':'reimbursed'}).json()
    assert set(paid['import_day_ids']['2026-01-01'])=={x['id'] for x in paid['items']}
    searched=client.get('/api/invoices',params={'limit':50,'query':'商户42'}).json()
    assert searched['import_day_ids']['2026-01-01']==[searched['items'][0]['id']]
    assert client.get('/api/invoices',params={'limit':50,'query':'商户42'}).json()['total']==1
    assert client.get('/api/invoices',params={'limit':50,'query':'%'}).json()['total']==0
    messages=client.get('/api/emails',params={'limit':50,'offset':50,'account_id':account,'invoice_status':'none'}).json()
    assert messages['total']==55 and len(messages['items'])==5


def test_invoice_date_selection_and_amount_sort_cover_all_pages(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    with db.connect() as conn:
        for i in range(55):
            day='2026-11-09' if i%2==0 else '2026-11-10'
            conn.execute("INSERT INTO invoices(sha256,original_name,stored_name,source,imported_at,seller,amount_cents,invoice_date,recognition_status,reimbursed) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f'sort-{i}',f'{i}.pdf',f'{i}.pdf','本地上传',day+('T00:00:00' if i==0 else 'T23:59:59'),'日期批量商户',None if i==54 else (54-i)*100,'2026-10-01','已识别',i%2))
    for sort,reverse in [('amount_asc',False),('amount_desc',True)]:
        pages=[client.get('/api/invoices',params={'limit':20,'offset':offset,'sort':sort}).json() for offset in (0,20,40)]
        rows=[row for page in pages for row in page['items']]
        assert len(rows)==len({row['id'] for row in rows})==55
        assert all(page['total']==55 for page in pages)
        amounts=[row['amount_cents'] for row in rows]
        assert amounts[-1] is None
        assert amounts[:-1]==sorted(amounts[:-1],reverse=reverse)
    params={'limit':500,'imported_from':'2026-11-09','imported_to':'2026-11-09','query':'日期批量','reimbursement':'unreimbursed'}
    selected=client.get('/api/invoices',params=params).json()
    assert selected['total']==len(selected['items'])==28
    assert all(row['imported_at'].startswith('2026-11-09') and row['invoice_date']=='2026-10-01' for row in selected['items'])
    assert client.get('/api/invoices',params={**params,'imported_to':'2026-11-10','reimbursement':'all'}).json()['total']==55
    assert client.get('/api/invoices',params={**params,'query':'不存在'}).json()['total']==0
    assert client.get('/api/invoices',params={'imported_from':'2026-11-10','imported_to':'2026-11-09'}).status_code==422
    assert client.get('/api/invoices',params={'imported_from':'2026-11-31'}).status_code==422
    assert client.get('/api/invoices',params={'sort':'invalid'}).status_code==422


def test_email_filters_cover_visible_invoice_states(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    account=client.post('/api/accounts',json={'name':'状态测试邮箱','email':'filter-test@qq.com','auth_code':'fake'}).json()['id']
    other=client.post('/api/accounts',json={'name':'其他邮箱','email':'other-filter@qq.com','auth_code':'fake'}).json()['id']
    cases={
        'none':('普通邮件',[]),
        'deleted':('发票已删除',[]),
        'recognized':('已识别',[('已识别','')]),
        'failed':('识别失败',[('识别失败','')]),
        'duplicate':('已识别',[('识别失败','confirmed')]),
        'pending_invoice':('待处理',[('待处理','')]),
        'pending_mail':('待处理',[]),
        'mail_error':('导入失败',[]),
        'empty_failed':('识别失败',[]),
        'link_pending':('发票链接待处理',[]),
        'mixed':('识别失败',[('已识别',''),('识别失败','')]),
        'partial_error':('导入失败',[('已识别','')]),
        'partial_link':('发票链接待处理',[('已识别','')]),
    }
    ids={}
    with db.connect() as conn:
        for name,(status,invoices) in cases.items():
            ident=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,status) VALUES(?,?,?,?,?,?)',(account,'FILTER',name,'测试发件人',name,status)).lastrowid
            ids[name]=ident
            for index,(recognition,duplicate) in enumerate(invoices):
                key=f'filter-{name}-{index}'
                invoice=conn.execute('INSERT INTO invoices(sha256,original_name,stored_name,source,email_id,imported_at,recognition_status,duplicate_state) VALUES(?,?,?,?,?,?,?,?)',(key,key+'.pdf',key+'.pdf','邮件',ident,'2026-11-03',recognition,duplicate)).lastrowid
                conn.execute('INSERT INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',(invoice,ident))
        other_deleted=conn.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,status) VALUES(?,?,?,?,?,?)',(other,'FILTER','deleted','测试发件人','其他邮箱的已删除发票','发票已删除')).lastrowid
    expected={
        'all':set(cases),
        'invoice':{'recognized','failed','duplicate','pending_invoice','mixed','partial_error','partial_link'},
        'has':{'recognized','duplicate','mixed','partial_error','partial_link'},
        'suspected':{'failed','pending_invoice'},
        'none':{'none'},
        'recognized':{'recognized','mixed','partial_error','partial_link'},
        'failed':{'failed','mixed'},
        'deleted':{'deleted'},
        'duplicate':{'duplicate'},
        'pending':{'pending_invoice','pending_mail'},
        'processing_failed':{'mail_error','empty_failed','partial_error'},
        'link_pending':{'link_pending','partial_link'},
    }
    for status,names in expected.items():
        response=client.get('/api/emails',params={'invoice_status':status,'account_id':account,'limit':50})
        assert response.status_code==200,(status,response.text)
        result=response.json()
        assert result['total']==len(names),status
        assert {m['id'] for m in result['items']}=={ids[name] for name in names},status
    for offset in (0,1):
        result=client.get('/api/emails',params={'invoice_status':'deleted','limit':1,'offset':offset}).json()
        assert result['total']==2 and result['items'][0]['id']==[other_deleted,ids['deleted']][offset]
    assert client.get('/api/emails',params={'invoice_status':'unknown'}).status_code==422


def test_external_image_rejects_private_destinations_and_redirects(monkeypatch):
    import socket
    import app.download as download
    monkeypatch.setattr(download.socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError,match='非公网'): download.public_socket('mock.example',80,1)
    class Redirect:
        status=302
        def getheader(self,name): return 'http://127.0.0.1/private'
    class Connection:
        def __init__(self,host,*args,**kwargs): self.host=host
        def request(self,*args,**kwargs):
            if self.host=='127.0.0.1': download.public_socket(self.host,80,1)
        def getresponse(self): return Redirect()
        def close(self): pass
    monkeypatch.setattr(download,'PublicHTTP',Connection)
    with pytest.raises(ValueError,match='非公网'): download.public_image('http://public.example/image')
    with pytest.raises(ValueError): download.public_image('http://public.example:8080/image')


def test_invoice_images_with_body_hint_and_zip_are_detected(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    from PIL import Image
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    monkeypatch.setattr(service,'recognize',lambda path:({'buyer':'买方','seller':'卖方','amount_cents':1,'invoice_date':'2026-01-01','invoice_number':path.name},''))
    image=io.BytesIO();Image.new('RGB',(2,2),'white').save(image,'PNG')
    zipped=io.BytesIO()
    with zipfile.ZipFile(zipped,'w') as archive: archive.writestr('scan.png',image.getvalue()+b'zip')
    message=EmailMessage();message['Subject']='资料';message.set_content('请查收本次发票')
    message.add_attachment(image.getvalue(),maintype='image',subtype='png',filename='photo.png')
    message.add_attachment(zipped.getvalue(),maintype='application',subtype='zip',filename='docs.zip')
    class Box:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'778'])
        def uid(self,cmd,*args): return ('OK',[b'1']) if cmd=='search' else ('OK',[(b'data',message.as_bytes())])
    account=client.post('/api/accounts',json={'name':'mock','email':'photo-mock@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_folder(Box(),account,b'INBOX','INBOX')==1
    messages=client.get('/api/emails',params={'account_id':account}).json()
    assert messages[0]['invoice_count']==2


def test_later_pdf_pages_are_considered_for_text_and_ocr(monkeypatch):
    import fitz
    import pytesseract
    import app.recognition as recognition
    from PIL import Image
    document=fitz.open()
    for i in range(4):
        page=document.new_page(width=200,height=200)
        if i==3: page.insert_text((10,30),'page-four-invoice')
    original=recognition.parse_invoice
    expected={'buyer':'A','seller':'B','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'P4'}
    monkeypatch.setattr(recognition,'parse_invoice',lambda text:(expected,'') if 'page-four-invoice' in text else ({},'missing'))
    fields,reason=recognition.recognize_pdf(document)
    assert fields==expected and reason==''
    count=[0]
    def fake_ocr(*args,**kwargs):
        count[0]+=1
        return 'page-four-invoice' if count[0]%4==0 else ''
    monkeypatch.setattr(pytesseract,'image_to_string',fake_ocr)
    images=[Image.new('RGB',(2,2)) for _ in range(4)]
    fields,reason=recognition._recognize_images(images)
    assert fields==expected and 'OCR' in reason and count[0]>=4
    document.close()


def test_stale_delete_identity_cannot_remove_reused_id(isolated_storage, monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    first=client.post('/api/invoices/upload',files={'files':('a.xml',invoice_xml('111'),'application/xml')}).json()[0]['id']
    stale=delete_payload([first])
    assert client.post('/api/invoices/delete',json=stale).status_code == 200
    second=client.post('/api/invoices/upload',files={'files':('b.xml',invoice_xml('222'),'application/xml')}).json()[0]['id']
    assert first == second
    assert client.post('/api/invoices/delete',json=stale).status_code == 409
    assert client.get(f'/api/invoices/{second}/original').status_code == 200


def test_restore_valid_backup_when_current_original_missing(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    ident=client.post('/api/invoices/upload',files={'files':('a.xml',invoice_xml('111'),'application/xml')}).json()[0]['id']
    snapshot=client.get('/api/backup/export').content
    with db.connect() as conn:
        name=conn.execute('SELECT stored_name FROM invoices WHERE id=?',(ident,)).fetchone()['stored_name']
    (data/'files'/name).unlink()
    response=client.post('/api/backup/restore',files={'file':('backup.zip',snapshot,'application/zip')})
    assert response.status_code == 200 and response.json()['recovery_incomplete']
    assert (data/'files'/name).is_file()
    assert list((config/'.piaoxu-recovery').glob('*-raw.zip'))


def test_edit_invoice_number_rechecks_linked_duplicates(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    fields={'buyer':'A','seller':'B','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'101'}
    monkeypatch.setattr(service,'recognize',lambda p:(fields.copy(),''))
    ids=[client.post('/api/invoices/upload',files={'files':(name+'.pdf',b'%PDF-1.7\n'+name.encode()+b'\n%%EOF','application/pdf')}).json()[0]['id'] for name in ('a','b')]
    assert client.patch(f'/api/invoices/{ids[1]}/duplicate',json={'decision':'confirm'}).status_code==200
    assert client.patch(f'/api/invoices/{ids[0]}',json={**fields,'invoice_number':'202'}).status_code==200
    with db.connect() as conn:
        row=conn.execute('SELECT duplicate_of,duplicate_state FROM invoices WHERE id=?',(ids[1],)).fetchone()
    assert row['duplicate_of'] is None and row['duplicate_state']==''


def test_unlabelled_gb18030_mail_body():
    from email.message import EmailMessage
    from app.mail import text_part
    part=EmailMessage();part.set_type('text/plain');part.set_payload('电子发票'.encode('gb18030'))
    assert text_part(part)=='电子发票'


def test_preview_recognition_is_reused_but_duplicates_rechecked(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    import app.main as main
    calls=[]
    def recognize(path):
        calls.append(1)
        return {'buyer':'A','seller':'B','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'101'},''
    monkeypatch.setattr(main,'recognize',recognize)
    monkeypatch.setattr(service,'recognize',recognize)
    content=b'%PDF-1.7\npreview-cache\n%%EOF'
    assert client.post('/api/invoices/inspect',files={'files':('a.pdf',content,'application/pdf')}).status_code==200
    assert client.post('/api/invoices/upload',files={'files':('a.pdf',content,'application/pdf')}).json()[0]['created']
    assert len(calls)==1


def test_english_invoice_subject_enables_direct_link(isolated_storage, monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    message=EmailMessage();message['Subject']='Your invoice';message.set_content('Download: https://example.test/doc.pdf')
    class Mail:
        def uid(self,*args): return 'OK',[(None,message.as_bytes())]
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
    monkeypatch.setattr(mail,'download',lambda url:('invoice.xml',invoice_xml('12345678901234567890')))
    result=mail.process_message(Mail(),account,'1','test','INBOX')
    assert result['links_found']==1 and len(result['created_ids'])==1


def test_mail_multiple_formats_preserve_originals_and_backup(isolated_storage, monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    fields={'buyer':'购买方','seller':'销售方','amount_cents':12345,'invoice_date':'2026-10-15','invoice_number':'12345678901234567890'}
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    pdf=b'%PDF-1.7\nexample\n%%EOF';xml=invoice_xml(fields['invoice_number'])
    ofd_buffer=io.BytesIO()
    with zipfile.ZipFile(ofd_buffer,'w') as z: z.writestr('OFD.xml','<OFD/>')
    ofd=ofd_buffer.getvalue()
    message=EmailMessage();message['Subject']='电子发票';message.set_content('附件')
    for name,body,kind in [('invoice.xml',xml,'xml'),('invoice.ofd',ofd,'octet-stream'),('invoice.pdf',pdf,'pdf')]:
        message.add_attachment(body,maintype='application',subtype=kind,filename=name)
    class Mail:
        def uid(self,*args): return 'OK',[(None,message.as_bytes())]
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
    result=mail.process_message(Mail(),account,'1','valid','INBOX')
    assert result['status']=='已识别' and len(result['created_ids'])==1
    rows=client.get('/api/invoices').json();assert len(rows)==1
    invoice=rows[0];assert invoice['original_name']=='invoice.pdf' and len(invoice['other_originals'])==2
    for item in invoice['other_originals']:
        content=client.get(f"/api/invoices/{invoice['id']}/originals/{item['id']}").content
        assert content=={'invoice.xml':xml,'invoice.ofd':ofd}[item['name']]
    for pdf_only,expected in [(False,{pdf,xml,ofd}),(True,{pdf})]:
        response=client.post('/api/invoices/export',json={'ids':[invoice['id']],'pdf_only':pdf_only})
        with zipfile.ZipFile(io.BytesIO(response.content)) as z: assert {z.read(name) for name in z.namelist()}==expected
    replay=mail.process_message(Mail(),account,'1','valid','INBOX',(result['email_id'],'已识别'),force=True)
    assert not replay['created_ids'] and replay['status']=='已识别'
    # Backup validates and includes every original; deleting the invoice removes them all.
    with db.connect() as conn: conn.execute('DELETE FROM accounts WHERE id=?',(account,))
    snapshot=io.BytesIO();backup.create_backup(snapshot)
    with zipfile.ZipFile(snapshot) as z:
        assert all(any(z.read(name)==body for name in z.namelist() if name.startswith('data/files/') and not name.endswith('/')) for body in (pdf,xml,ofd))
    assert client.post('/api/invoices/delete',json=delete_payload([invoice['id']])).status_code==200
    assert not list((data/'files').iterdir())


def test_different_invoice_details_do_not_merge_mail_attachments(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
        email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject) VALUES(?,?,?,?,?)",(account,'test','1','','发票')).lastrowid
    def recognize(path):
        return {'buyer':'购买方','seller':'销售方','amount_cents':100 if path.suffix=='.pdf' else 200,'invoice_date':'2026-10-15','invoice_number':'12345678901234567890'},''
    monkeypatch.setattr(service,'recognize',recognize)
    first=service.import_file('a.pdf',b'%PDF-1.7\na\n%%EOF','邮件',email_id)[0]
    second=service.import_file('a.xml',invoice_xml('12345678901234567890'),'邮件',email_id)[0]
    assert first!=second
    with db.connect() as conn:
        assert conn.execute('SELECT duplicate_state FROM invoices WHERE id=?',(second,)).fetchone()[0]=='pending'
        assert conn.execute('SELECT count(*) FROM invoice_originals').fetchone()[0]==0


def test_reprocess_merges_only_untouched_historical_formats(isolated_storage,monkeypatch):
    import app.mail as mail
    config,data,client,db,service,backup=isolated_storage
    fields={'buyer':'购买方','seller':'销售方','amount_cents':123,'invoice_date':'2026-10-15','invoice_number':'12345678901234567890'}
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
        mail_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject) VALUES(?,?,?,?,?)",(account,'test','1','','发票')).lastrowid
    pdf=b'%PDF-1.7\na\n%%EOF';xml=invoice_xml(fields['invoice_number'])
    first=service.import_file('a.pdf',pdf,'邮件')[0]
    second=service.import_file('a.xml',xml,'邮件')[0]
    with db.connect() as conn:
        conn.executemany('INSERT INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',[(first,mail_id),(second,mail_id)])
    assert mail.merge_existing_formats(mail_id)=={second:first}
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM invoices').fetchone()[0]==1
        assert conn.execute('SELECT invoice_id FROM invoice_originals').fetchone()[0]==first
    assert client.post('/api/invoices/inspect',files={'files':('a.xml',xml,'application/xml')}).json()[0]['duplicate']=='existing_file'


def test_partial_ofd_matches_number_and_amount_without_conflicts(isolated_storage,monkeypatch):
    config,data,client,db,service,backup=isolated_storage
    from app.mail import merge_existing_formats
    fields={'buyer':'购买方','seller':'销售方','amount_cents':12345,'invoice_date':'2026-10-15','invoice_number':'12345678901234567890'}
    partial={**fields,'buyer':'','seller':''}
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
        email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject) VALUES(?,?,?,?,?)",(account,'test','1','','发票')).lastrowid
    ofd=io.BytesIO()
    with zipfile.ZipFile(ofd,'w') as archive: archive.writestr('OFD.xml','<OFD/>')
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    original=service.import_file('a.pdf',b'%PDF-1.7\na\n%%EOF','邮件',email_id)[0]
    monkeypatch.setattr(service,'recognize',lambda path:(partial.copy(),'未识别：抬头、销售方'))
    merged,created=service.import_file('a.ofd',ofd.getvalue(),'邮件',email_id)
    assert merged==original and not created
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM invoice_originals').fetchone()[0]==1
    # Existing records are reconciled after reprocessing, with the same constraints.
    second=service.import_file('other.ofd',ofd.getvalue()+b'other','邮件')[0]
    with db.connect() as conn:
        conn.execute('INSERT INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',(second,email_id))
    assert merge_existing_formats(email_id)=={second:original}


def test_partial_original_conflicts_or_uncertainty_do_not_merge():
    from app.service import matching_mail_original
    invoice={'stored_name':'example.pdf','buyer':'A','seller':'B','amount_cents':100,'invoice_date':'2026-10-15','invoice_number':'123'}
    fields={'buyer':'','seller':'B','amount_cents':100,'invoice_date':'2026-10-15','invoice_number':'123'}
    assert matching_mail_original(invoice,fields,'未识别：抬头','.ofd')
    assert not matching_mail_original(invoice,{**fields,'amount_cents':101},'未识别：抬头','.ofd')
    assert not matching_mail_original(invoice,{**fields,'invoice_number':''},'未识别：抬头','.ofd')
    assert not matching_mail_original(invoice,{**fields,'seller':'C'},'未识别：抬头','.ofd')
    assert not matching_mail_original(invoice,fields,'扫描件 OCR 结果请人工核对','.ofd')


def referenced_ofd(fields, missing_reference=False):
    """Synthetic OFD with semantic tags pointing to page text objects."""
    from xml.etree import ElementTree as ET
    document = ET.Element('Document')
    pages = ET.SubElement(document,'Pages')
    ET.SubElement(pages,'Page',ID='61',BaseLoc='Pages/Page_0/Content.xml')
    page = ET.Element('Page')
    tags = ET.Element('root')
    entries = [('InvoiceNo',fields['invoice_number']),('IssueDate',fields['invoice_date']),
               ('BuyerName',fields['buyer']),('SellerName',fields['seller']),
               ('TaxExclusiveTotalAmount','100.00'),('TaxTotalAmount','6.00'),
               ('TaxInclusiveTotalAmount',f"{fields['amount_cents']/100:.2f}")]
    ident = 1
    for name,value in entries:
        tag = ET.SubElement(tags,name)
        for text in (['¥',value] if name.endswith('Amount') else [value]):
            obj = ET.SubElement(page,'TextObject',ID=str(ident))
            ET.SubElement(obj,'TextCode').text = text
            ref = ET.SubElement(tag,'ObjectRef',PageRef='61')
            ref.text = '999' if missing_reference and name=='InvoiceNo' else str(ident)
            ident += 1
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('OFD.xml','<OFD/>')
        archive.writestr('Doc_0/Document.xml',ET.tostring(document))
        archive.writestr('Doc_0/Pages/Page_0/Content.xml',ET.tostring(page))
        archive.writestr('Doc_0/Tags/CustomTag.xml',ET.tostring(tags))
    return output.getvalue()


def test_ofd_semantic_references_and_currency_totals(tmp_path):
    from app.recognition import recognize_ofd
    fields={'invoice_number':'12345678901234567890','buyer':'测试购买方','seller':'测试销售方','invoice_date':'2026-11-07','amount_cents':10600}
    path = tmp_path/'invoice.ofd'
    path.write_bytes(referenced_ofd(fields))
    result,reason = recognize_ofd(path)
    assert not reason
    assert all(result[key]==value for key,value in fields.items())
    path.write_bytes(referenced_ofd(fields,missing_reference=True))
    assert recognize_ofd(path)[1]
    path.write_bytes(referenced_ofd({**fields,'amount_cents':10700}))
    assert '价税合计与金额、税额不一致' in recognize_ofd(path)[1]


def test_old_ai_ofd_is_merged_only_after_native_verification(isolated_storage,monkeypatch):
    from app.mail import merge_existing_formats
    config,data,client,db,service,backup=isolated_storage
    fields={'invoice_number':'12345678901234567890','buyer':'测试购买方','seller':'测试销售方','invoice_date':'2026-11-07','amount_cents':10600}
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
        email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject) VALUES(?,?,?,?,?)",(account,'test','1','','发票')).lastrowid
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    first=service.import_file('a.pdf',b'%PDF-1.7\na\n%%EOF','邮件',email_id)[0]
    second=service.import_file('a.ofd',referenced_ofd(fields),'邮件')[0]
    with db.connect() as conn:
        conn.execute('INSERT INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',(second,email_id))
        conn.execute('UPDATE invoices SET ai_suggestion=?,failure_reason=? WHERE id=?',
                     (json.dumps(fields),'AI 建议已生成，请人工核对；'+service.DUPLICATE_REASON,second))
        conn.execute('UPDATE invoices SET amount_cents=10700 WHERE id=?',(second,))
    assert not merge_existing_formats(email_id)
    with db.connect() as conn: conn.execute('UPDATE invoices SET amount_cents=10600 WHERE id=?',(second,))
    assert merge_existing_formats(email_id)=={second:first}
    rows=client.get('/api/invoices').json()
    assert len(rows)==1 and len(rows[0]['other_originals'])==1


def discounted_invoice_files():
    import fitz
    fields={'buyer':'星辰贸易有限公司','seller':'远航科技有限公司','amount_cents':1690,
            'invoice_date':'2026-11-09','invoice_number':'12345678901234567890'}
    pdf=fitz.open();page=pdf.new_page(width=700,height=350)
    def put(x,y,text): page.insert_text((x,y),text,fontname='china-s',fontsize=10)
    # Separate text objects and shuffled insertion order reproduce the issuer layout.
    put(81,110,fields['buyer']);put(366,110,fields['seller'])
    put(390,55,'开票日期：');put(390,35,'发票号码：')
    put(440,35,fields['invoice_number']);put(440,55,'2026年11月09日')
    put(100,35,'电子发票');put(450,200,'￥16.90');put(30,200,'价税合计（小写）')
    for x,first,second in [(26,'购','买'),(312,'销','售')]:
        put(x,103,first);put(x,113,second);put(x,123,'方')
    for x in [41,326]: put(x,110,'名');put(x+14,110,'称:')
    content=pdf.tobytes();pdf.close()
    xml=f'''<EInvoice><Header><EIid>{fields['invoice_number']}</EIid></Header><EInvoiceData>
      <SellerInformation><SellerName>{fields['seller']}</SellerName></SellerInformation>
      <BuyerInformation><BuyerName>{fields['buyer']}</BuyerName></BuyerInformation>
      <BasicInformation><TotalAmWithoutTax>14.96</TotalAmWithoutTax><TotalTaxAm>1.94</TotalTaxAm>
      <TotalTax-includedAmount>16.90</TotalTax-includedAmount><RequestTime>2026-11-08 19:00:00</RequestTime></BasicInformation>
      <IssuItemInformation><TotaltaxIncludedAmount>21.90</TotaltaxIncludedAmount></IssuItemInformation>
      <IssuItemInformation><TotaltaxIncludedAmount>-5.00</TotaltaxIncludedAmount></IssuItemInformation>
      </EInvoiceData><TaxSupervisionInfo><InvoiceNumber>{fields['invoice_number']}</InvoiceNumber>
      <IssueTime>2026-11-09 08:00:00</IssueTime></TaxSupervisionInfo></EInvoice>'''.encode()
    return fields,content,xml


def test_native_split_label_pdf_and_discount_xml_agree(tmp_path,monkeypatch):
    from app import recognition
    fields,pdf,xml=discounted_invoice_files()
    def no_ocr(*args,**kwargs): pytest.fail('A text invoice should not need OCR')
    monkeypatch.setattr(recognition,'_recognize_images',no_ocr)
    for ext,content in [('pdf',pdf),('xml',xml)]:
        path=tmp_path/('invoice.'+ext);path.write_bytes(content)
        actual,reason=recognition.recognize(path)
        assert not reason
        assert all(actual[key]==value for key,value in fields.items())
    # Item prices must never stand in for a missing invoice-level total.
    path=tmp_path/'missing-total.xml'
    path.write_bytes(xml.replace(b'<TotalTax-includedAmount>16.90</TotalTax-includedAmount>',b''))
    actual,reason=recognition.recognize(path)
    assert actual['amount_cents'] is None and '金额' in reason


def test_mail_reprocess_corrects_old_ai_fields_and_merges_formats(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    from app import mail
    config,data,client,db,service,backup=isolated_storage
    fields,pdf,xml=discounted_invoice_files()
    message=EmailMessage();message['Subject']='电子发票';message.set_content('请查收附件')
    for name,content,kind in [('a.xml',xml,'xml'),('a.pdf',pdf,'pdf')]:
        message.add_attachment(content,maintype='application',subtype=kind,filename=name)
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap({b'1':message.as_bytes()},[],[b'123']))
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'native-test@qq.com','auth_code':'fake'}).json()['id']
    with monkeypatch.context() as old:
        old.setattr(service,'recognize',lambda path:({**fields,'amount_cents':2190 if path.suffix=='.xml' else 1690},'未识别：抬头'))
        old.setattr(service,'assist',lambda path,result,reason:(result,'AI 建议已生成，请人工核对',fields))
        assert mail.sync_account(account)==1
    records=client.get('/api/invoices').json();assert len(records)==2
    assert all(item['recognition_status']=='识别失败' for item in records)
    email_id=records[0]['email_id']
    result=client.post(f'/api/emails/{email_id}/reprocess')
    assert result.status_code==200 and result.json()['status']=='已识别'
    records=client.get('/api/invoices').json();assert len(records)==1
    item=records[0]
    assert all(item[key]==value for key,value in fields.items())
    assert item['original_name']=='a.pdf' and len(item['other_originals'])==1
    assert client.get('/api/summary').json()['failed_invoices']==0
    assert client.post(f'/api/emails/{email_id}/reprocess').json()['existing']==1
    response=client.post('/api/invoices/export',json={'ids':[item['id']]})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {archive.read(name) for name in archive.namelist()}=={pdf,xml}


def test_native_refresh_preserves_human_decisions(isolated_storage,monkeypatch):
    from app.mail import refresh_native_mail_invoices
    config,data,client,db,service,backup=isolated_storage
    fields,pdf,xml=discounted_invoice_files()
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
        email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject) VALUES(?,?,?,?,?)",(account,'test','1','','发票')).lastrowid
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    ids=[]
    for index in range(4):
        content=xml.replace(b'</EInvoice>',f'<Extra>{index}</Extra></EInvoice>'.encode())
        ids.append(service.import_file(f'{index}.xml',content,'邮件')[0])
    with db.connect() as conn:
        for ident in ids:
            conn.execute('INSERT INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',(ident,email_id))
            conn.execute("UPDATE invoices SET ai_suggestion=?,recognition_status='识别失败',failure_reason=?,amount_cents=2190 WHERE id=?",(json.dumps(fields),'AI 建议已生成，请人工核对',ident))
        conn.execute("UPDATE invoices SET duplicate_state='confirmed' WHERE id=?",(ids[0],))
        conn.execute("UPDATE invoices SET failure_reason='未填写：抬头' WHERE id=?",(ids[1],))
        conn.execute('UPDATE invoices SET reimbursed=1 WHERE id=?',(ids[2],))
        group=conn.execute("INSERT INTO groups(name,created_at) VALUES('测试','2026-11-09')").lastrowid
        conn.execute('INSERT INTO group_invoices(group_id,invoice_id) VALUES(?,?)',(group,ids[3]))
    refresh_native_mail_invoices(email_id)
    with db.connect() as conn:
        assert all(row['amount_cents']==2190 for row in conn.execute('SELECT * FROM invoices'))


def merge_payload(client,source,target):
    items={row['id']:row for row in client.get('/api/invoices').json()}
    return {'target_id':target,'sha256':items[source]['sha256'],'target_sha256':items[target]['sha256']}


def test_explicit_merge_keeps_originals_mail_and_reimbursement(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    content=invoice_xml('12345678901234567890')
    target=service.import_file('base.xml',content,'本地上传')[0]
    source_content=content.replace(b'</Invoice>',b'<Extra>copy</Extra></Invoice>')
    source=service.import_file('copy.xml',source_content,'邮件')[0]
    extra=b'%PDF-1.7\nextra\n%%EOF'
    import hashlib
    digest=hashlib.sha256(extra).hexdigest();stored=digest+'.pdf';(data/'files'/stored).write_bytes(extra)
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'merge-test@qq.com','auth_code':'fake'}).json()['id']
    with db.connect() as conn:
        email_ids=[]
        for ident in [target,source]:
            email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,status) VALUES(?,?,?,?,?,?)",(account,'test',str(ident),'','发票','识别失败')).lastrowid
            email_ids.append(email_id)
            conn.execute('INSERT INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',(ident,email_id))
            conn.execute('UPDATE invoices SET email_id=? WHERE id=?',(email_id,ident))
        conn.execute('INSERT INTO invoice_originals(invoice_id,sha256,original_name,stored_name) VALUES(?,?,?,?)',(source,digest,'extra.pdf',stored))
    group=client.post('/api/groups',json={'name':'测试报销组','ids':[target]}).json()['id']
    assert client.patch(f'/api/groups/{group}',json={'reimbursed':True}).status_code==200
    assert client.patch(f'/api/invoices/{source}/duplicate',json={'decision':'confirm'}).status_code==200
    response=client.post(f'/api/invoices/{source}/merge',json=merge_payload(client,source,target))
    assert response.status_code==200 and response.json()['id']==target
    items=client.get('/api/invoices').json();assert len(items)==1
    assert items[0]['reimbursed']==1 and len(items[0]['other_originals'])==2
    assert len(items[0]['mail_sources'])==2 and items[0]['has_local_upload']
    for email_id in email_ids:
        item=client.get(f'/api/emails/{email_id}').json()
        assert item['status']=='已识别' and [row['id'] for row in item['invoices']]==[target]
    response=client.post('/api/invoices/export',json={'ids':[target]})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {archive.read(name) for name in archive.namelist()}=={content,source_content,extra}
    assert client.get('/api/backup/export').status_code==200
    assert client.post('/api/invoices/delete',json=delete_payload([target])).status_code==200
    assert not list((data/'files').iterdir())


def test_merge_rejects_conflicts_stale_identity_and_grouped_source(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    content=invoice_xml('12345678901234567890')
    target=service.import_file('base.xml',content,'本地上传')[0]
    source=service.import_file('copy.xml',content.replace(b'200.35',b'300.35'),'邮件')[0]
    payload=merge_payload(client,source,target)
    assert client.post(f'/api/invoices/{source}/merge',json=payload).status_code==409
    with db.connect() as conn: conn.execute('UPDATE invoices SET amount_cents=20035 WHERE id=?',(source,))
    assert client.post(f'/api/invoices/{source}/merge',json={**payload,'target_sha256':'0'*64}).status_code==409
    assert client.post(f'/api/invoices/{source}/merge',json={**payload,'sha256':'0'*64}).status_code==409
    assert client.patch(f'/api/invoices/{source}/duplicate',json={'decision':'distinct'}).status_code==200
    client.post('/api/groups',json={'name':'独立报销组','ids':[source]})
    assert client.post(f'/api/invoices/{source}/merge',json=payload).status_code==409
    assert len(client.get('/api/invoices').json())==2
    assert len(list((data/'files').iterdir()))==2


def test_merging_pdf_into_xml_uses_normal_pdf_preview_and_multiple_originals(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    fields,pdf,xml=discounted_invoice_files()
    target=service.import_file('first.xml',xml,'本地上传')[0]
    source=service.import_file('later.pdf',pdf,'本地上传')[0]
    result=client.post(f'/api/invoices/{source}/merge',json=merge_payload(client,source,target))
    assert result.status_code==200
    items=client.get('/api/invoices').json();assert len(items)==1
    item=items[0]
    assert item['id']==target and item['recognition_status']=='已识别'
    assert item['original_name']=='later.pdf' and len(item['other_originals'])==1
    assert item['other_originals'][0]['name']=='first.xml'
    assert client.get(f'/api/invoices/{target}/original').content==pdf
    assert client.get(f'/api/invoices/{target}/preview?info=true').json()['pages']==1
    alternate=item['other_originals'][0]['id']
    assert client.get(f'/api/invoices/{target}/originals/{alternate}').content==xml
    for filename,content in [('later.pdf',pdf),('first.xml',xml)]:
        assert service.import_file(filename,content,'本地上传')==(target,False)
    response=client.post('/api/invoices/export',json={'ids':[target]})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {archive.read(name) for name in archive.namelist()}=={pdf,xml}
    assert client.get('/api/summary').json()['invoices']==1
    assert client.get('/api/backup/export').status_code==200


def test_mail_without_attachments_imports_http_pdf_link_once(isolated_storage,monkeypatch):
    from email.message import EmailMessage
    from app import mail
    import app.download as downloader
    _,_,client,db,_,_=isolated_storage
    fields,pdf,_=discounted_invoice_files()
    url='http://files.example/invoice.pdf?Expires=1234567890&AccessKey=TEST_ONLY&Signature=a%2Bb%3D'
    message=EmailMessage();message['Subject']='您的订单电子发票已开具'
    message.set_content('电子发票已开具，请点击邮件中的下载链接')
    message.add_alternative(f'<p>电子发票已开具</p><a href="{url.replace("&","&amp;")}">发票PDF文件下载</a>',subtype='html')
    requests=[]
    class Response:
        status=200
        def getheader(self,key): return 'application/pdf' if key=='Content-Type' else None
        def read(self,n): return pdf[:n]
    class Connection:
        def __init__(self,host,port,**kwargs): assert host=='files.example' and port==443
        def request(self,method,path,headers): requests.append((method,path))
        def getresponse(self): return Response()
        def close(self): pass
    class Mail:
        def uid(self,*args): return 'OK',[(None,message.as_bytes())]
    monkeypatch.setattr(downloader,'PublicHTTPS',Connection)
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
    first=mail.process_message(Mail(),account,'1','test','INBOX')
    assert first['status']=='已识别' and first['links_found']==1 and len(first['created_ids'])==1
    assert requests==[('GET','/invoice.pdf?Expires=1234567890&AccessKey=TEST_ONLY&Signature=a%2Bb%3D')]
    item=client.get('/api/invoices').json()[0]
    assert all(item[key]==value for key,value in fields.items())
    assert client.get(f"/api/invoices/{item['id']}/original").content==pdf
    replay=mail.process_message(Mail(),account,'1','test','INBOX',(first['email_id'],first['status']),force=True)
    assert replay['status']=='已识别' and not replay['created_ids'] and replay['existing_ids']==[item['id']]
    assert len(client.get('/api/invoices').json())==1


@pytest.mark.parametrize('scenario', ['covered','missing_number','declared_more','other_provider','broken_archive'])
def test_crest_mail_download_fallback(isolated_storage,monkeypatch,scenario):
    from email.message import EmailMessage
    from app import mail
    _,_,client,db,_,_=isolated_storage
    fields,pdf,xml=discounted_invoice_files()
    number=fields['invoice_number']
    host='other.example' if scenario=='other_provider' else 'webapp.crestv.com'
    numbers=f'<p>发票号码：{number}</p>'
    if scenario=='missing_number': numbers+='<p>发票号码：88888888888888888888</p>'
    if scenario=='declared_more': numbers+='<p>发票数量共计2张</p>'
    message=EmailMessage();message['Subject']='您收到示例商户开具的发票'
    message.set_content('请查收电子发票')
    message.add_alternative(f'{numbers}<a href="http://{host}/qrscan/staticclick?Type=invmaildesktop&amp;Code=TEST_ONLY">点击下载发票。</a>',subtype='html')
    message.add_attachment(pdf,maintype='application',subtype='pdf',filename=f'{number}.pdf')
    message.add_attachment(xml,maintype='application',subtype='xml',filename=f'{number}.xml')
    if scenario=='broken_archive':
        message.add_attachment(b'broken zip',maintype='application',subtype='zip',filename='broken.zip')
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap({b'1':message.as_bytes()},[],[b'123']))
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'fallback-test@qq.com','auth_code':'fake'}).json()['id']
    client.post('/api/notification-channels',json={'name':'测试渠道','provider':'serverchan','secret':'SCT_test_fallback'})
    downloads=[]
    def download(url):
        downloads.append(url)
        raise ValueError('需要登录或手动下载发票')
    monkeypatch.setattr(mail,'download',download)
    assert mail.sync_account(account)==1
    message_row=client.get('/api/emails').json()[0]
    item=client.get('/api/invoices').json()[0]
    assert message_row['recognized_count']==1 and message_row['failed_count']==0
    assert item['original_name']==f'{number}.pdf' and len(item['other_originals'])==1
    skipped=scenario in ('covered','broken_archive')
    error_reason=[] if scenario=='covered' else ['压缩附件处理失败'] if scenario=='broken_archive' else ['链接下载失败']
    assert message_row['status']==('导入失败' if error_reason else '已识别')
    assert len(downloads)==int(not skipped)
    with db.connect() as conn:
        events=[row['event'] for row in conn.execute('SELECT event FROM notification_deliveries ORDER BY id')]
        assert events==(['received','failed'] if error_reason else ['received'])
        if scenario=='covered':
            # An already affected email should leave the failure list on manual retry.
            conn.execute("UPDATE emails SET status='导入失败' WHERE id=?",(message_row['id'],))
    response=client.post(f"/api/emails/{message_row['id']}/reprocess")
    assert response.status_code==200
    result=response.json()
    assert result['existing']==1 and result['created']==0
    assert result['skipped_links']==int(skipped) and result['error_reasons']==error_reason
    assert result['errors']==len(error_reason) and result['status']==message_row['status']
    assert len(downloads)==2*int(not skipped)
    assert len(client.get('/api/invoices').json())==1
    attention=client.get('/api/emails',params={'attention':True}).json()
    assert bool(attention)==bool(error_reason)
