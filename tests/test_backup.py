import io
import json
import os
import zipfile
import asyncio

import pytest
from fastapi.testclient import TestClient

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
    assert client.post('/api/invoices/delete',json={'ids':[invoice_id]}).json()=={'deleted':1,'cleanup_pending':0}
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
    assert client.post('/api/invoices/delete',json={'ids':[ids[0],9999999]}).status_code==409
    assert all(path.exists() for path in paths)
    real_replace=deletion.os.replace
    def fail_second(src,dst):
        if str(src)==str(paths[1]): raise PermissionError('simulated')
        return real_replace(src,dst)
    monkeypatch.setattr(deletion.os,'replace',fail_second)
    assert client.post('/api/invoices/delete',json={'ids':ids}).status_code==500
    assert len(client.get('/api/invoices').json())==2 and all(path.exists() for path in paths)
    assert not list((data/'files').glob(deletion.PREFIX+'*'))


def test_batch_delete_repairs_multiple_duplicate_references(isolated_storage):
    config,data,client,db,service,backup=isolated_storage
    original=invoice_xml('78123456789012345678')
    base=service.import_file('base.xml',original,'本地上传')[0]
    first=service.import_file('one.xml',original.replace(b'</Invoice>',b'<Extra>one</Extra></Invoice>'),'邮件')[0]
    second=service.import_file('two.xml',original.replace(b'</Invoice>',b'<Extra>two</Extra></Invoice>'),'邮件')[0]
    result=client.post('/api/invoices/delete',json={'ids':[base,first]})
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
    assert client.post('/api/invoices/delete',json={'ids':[ident]}).status_code==200
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
    assert client.post('/api/invoices/delete',json={'ids':[ident]}).status_code==200
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
    assert not client.get('/api/invoices').json()
    emails=client.get('/api/emails',params={'account_id':account}).json()
    for message in emails:
        response=client.post(f"/api/emails/{message['id']}/reprocess")
        assert response.status_code==200 and response.json()['created']==1
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
    first=client.get('/api/invoices',params={'limit':50}).json();second=client.get('/api/invoices',params={'limit':50,'offset':50}).json()
    assert first['total']==second['total']==55 and len(first['items'])==50 and len(second['items'])==5
    assert not {x['id'] for x in first['items']} & {x['id'] for x in second['items']}
    assert client.get('/api/invoices',params={'limit':50,'reimbursement':'reimbursed'}).json()['total']==27
    assert client.get('/api/invoices',params={'limit':50,'query':'商户42'}).json()['total']==1
    assert client.get('/api/invoices',params={'limit':50,'query':'%'}).json()['total']==0
    messages=client.get('/api/emails',params={'limit':50,'offset':50,'account_id':account,'invoice_status':'none'}).json()
    assert messages['total']==55 and len(messages['items'])==5


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
