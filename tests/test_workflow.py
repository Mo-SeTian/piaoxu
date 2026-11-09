import io
import os
import tempfile
import zipfile
from pathlib import Path

os.environ['PIAOXU_DATA'] = tempfile.mkdtemp(prefix='piaoxu-test-')
from fastapi.testclient import TestClient
from app.main import app
from app.recognition import parse_invoice
import app.service as service

client = TestClient(app, headers={'X-Piaoxu-Request': '1'})
assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code == 200

def test_parser_recognizes_fields_and_rejects_missing():
    fields, reason = parse_invoice('电子发票\n购买方信息\n名称：示例购买方\n销售方信息\n名称：示例销售方\n开票日期：2026年10月28日\n价税合计（小写）￥268.00\n发票号码：12345678901234567890')
    assert reason == ''
    assert fields['buyer'] == '示例购买方'
    assert fields['seller'] == '示例销售方'
    assert fields['amount_cents'] == 26800
    assert fields['invoice_date'] == '2026-10-28'
    assert parse_invoice('普通文件')[1]

def test_upload_group_export_preserves_bytes(monkeypatch):
    monkeypatch.setattr(service, 'recognize', lambda p: ({'buyer':'购买方','seller':'销售方','amount_cents':26800,'invoice_date':'2026-10-28','invoice_number':'TEST-1001'}, ''))
    pdf=b'%PDF-1.7\nexample original bytes\n%%EOF'
    response=client.post('/api/invoices/upload',files=[('files',('invoice.pdf',pdf,'application/pdf'))])
    assert response.status_code == 200
    item=response.json()[0]
    assert item['created'] is True
    ident=item['id']
    assert client.get(f'/api/invoices/{ident}/preview').status_code==422
    duplicate=client.post('/api/invoices/upload',files=[('files',('again.pdf',pdf,'application/pdf'))]).json()[0]
    assert duplicate['id']==ident and not duplicate['created']
    assert client.get(f'/api/invoices/{ident}/original').content == pdf
    response=client.post('/api/groups',json={'name':'差旅','ids':[ident]})
    assert response.status_code==200
    group_id=response.json()['id']
    assert client.post('/api/groups',json={'name':'重复','ids':[ident]}).status_code==409
    assert client.patch(f'/api/groups/{group_id}',json={'reimbursed':True}).status_code==200
    assert next(i for i in client.get('/api/invoices').json() if i['id']==ident)['reimbursed']==1
    assert client.patch(f'/api/invoices/{ident}',json={'buyer':'其他'}).status_code==409
    zipped=client.post('/api/invoices/export',json={'ids':[ident]})
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        names=archive.namelist()
        assert len(names)==1 and names[0].endswith('.pdf')
        assert archive.read(names[0])==pdf
    assert client.patch(f'/api/groups/{group_id}',json={'reimbursed':False}).status_code==200
    assert next(i for i in client.get('/api/invoices').json() if i['id']==ident)['reimbursed']==0

def test_reject_non_pdf_and_cross_origin_form():
    result=client.post('/api/invoices/upload',files=[('files',('a.pdf',b'not a pdf','application/pdf'))]).json()[0]
    assert 'error' in result
    assert TestClient(app).post('/api/groups',json={'name':'x','ids':[1]}).status_code==403

def test_batch_upload_reports_each_file_and_continues_after_errors(monkeypatch):
    def recognize(path):
        content=path.read_bytes()
        if b'exception' in content: raise RuntimeError('simulated failure')
        if b'review' in content: return {},'需要人工确认'
        return {'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'TEST-1002'},''
    monkeypatch.setattr(service,'recognize',recognize)
    files=[('files',('ok.pdf',b'%PDF-batch-ok','application/pdf')),
           ('files',('bad.pdf',b'not pdf','application/pdf')),
           ('files',('exception.pdf',b'%PDF-batch-exception','application/pdf')),
           ('files',('review.pdf',b'%PDF-batch-review','application/pdf')),
           ('files',('duplicate.pdf',b'%PDF-batch-ok','application/pdf'))]
    result=client.post('/api/invoices/upload',files=files)
    assert result.status_code==200
    ok,bad,exception,review,duplicate=result.json()
    assert ok['created'] and ok['recognition_status']=='已识别'
    assert bad['error'] and exception['error']=='导入失败，请重试'
    assert review['created'] and review['recognition_status']=='识别失败' and '需要人工确认' in review['failure_reason'] and '未识别发票号码' in review['failure_reason']
    assert not duplicate['created'] and duplicate['id']==ok['id']

def test_import_inspection_detects_existing_and_batch_duplicates_without_importing():
    import hashlib
    existing=invoice_xml().replace(b'88123456789012345678',b'66123456789012345678')
    batch=invoice_xml().replace(b'88123456789012345678',b'77123456789012345678')
    altered_existing=existing.replace(b'200.35',b'201.35')
    altered_batch=batch.replace(b'200.35',b'202.35')
    existing_id=client.post('/api/invoices/upload',files=[('files',('existing.xml',existing,'application/xml'))]).json()[0]['id']
    before=len(client.get('/api/invoices').json())
    files=[('files',('same.xml',existing,'application/xml')),
           ('files',('same-number.xml',altered_existing,'application/xml')),
           ('files',('new.xml',batch,'application/xml')),
           ('files',('batch-copy.xml',batch,'application/xml')),
           ('files',('batch-number.xml',altered_batch,'application/xml')),
           ('files',('invalid.pdf',b'not a pdf','application/pdf'))]
    response=client.post('/api/invoices/inspect',files=files)
    assert response.status_code==200
    same,number,new,copy,batch_number,invalid=response.json()
    assert same['duplicate']=='existing_file' and same['matched_name']=='existing.xml'
    assert number['duplicate']=='existing_invoice' and number['matched_name']=='existing.xml'
    assert new['duplicate']=='' and new['fields']['invoice_number']=='77123456789012345678'
    assert copy['duplicate']=='batch_file' and copy['matched_name']=='new.xml'
    assert new['sha256']==copy['sha256']==hashlib.sha256(batch).hexdigest()
    assert batch_number['duplicate']=='batch_invoice' and batch_number['matched_name']=='new.xml'
    assert invalid['error']
    assert len(client.get('/api/invoices').json())==before
    assert any(i['id']==existing_id for i in client.get('/api/invoices').json())

def test_inspection_includes_a_pdf_thumbnail():
    import base64
    import fitz
    doc=fitz.open();doc.new_page(width=600,height=300)
    content=doc.tobytes();doc.close()
    result=client.post('/api/invoices/inspect',files=[('files',('preview.pdf',content,'application/pdf'))]).json()[0]
    assert base64.b64decode(result['thumbnail']).startswith(b'\xff\xd8')
    assert not result['fields']['invoice_number']

def test_invoice_records_local_and_multiple_mailbox_sources(monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    monkeypatch.setattr(service,'recognize',lambda p: ({'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01'},''))
    local_first=b'%PDF-sources-local-first'
    mail_first=b'%PDF-sources-mail-first'
    local_id=client.post('/api/invoices/upload',files=[('files',('local.pdf',local_first,'application/pdf'))]).json()[0]['id']
    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'378'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'1'])
            message=EmailMessage();message['Subject']='发票';message.set_content('请查收')
            message.add_attachment(local_first,maintype='application',subtype='pdf',filename='local.pdf')
            message.add_attachment(mail_first,maintype='application',subtype='pdf',filename='mail.pdf')
            return ('OK',[(b'1',message.as_bytes())])
    ids=[]
    for name in ('source-one','source-two'):
        account=client.post('/api/accounts',json={'name':name,'email':f'{name}@qq.com','auth_code':'fake'}).json()['id']
        ids.append(account)
        assert mail.sync_folder(Mailbox(),account,b'INBOX','INBOX')==1
    invoices=client.get('/api/invoices').json()
    first=next(i for i in invoices if i['id']==local_id)
    second=next(i for i in invoices if i['original_name']=='mail.pdf')
    assert first['has_local_upload']==1 and second['has_local_upload']==0
    for invoice in (first,second):
        assert {source['account_id'] for source in invoice['mail_sources']}==set(ids)
        assert {source['account_name'] for source in invoice['mail_sources']}=={'source-one','source-two'}
        assert len({source['email_id'] for source in invoice['mail_sources']})==2
    duplicate=client.post('/api/invoices/upload',files=[('files',('again.pdf',mail_first,'application/pdf'))]).json()[0]
    assert duplicate['id']==second['id'] and not duplicate['created']
    updated=next(i for i in client.get('/api/invoices').json() if i['id']==second['id'])
    assert updated['has_local_upload']==1 and len(updated['mail_sources'])==2

def test_existing_invoice_sources_are_migrated_once(tmp_path,monkeypatch):
    import sqlite3
    import app.db as db
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE invoices(id INTEGER PRIMARY KEY,source TEXT NOT NULL,email_id INTEGER)')
        conn.executemany('INSERT INTO invoices(id,source) VALUES(?,?)',[(1,'本地上传'),(2,'邮件')])
    monkeypatch.setattr(db,'DB',path)
    db.init_db()
    with db.connect() as conn:
        assert [row['has_local_upload'] for row in conn.execute('SELECT has_local_upload FROM invoices ORDER BY id')]==[1,0]
        conn.execute('UPDATE invoices SET has_local_upload=1 WHERE id=2')
    db.init_db()
    with db.connect() as conn:
        assert conn.execute('SELECT has_local_upload FROM invoices WHERE id=2').fetchone()[0]==1

def test_multiple_mailboxes_uid_isolation_and_repeat_sync(monkeypatch):
    from email.message import EmailMessage
    from app.mail import sync_account
    import app.mail as mail_module
    monkeypatch.setattr(service, 'recognize', lambda p: ({'buyer':'购买方','seller':'销售方','amount_cents':1000,'invoice_date':'2026-10-28'}, ''))
    class FakeMailbox:
        def __init__(self,*args,**kwargs): pass
        def list(self): return ('OK',[b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"', b'(\\Noselect) "/" "Parent"'])
        def login(self,address,password): self.address=address; return ('OK', [])
        def select(self,*args,**kwargs): assert kwargs.get('readonly') is True; return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'77'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'1'])
            message=EmailMessage()
            message['Subject']='电子发票'
            message['From']='sender@example.test'
            message['Date']='Wed, 28 Oct 2026 10:00:00 +0800'
            message.set_content('发票附件')
            message.add_attachment(b'%PDF-1.7 '+self.address.encode(),maintype='application',subtype='pdf',filename='invoice.pdf')
            return ('OK',[(b'1',message.as_bytes())])
        def logout(self): pass
    monkeypatch.setattr(mail_module.imaplib,'IMAP4_SSL',FakeMailbox)
    ids=[]
    for n in ['one','two']:
        result=client.post('/api/accounts',json={'name':n,'email':f'{n}@qq.com','auth_code':'example-authorization-code'})
        assert result.status_code==200
        ids.append(result.json()['id'])
    for ident in ids:
        assert sync_account(ident)==2
        assert sync_account(ident)==0
    messages=client.get('/api/emails').json()
    assert len([m for m in messages if m['account_id'] in ids])==4
    for ident in ids:
        assert len(client.get('/api/emails',params={'account_id':ident}).json())==2
    assert all('secret' not in item for item in client.get('/api/accounts').json())
    assert all(len(client.get(f"/api/emails/{m['id']}").json()['invoices']) == 1 for m in messages if m['account_id'] in ids)

def test_mail_headers_without_charset_are_decoded_and_saved():
    import email
    import app.mail as mail

    for encoding in ('utf-8', 'gb18030'):
        raw = b'Subject: ' + '测试邮件标题'.encode(encoding) + b'\r\n\r\n'
        assert mail.decode(email.message_from_bytes(raw)['Subject']) == '测试邮件标题'
    assert mail.decode('=?utf-8?b?5Y+R56Wo?=') == '发票'

    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'231'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'1'])
            raw = (b'Subject: ' + '测试邮件标题'.encode() + b'\r\nFrom: '
                   + '测试发件人'.encode() + b' <sender@example.test>\r\n\r\nHello')
            return ('OK',[(b'1',raw)])
    account=client.post('/api/accounts',json={'name':'encoding-test','email':'encoding-test@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_folder(Mailbox(),account,b'INBOX','INBOX')==1
    message=client.get('/api/emails',params={'account_id':account}).json()[0]
    assert message['subject']=='测试邮件标题'
    assert message['sender']=='测试发件人 <sender@example.test>'

def rich_test_message():
    from email.message import EmailMessage
    from PIL import Image
    image=io.BytesIO();Image.new('RGB',(2,2),'red').save(image,format='PNG')
    message=EmailMessage();message['Subject']='排版测试';message['From']='sender@example.test'
    message.set_content('纯文本内容')
    message.add_alternative('''<html><head><meta http-equiv="refresh" content="0;url=https://evil.example"><style>.banner{background:#cee url(cid:logo)}</style></head><body><h1>排版标题</h1><img src="cid:logo" onerror="alert(1)"><img src="https://images.example.test/banner.png"><script>alert(2)</script><iframe src="https://evil.example"></iframe></body></html>''',subtype='html')
    message.get_payload()[-1].add_related(image.getvalue(),maintype='image',subtype='png',cid='<logo>')
    return message.as_bytes()

def test_rich_email_is_preserved_and_dangerous_markup_removed():
    import app.mail as mail
    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'501'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'1'])
            return ('OK',[(b'1',rich_test_message())])
    account=client.post('/api/accounts',json={'name':'rich-test','email':'rich-test@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_folder(Mailbox(),account,b'INBOX','INBOX')==1
    item=client.get('/api/emails',params={'account_id':account}).json()[0]
    detail=client.get(f"/api/emails/{item['id']}").json()
    assert detail['html_checked']==1 and detail['body'].strip()=='纯文本内容'
    assert '<h1>排版标题</h1>' in detail['html_body'] and '<style>' in detail['html_body']
    assert 'data:image/png;base64,' in detail['html_body']
    assert 'images.example.test' not in detail['html_body']
    assert any('images.example.test' in item['url'] for item in detail['remote_images'])
    assert all(value not in detail['html_body'].lower() for value in ('<script','onerror','<iframe','http-equiv'))

def test_rich_email_supports_inline_content_location_and_backgrounds():
    from email.message import EmailMessage
    from PIL import Image
    from app.email_html import prepare_html
    from app.mail import text_part
    image=io.BytesIO();Image.new('RGB',(2,2),'blue').save(image,format='PNG')
    message=EmailMessage();message.set_content('纯文本')
    message.add_alternative('''<body bgcolor="#eeeeee" background="cid:bg" style="padding:10px"><img src="logo.png"><img src="//images.example.test/a.png" srcset="//images.example.test/a.png 1x, https://images.example.test/b.png 2x"><style>.x{background-image:url(//images.example.test/c.png)}</style></body>''',subtype='html')
    message.get_payload()[-1].add_related(image.getvalue(),maintype='application',subtype='octet-stream',cid='<bg>',headers=['Content-Location: logo.png'])
    parts=[text_part(part) for part in message.walk() if part.get_content_type()=='text/html']
    result=prepare_html(message,parts)
    assert result.count('data:image/png;base64,')==2
    assert 'background-image:' in result and 'background-color:#eeeeee' in result and 'padding:10px' in result
    assert 'src="https://images.example.test/a.png"' in result
    assert 'srcset="https://images.example.test/a.png 1x, https://images.example.test/b.png 2x"' in result
    assert 'url(&quot;https://images.example.test/c.png&quot;)' in result or 'url("https://images.example.test/c.png")' in result
    assert '<body' not in result

def test_existing_email_rich_content_is_refetched_read_only(monkeypatch):
    import app.mail as mail
    from app.db import connect
    calls=[]
    class Mailbox:
        def __init__(self,*args,**kwargs): pass
        def login(self,*args): pass
        def list(self): return ('OK',[b'(\\HasNoChildren) "/" "INBOX"'])
        def select(self,token,readonly=False): calls.append(('select',readonly));return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'502'])
        def uid(self,command,*args): calls.append((command,args[-1]));return ('OK',[(b'1',rich_test_message())])
        def logout(self): pass
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',Mailbox)
    account=client.post('/api/accounts',json={'name':'old-rich-test','email':'old-rich-test@qq.com','auth_code':'fake'}).json()['id']
    with connect() as db:
        ident=db.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,body,folder) VALUES(?,?,?,?,?,?,?)',(account,'INBOX\x00502','7','sender@example.test','旧邮件','原文本','INBOX')).lastrowid
    response=client.post(f'/api/emails/{ident}/refresh-html')
    assert response.status_code==200 and response.json()['html_checked']==1
    assert '排版标题' in response.json()['html_body'] and response.json()['body']=='原文本'
    assert calls==[('select',True),('fetch','(BODY.PEEK[])')]
    assert client.post(f'/api/emails/{ident}/refresh-html').status_code==200 and len(calls)==2
    with connect() as db:
        db.execute('UPDATE emails SET html_body=?,html_checked=1,html_version=1 WHERE id=?',('<p>旧缓存</p>',ident))
    updated=client.post(f'/api/emails/{ident}/refresh-html')
    assert updated.status_code==200 and updated.json()['html_version']==2 and '排版标题' in updated.json()['html_body']
    assert len(calls)==4
    with connect() as db:
        changed=db.execute('INSERT INTO emails(account_id,uidvalidity,uid,sender,subject,body,folder) VALUES(?,?,?,?,?,?,?)',(account,'INBOX\x00old','8','sender@example.test','旧邮件','原文本','INBOX')).lastrowid
    assert client.post(f'/api/emails/{changed}/refresh-html').status_code==502
    assert client.get(f'/api/emails/{changed}').json()['html_checked']==0
    assert calls[-1]==('select',True)

def test_real_pdf_text_extraction_and_retry():
    import fitz
    doc=fitz.open()
    page=doc.new_page()
    text='电子发票\n发票号码：12345678901234567890\n开票日期：2026年10月28日\n购买方信息\n名称：示例购买方\n销售方信息\n名称：示例销售方\n价税合计（小写）￥268.00'
    page.insert_textbox(fitz.Rect(40,40,550,600),text,fontname='china-s',fontsize=13)
    contents=doc.tobytes()
    doc.close()
    result=client.post('/api/invoices/upload',files=[('files',('real.pdf',contents,'application/pdf'))]).json()[0]
    invoice=next(i for i in client.get('/api/invoices').json() if i['id']==result['id'])
    assert invoice['recognition_status']=='已识别'
    assert invoice['amount_cents']==26800
    assert invoice['buyer']=='示例购买方'
    assert invoice['seller']=='示例销售方'
    preview=client.get(f"/api/invoices/{result['id']}/preview")
    assert preview.status_code==200 and preview.headers['content-type']=='image/png'
    from PIL import Image
    with Image.open(io.BytesIO(preview.content)) as image:
        assert image.format=='PNG' and max(image.size)<=1200
    assert client.get('/api/invoices/99999999/preview').status_code==404

def test_pdf_preview_supports_multiple_pages(monkeypatch):
    import fitz
    monkeypatch.setattr(service, 'recognize', lambda p: ({}, '需要人工确认'))
    doc=fitz.open()
    doc.new_page(width=200,height=100).insert_text((20,40),'FIRST PAGE')
    doc.new_page(width=100,height=200).insert_text((20,40),'SECOND PAGE')
    contents=doc.tobytes();doc.close()
    ident=client.post('/api/invoices/upload',files=[('files',('two-pages.pdf',contents,'application/pdf'))]).json()[0]['id']
    first=client.get(f'/api/invoices/{ident}/preview')
    second=client.get(f'/api/invoices/{ident}/preview?page=2')
    assert client.get(f'/api/invoices/{ident}/preview?info=true').json()=={'pages':2}
    assert first.status_code==second.status_code==200 and first.content!=second.content
    assert client.get(f'/api/invoices/{ident}/preview?page=0').status_code==422
    assert client.get(f'/api/invoices/{ident}/preview?page=3').status_code==404
    assert client.get(f'/api/invoices/{ident}/original').content==contents


def invoice_xml():
    return '''<?xml version="1.0" encoding="UTF-8"?><Invoice xmlns="urn:invoice"><Fphm>88123456789012345678</Fphm><Kprq>2026-10-15</Kprq><Gmfmc>示例购买方公司</Gmfmc><Xsfmc>示例销售方公司</Xsfmc><Jshj>200.35</Jshj></Invoice>'''.encode()


def invoice_ofd():
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('OFD.xml','<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DocBody/></ofd:OFD>')
        archive.writestr('Doc_0/CustomTags.xml',invoice_xml())
    return stream.getvalue()


def test_xml_ofd_upload_recognize_and_export_originals():
    original_xml=invoice_xml()
    original_ofd=invoice_ofd()
    upload=client.post('/api/invoices/upload',files=[('files',('invoice.xml',original_xml,'application/xml')),('files',('invoice.ofd',original_ofd,'application/ofd'))])
    assert upload.status_code==200
    xml_item,ofd_item=upload.json()
    assert 'id' in xml_item and 'id' in ofd_item
    invoices={x['id']:x for x in client.get('/api/invoices').json()}
    for item in (xml_item,ofd_item):
        invoice=invoices[item['id']]
        assert invoice['recognition_status'] in ('已识别','识别失败')
        if invoice['recognition_status']=='识别失败': assert invoice['duplicate_state']=='pending'
        assert (invoice['buyer'],invoice['seller'],invoice['amount_cents'],invoice['invoice_date'])==('示例购买方公司','示例销售方公司',20035,'2026-10-15')
    assert client.get(f"/api/invoices/{xml_item['id']}/original").content==original_xml
    assert client.get(f"/api/invoices/{xml_item['id']}/preview").status_code==415
    assert client.get(f"/api/invoices/{ofd_item['id']}/original").content==original_ofd
    xml_preview=client.get(f"/api/invoices/{xml_item['id']}/text-preview")
    assert xml_preview.status_code==200 and '88123456789012345678' in xml_preview.json()['text']
    ofd_preview=client.get(f"/api/invoices/{ofd_item['id']}/text-preview")
    assert ofd_preview.status_code==200 and 'Fphm: 88123456789012345678' in ofd_preview.json()['text']
    zipped=client.post('/api/invoices/export',json={'ids':[xml_item['id'],ofd_item['id']]})
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        entries={Path(n).suffix:archive.read(n) for n in archive.namelist()}
    assert entries=={'.xml':original_xml,'.ofd':original_ofd}
    rejected=client.post('/api/invoices/export',json={'ids':[xml_item['id']], 'pdf_only':True})
    assert rejected.status_code==422


def test_reject_malicious_xml_and_ofd_paths():
    xml=b'<!DOCTYPE a [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><a>&xxe;</a>'
    result=client.post('/api/invoices/upload',files=[('files',('evil.xml',xml,'application/xml'))]).json()[0]
    assert 'error' in result
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('OFD.xml','<OFD/>')
        archive.writestr('../outside.xml','<x/>')
    result=client.post('/api/invoices/upload',files=[('files',('evil.ofd',stream.getvalue(),'application/ofd'))]).json()[0]
    assert 'error' in result


def test_untrusted_invoice_urls_cannot_reach_private_addresses(monkeypatch):
    import socket
    from app.download import candidates, download
    assert candidates('https://example.test/file.pdf?x=1')==['https://example.test/file.pdf?x=1']
    assert candidates('http://example.test/file.pdf')==['https://example.test/file.pdf']
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    connected=[]
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k: connected.append(True))
    import pytest
    with pytest.raises(ValueError,match='非公网'):
        download('https://invoice.example.test/test.pdf')
    assert not connected


def test_mismatched_xml_totals_need_review():
    from app.formats import read_xml
    from app.recognition import parse_xml_invoice
    xml='''<Invoice><InvoiceNumber>23456789012345678901</InvoiceNumber><IssueTime>2026-10-10</IssueTime><BuyerName>购买方</BuyerName><SellerName>销售方</SellerName><TotalAmWithoutTax>100.00</TotalAmWithoutTax><TotalTaxAm>6.00</TotalTaxAm><TotalTaxIncludedAmount>160.00</TotalTaxIncludedAmount></Invoice>'''.encode()
    fields,reason=parse_xml_invoice(read_xml(xml))
    assert fields['amount_cents']==16000
    assert fields['invoice_date']=='2026-10-10'
    assert '不一致' in reason


def test_mail_direct_link_failure_then_retry(monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    calls=[]
    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'99'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'5'])
            message=EmailMessage(); message['Subject']='电子发票'
            message.set_content('下载：https://example.test/invoice.xml')
            return ('OK',[(b'5',message.as_bytes())])
    def fake_download(url):
        calls.append(url)
        if len(calls)==1: raise RuntimeError('offline')
        return 'invoice.xml',invoice_xml()
    monkeypatch.setattr(mail,'download',fake_download)
    account=client.post('/api/accounts',json={'name':'link-test','email':'link-test@qq.com','auth_code':'fake-code'}).json()['id']
    mailbox=Mailbox()
    mail.sync_folder(mailbox,account,b'INBOX','INBOX')
    first=client.get('/api/emails',params={'account_id':account}).json()[0]
    assert first['status']=='发票链接待处理'
    mail.sync_folder(mailbox,account,b'INBOX','INBOX')
    second=client.get('/api/emails',params={'account_id':account}).json()[0]
    assert second['status']=='已识别'
    assert len(client.get('/api/emails/'+str(second['id'])).json()['invoices'])==1


def test_retry_preserves_user_fields_and_reimbursed_lock(monkeypatch):
    import app.main as main
    fake=b'%PDF-1.7\ntest retry immutable\n%%EOF'
    monkeypatch.setattr(service,'recognize',lambda p: ({},'识别失败'))
    ident=client.post('/api/invoices/upload',files=[('files',('failed.pdf',fake,'application/pdf'))]).json()[0]['id']
    assert client.patch(f'/api/invoices/{ident}',json={'buyer':'已确认抬头','seller':'','invoice_date':None}).status_code==200
    monkeypatch.setattr(main,'recognize',lambda p: ({},'识别失败'))
    assert client.post(f'/api/invoices/{ident}/retry').status_code==200
    invoice=next(i for i in client.get('/api/invoices').json() if i['id']==ident)
    assert invoice['buyer']=='已确认抬头'
    assert client.post('/api/groups',json={'name':'未识别票据','ids':[ident]}).status_code==409


def test_multiple_originals_from_same_invoice_cannot_group_together():
    xml_id=client.post('/api/invoices/upload',files=[('files',('x.xml',invoice_xml(),'application/xml'))]).json()[0]['id']
    ofd_id=client.post('/api/invoices/upload',files=[('files',('x.ofd',invoice_ofd(),'application/ofd'))]).json()[0]['id']
    assert client.post('/api/groups',json={'name':'重复票号','ids':[xml_id,ofd_id]}).status_code==409


def test_mail_multiple_formats_and_two_mailboxes(monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'122'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'10'])
            message=EmailMessage(); message['Subject']='发票'
            message.set_content('请查收附件。')
            message.add_attachment(invoice_xml(),maintype='application',subtype='xml',filename='票据.xml')
            message.add_attachment(invoice_ofd(),maintype='application',subtype='octet-stream',filename='票据.ofd')
            return ('OK',[(b'10',message.as_bytes())])
    for name in ['one-format-test','two-format-test']:
        ident=client.post('/api/accounts',json={'name':name,'email':f'{name}@qq.com','auth_code':'fake'}).json()['id']
        assert mail.sync_folder(Mailbox(),ident,b'INBOX','INBOX')==1
        m=client.get('/api/emails',params={'account_id':ident}).json()[0]
        detail=client.get('/api/emails/'+str(m['id'])).json()
        assert detail['status']=='识别失败'
        assert any(i['duplicate_state']=='pending' for i in detail['invoices'])
        assert len(detail['invoices'])==2
        assert len(detail['attachments'])==2
        assert mail.sync_folder(Mailbox(),ident,b'INBOX','INBOX')==0

def test_automatic_mail_duplicates_are_linked_or_flagged_for_review(monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    original=invoice_xml().replace(b'88123456789012345678',b'44123456789012345678')
    same_number=original.replace(b'200.35',b'201.35')
    existing_id=client.post('/api/invoices/upload',files=[('files',('original.xml',original,'application/xml'))]).json()[0]['id']
    class Mailbox:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'601'])
        def uid(self,cmd,*args):
            if cmd=='search': return ('OK',[b'1'])
            message=EmailMessage();message['Subject']='发票';message.set_content('请查收')
            message.add_attachment(original,maintype='application',subtype='xml',filename='same.xml')
            message.add_attachment(same_number,maintype='application',subtype='xml',filename='different.xml')
            return ('OK',[(b'1',message.as_bytes())])
    account=client.post('/api/accounts',json={'name':'duplicate-mail','email':'duplicate-mail@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_folder(Mailbox(),account,b'INBOX','INBOX')==1
    message=client.get('/api/emails',params={'account_id':account}).json()[0]
    assert message['status']=='识别失败'
    linked=client.get(f"/api/emails/{message['id']}").json()['invoices']
    assert len(linked)==2 and any(i['id']==existing_id for i in linked)
    duplicate=next(i for i in client.get('/api/invoices').json() if i['original_name']=='different.xml')
    assert duplicate['duplicate_of']==existing_id and duplicate['recognition_status']=='识别失败'
    assert '是否重复' in duplicate['failure_reason']
    assert client.get(f"/api/invoices/{duplicate['id']}/original").content==same_number
    assert client.post(f"/api/invoices/{duplicate['id']}/retry").status_code==200
    after_retry=next(i for i in client.get('/api/invoices').json() if i['id']==duplicate['id'])
    assert after_retry['duplicate_of']==existing_id and after_retry['recognition_status']=='识别失败'
    value={'buyer':'示例购买方公司','seller':'示例销售方公司','amount_cents':20135,'invoice_date':'2026-10-15','invoice_number':'44123456789012345678'}
    assert client.patch(f"/api/invoices/{duplicate['id']}",json=value).status_code==200
    still_duplicate=next(i for i in client.get('/api/invoices').json() if i['id']==duplicate['id'])
    assert still_duplicate['recognition_status']=='识别失败' and still_duplicate['duplicate_of']==existing_id
    endpoint=f"/api/invoices/{duplicate['id']}/duplicate"
    assert client.patch(endpoint,json={'decision':'confirm'}).status_code==200
    confirmed=next(i for i in client.get('/api/invoices').json() if i['id']==duplicate['id'])
    assert confirmed['duplicate_state']=='confirmed' and confirmed['recognition_status']=='识别失败'
    assert client.get(f"/api/emails/{message['id']}").json()['status']=='重复发票'
    assert client.patch(endpoint,json={'decision':'reopen'}).status_code==200
    assert client.get(f"/api/emails/{message['id']}").json()['status']=='识别失败'
    assert client.patch(endpoint,json={'decision':'distinct'}).status_code==200
    dismissed=next(i for i in client.get('/api/invoices').json() if i['id']==duplicate['id'])
    assert dismissed['duplicate_state']=='dismissed' and dismissed['recognition_status']=='已识别'
    assert client.get(f"/api/emails/{message['id']}").json()['status']=='已识别'
    assert client.post(f"/api/invoices/{duplicate['id']}/retry").status_code==200
    assert next(i for i in client.get('/api/invoices').json() if i['id']==duplicate['id'])['duplicate_state']=='dismissed'
    assert client.post('/api/groups',json={'name':'已确认不同发票','ids':[existing_id,duplicate['id']]}).status_code==200
    assert client.patch(f"/api/invoices/{duplicate['id']}",json=value).status_code==200
    value['invoice_number']='55123456789012345678'
    assert client.patch(f"/api/invoices/{duplicate['id']}",json=value).status_code==200
    fixed=next(i for i in client.get('/api/invoices').json() if i['id']==duplicate['id'])
    assert fixed['duplicate_of'] is None and fixed['recognition_status']=='已识别'

def test_concurrent_automatic_imports_check_invoice_number_atomically(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    fields={'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'99123456789012345678'}
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    monkeypatch.setattr(service,'assist',lambda path,fields,reason:(fields,reason,{}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        items=list(pool.map(lambda n:service.import_file(f'{n}.pdf',f'%PDF-atomic-{n}'.encode(),'邮件'),(1,2)))
    ids={item[0] for item in items}
    rows=[i for i in client.get('/api/invoices').json() if i['id'] in ids]
    assert len(rows)==2
    assert sorted(i['recognition_status'] for i in rows)==['已识别','识别失败']
    flagged=next(i for i in rows if i['duplicate_of'])
    assert flagged['duplicate_of'] in ids

def test_ai_suggestion_rechecks_duplicate_invoice_number(monkeypatch):
    import app.main as main
    number='98123456789012345678'
    fields={'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':number}
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    monkeypatch.setattr(service,'assist',lambda path,fields,reason:(fields,reason,{}))
    original=service.import_file('ai-base.pdf',b'%PDF-ai-duplicate-base','本地上传')[0]
    monkeypatch.setattr(service,'recognize',lambda path:({**fields,'invoice_number':''},'未识别票号'))
    candidate=service.import_file('ai-candidate.pdf',b'%PDF-ai-duplicate-candidate','邮件')[0]
    monkeypatch.setattr(main,'assist',lambda path,current,reason,force=False:(fields.copy(),'AI 建议已生成，请人工核对',{'invoice_number':number}))
    assert client.post(f'/api/invoices/{candidate}/ai').status_code==200
    invoice=next(i for i in client.get('/api/invoices').json() if i['id']==candidate)
    assert invoice['duplicate_of']==original and invoice['duplicate_state']=='pending' and '是否重复' in invoice['failure_reason']

def test_automatic_invoice_without_number_requires_review(monkeypatch):
    import app.main as main
    fields={'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':''}
    monkeypatch.setattr(service,'recognize',lambda path:(fields.copy(),''))
    monkeypatch.setattr(service,'assist',lambda path,fields,reason:(fields,reason,{}))
    monkeypatch.setattr(main,'recognize',lambda path:(fields.copy(),''))
    monkeypatch.setattr(main,'assist',lambda path,fields,reason:(fields,reason,{}))
    ident=service.import_file('without-number.pdf',b'%PDF-without-number-review','邮件')[0]
    invoice=next(i for i in client.get('/api/invoices').json() if i['id']==ident)
    assert invoice['duplicate_state']=='unverified' and invoice['recognition_status']=='识别失败'
    assert '无法判断是否重复' in invoice['failure_reason']
    assert client.patch(f'/api/invoices/{ident}/duplicate',json={'decision':'confirm'}).status_code==409
    assert client.patch(f'/api/invoices/{ident}/duplicate',json={'decision':'distinct'}).status_code==200
    assert client.post(f'/api/invoices/{ident}/retry').status_code==200
    invoice=next(i for i in client.get('/api/invoices').json() if i['id']==ident)
    assert invoice['duplicate_state']=='dismissed' and invoice['recognition_status']=='已识别'


def test_xml_ofd_duplicate_filenames_keep_original_extension():
    ids=[client.post('/api/invoices/upload',files=[('files',(name,data,'application/octet-stream'))]).json()[0]['id'] for name,data in [('a.xml',invoice_xml()),('a.ofd',invoice_ofd())]]
    for ident in ids:
        assert client.patch(f'/api/invoices/{ident}',json={'buyer':'购买方','seller':'销售方','amount_cents':100,'invoice_date':'2026-01-01','export_name':'一样.pdf'}).status_code==200
    result=client.post('/api/invoices/export',json={'ids':ids})
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert sorted(Path(n).suffix for n in archive.namelist())==['.ofd','.xml']


def test_hosts_are_accepted_and_cross_origin_requests_are_rejected():
    cookie='piaoxu_session='+client.cookies.get('piaoxu_session')
    for host in ('rebound.example','192.168.1.50:8765','10.0.0.5:8765','172.16.0.5:8765','8.8.8.8:8765'):
        assert client.get('/api/invoices',headers={'Host':host,'Cookie':cookie}).status_code==200
    assert client.post('/api/groups',headers={'Origin':'https://evil.example'},json={'name':'x','ids':[1]}).status_code==403
    assert client.get('/api/invoices',headers={'Host':'localhost:8765','Origin':'http://localhost:8765','Cookie':cookie}).status_code==200


def test_email_zip_unpacks_invoice_originals_and_rejects_escape():
    from app.mail import unpack_attachments
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('发票/票据.xml',invoice_xml())
        archive.writestr('说明.txt','示例')
    items=unpack_attachments(stream.getvalue())
    assert items==[('票据.xml',invoice_xml())]
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('../unsafe.xml',invoice_xml())
    import pytest
    with pytest.raises(ValueError): unpack_attachments(stream.getvalue())


def test_group_cannot_reimburse_incomplete_invoice(monkeypatch):
    monkeypatch.setattr(service,'recognize',lambda p: ({'buyer':'买方','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01','invoice_number':'TEST-1003'},''))
    data=b'%PDF-1.7 group incomplete fixture %%EOF'
    ident=client.post('/api/invoices/upload',files=[('files',('group.pdf',data,'application/pdf'))]).json()[0]['id']
    group=client.post('/api/groups',json={'name':'状态校验','ids':[ident]}).json()['id']
    assert client.patch(f'/api/invoices/{ident}',json={'buyer':'','seller':'卖方','amount_cents':100,'invoice_date':'2026-01-01'}).status_code==200
    assert client.patch(f'/api/groups/{group}',json={'reimbursed':True}).status_code==409


def test_ocr_cannot_mistake_seller_for_buyer():
    text='电子发票\n购买方信息\nBm：示例购买方\n销售方信息\n名称：示例销售方\n开票日期：2026年10月28日\n价税合计（小写）￥268.00'
    fields,reason=parse_invoice(text)
    assert not fields['buyer']
    assert fields['seller']=='示例销售方'
    assert '抬头' in reason


def test_link_candidates_include_labelled_download_and_ignore_unrelated_links():
    from app.download import candidates
    html='<a href="https://invoices.example/download?id=123&amp;type=pdf">下载发票</a><a href="https://example.test/welcome">首页</a>'
    assert candidates('您的电子发票',html)==['https://invoices.example/download?id=123&type=pdf']
    assert candidates('普通通知','<a href="https://example.test/download">下载软件</a>')==[]
    assert candidates('发票下载','<a href="/file.pdf">PDF</a>', 'https://invoices.example/view')==['https://invoices.example/file.pdf']


def test_download_landing_page_and_content_validation(monkeypatch):
    import app.download as downloader
    request_paths=[]
    responses=[(200,'text/html',b'<html><a href="/get?format=pdf">download PDF</a></html>',{}),
               (302,'text/plain',b'',{'Location':'https://files.example/getfile'}),
               (200,'application/octet-stream',b'%PDF-1.7\nexample\n%%EOF',{})]
    class Response:
        def __init__(self,spec): self.status,self.mime,self.data,self.headers=spec
        def getheader(self,key): return self.mime if key=='Content-Type' else self.headers.get(key)
        def read(self,n): return self.data[:n]
    class Connection:
        def __init__(self,host,*a,**kw): self.host=host
        def request(self,method,path,headers): request_paths.append((self.host,path))
        def getresponse(self): return Response(responses.pop(0))
        def close(self): pass
    monkeypatch.setattr(downloader,'PublicHTTPS',Connection)
    name,data=downloader.download('https://billing.example/invoice?id=1')
    assert name.endswith('.pdf') and data.startswith(b'%PDF-')
    assert request_paths==[('billing.example','/invoice?id=1'),('billing.example','/get?format=pdf'),('files.example','/getfile')]
    responses.append((200,'text/html',b'<html>login required</html>',{}))
    import pytest
    with pytest.raises(ValueError,match='手动下载'):
        downloader.download('https://billing.example/invoice.pdf')


def test_ocr_fallback_uses_second_layout_and_requires_review(monkeypatch,tmp_path):
    import fitz,pytesseract
    from app.recognition import recognize
    doc=fitz.open(); doc.new_page(); target=tmp_path/'scan.pdf'; doc.save(target);doc.close()
    observed=[]
    def fake_ocr(image,**kwargs):
        observed.append(kwargs['config'])
        if kwargs['config']=='--psm 6': return '电子发票\n开票日期：2026年10月28日'
        return '电子发票\n购买方信息\n名称：示例购买方\n销售方信息\n名称：示例销售方\n开票日期：2026年10月28日\n价税合计（小写）￥268.00\n发票号码：12345678901234567890'
    monkeypatch.setattr(pytesseract,'image_to_string',fake_ocr)
    fields,reason=recognize(target)
    assert observed==['--psm 6','--psm 3']
    assert fields['buyer']=='示例购买方' and fields['amount_cents']==26800
    assert 'OCR' in reason and '人工核对' in reason


def test_email_downloads_link_even_when_an_attachment_exists(monkeypatch):
    from email.message import EmailMessage
    import app.mail as mail
    calls=[]
    message=EmailMessage(); message['Subject']='电子发票'
    message.set_content('请查收附件与下载链接')
    message.add_alternative('<p>电子发票</p><a href="https://invoice.example/get?id=1">下载发票</a>',subtype='html')
    message.add_attachment(invoice_xml(),maintype='application',subtype='xml',filename='invoice.xml')
    class Box:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'345'])
        def uid(self,cmd,*args): return ('OK',[b'1']) if cmd=='search' else ('OK',[(b'1',message.as_bytes())])
    monkeypatch.setattr(mail,'download',lambda u:(calls.append(u) or ('link.ofd',invoice_ofd())))
    account=client.post('/api/accounts',json={'name':'mixed','email':'mixed@qq.com','auth_code':'fake-code'}).json()['id']
    mail.sync_folder(Box(),account,b'INBOX','INBOX')
    item=client.get('/api/emails',params={'account_id':account}).json()[0]
    detail=client.get('/api/emails/'+str(item['id'])).json()
    assert calls==['https://invoice.example/get?id=1']
    assert detail['status']=='识别失败' and len(detail['invoices'])==2
    assert any(i['recognition_status']=='识别失败' for i in detail['invoices'])


def test_scan_images_keep_original_and_require_review(monkeypatch):
    from PIL import Image
    import pytesseract
    from app.recognition import recognize
    buf=io.BytesIO(); Image.new('RGB',(120,80),'white').save(buf,format='PNG'); original=buf.getvalue()
    text='电子发票\n购买方信息\n名称：图片购买方\n销售方信息\n名称：图片销售方\n开票日期：2026年10月28日\n价税合计（小写）￥268.00\n发票号码：12345678901234567890'
    monkeypatch.setattr(pytesseract,'image_to_string',lambda *a,**kw:text)
    result=client.post('/api/invoices/upload',files=[('files',('scan.png',original,'image/png'))]).json()[0]
    item=next(x for x in client.get('/api/invoices').json() if x['id']==result['id'])
    assert item['buyer']=='图片购买方' and item['amount_cents']==26800
    assert item['recognition_status']=='识别失败' and '人工核对' in item['failure_reason']
    assert client.get(f"/api/invoices/{item['id']}/original").content==original
    assert client.post('/api/invoices/export',json={'ids':[item['id']],'pdf_only':True}).status_code==422
    exported=client.post('/api/invoices/export',json={'ids':[item['id']]})
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        name=archive.namelist()[0]; assert name.endswith('.png') and archive.read(name)==original


def test_photo_attachment_and_inline_logo_handling(monkeypatch):
    from email.message import EmailMessage
    from PIL import Image
    import pytesseract
    import app.mail as mail
    buf=io.BytesIO(); Image.new('RGB',(40,40),'white').save(buf,format='JPEG'); image=buf.getvalue()
    monkeypatch.setattr(pytesseract,'image_to_string',lambda *a,**kw:'电子发票')
    message=EmailMessage(); message['Subject']='您申请的发票';message.set_content('请查收')
    message.add_attachment(image,maintype='image',subtype='jpeg',filename='扫描件.jpg')
    message.add_attachment(image+b'logo',maintype='image',subtype='jpeg',filename='logo.jpg',disposition='inline')
    class Box:
        def select(self,*args,**kwargs): return ('OK',[])
        def response(self,kind): return ('UIDVALIDITY',[b'712'])
        def uid(self,cmd,*args): return ('OK',[b'1']) if cmd=='search' else ('OK',[(b'1',message.as_bytes())])
    account=client.post('/api/accounts',json={'name':'scan-test','email':'scan-test@qq.com','auth_code':'fake-code'}).json()['id']
    mail.sync_folder(Box(),account,b'INBOX','INBOX')
    message_id=client.get('/api/emails',params={'account_id':account}).json()[0]['id']
    detail=client.get(f'/api/emails/{message_id}').json()
    assert detail['status']=='识别失败' and len(detail['invoices'])==1
    assert detail['invoices'][0]['original_name']=='扫描件.jpg'


def test_ocr_disagreement_does_not_silently_choose_conflicting_amount(monkeypatch,tmp_path):
    import fitz,pytesseract
    from app.recognition import recognize
    doc=fitz.open(); doc.new_page(); path=tmp_path/'disagree.pdf';doc.save(path);doc.close()
    prefix='电子发票\n购买方信息\n名称：购买方公司\n销售方信息\n名称：销售方公司\n开票日期：2026年10月28日\n发票号码：12345678901234567890\n价税合计（小写）￥'
    outputs=iter([prefix+'268.00',prefix+'286.00','电子发票','电子发票'])
    monkeypatch.setattr(pytesseract,'image_to_string',lambda *args,**kwargs:next(outputs))
    fields,reason=recognize(path)
    assert fields['amount_cents'] is None
    assert '金额' in reason and '不一致' in reason
    assert '人工核对' in reason


def test_plain_download_link_without_file_extension_and_ssrf_redirect(monkeypatch):
    import app.download as dl
    import pytest
    assert dl.candidates('电子发票\n下载发票：https://invoices.example/get?id=1')==['https://invoices.example/get?id=1']
    assert dl.candidates('电子发票\n帮助中心：https://help.example/home')==[]
    class Response:
        status=302
        def getheader(self,key): return 'http://127.0.0.1/admin' if key=='Location' else ''
    class Connection:
        def __init__(self,*a,**kw): pass
        def request(self,*a,**kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(dl,'PublicHTTPS',Connection)
    with pytest.raises(ValueError,match='公网 HTTPS'):
        dl.download('https://billing.example/invoice')


def test_ai_settings_and_automatic_fallback(monkeypatch):
    import json
    import app.ai as ai
    from app.db import connect
    fake_key = 'YOUR_API_KEY_HERE'
    try:
        response = client.put('/api/ai-settings', json={'base_url':'http://127.0.0.1:9999/v1','model':'test-model','api_key':fake_key,'auto_enabled':True})
        assert response.status_code == 200
        assert response.json()['has_key'] is True
        assert fake_key not in response.text and fake_key not in client.get('/api/ai-settings').text
        with connect() as db:
            assert fake_key not in db.execute("SELECT value FROM settings WHERE key='ai_settings'").fetchone()['value']
        calls = []
        monkeypatch.setattr(service, 'recognize', lambda p: ({'buyer':'本地购买方'}, '缺少字段'))
        def fake_ai(path, config):
            calls.append(1)
            return {'buyer':'AI 购买方','seller':'AI 销售方','amount_cents':1250,'invoice_date':'2026-10-01'}
        monkeypatch.setattr(ai, 'recognize_ai', fake_ai)
        data = b'%PDF-1.7 AI-fallback-test\n%%EOF'
        item = client.post('/api/invoices/upload', files=[('files',('ai.pdf',data,'application/pdf'))]).json()[0]
        duplicate = client.post('/api/invoices/upload', files=[('files',('again.pdf',data,'application/pdf'))]).json()[0]
        invoice = next(i for i in client.get('/api/invoices').json() if i['id'] == item['id'])
        assert duplicate['created'] is False and len(calls) == 1
        assert invoice['buyer'] == '本地购买方' and invoice['seller'] == 'AI 销售方'
        assert invoice['recognition_status'] == '识别失败'
        assert json.loads(invoice['ai_suggestion'])['buyer'] == 'AI 购买方'
        changed = client.put('/api/ai-settings', json={'base_url':'https://example.test/v1','model':'test-model','auto_enabled':False})
        assert changed.json()['has_key'] is False
        assert client.put('/api/ai-settings', json={'base_url':'http://example.test/v1','model':'x','auto_enabled':True}).status_code == 422
        assert client.put('/api/ai-settings', json={'base_url':'http://169.254.169.254/v1','model':'x','auto_enabled':True}).status_code == 422
    finally:
        with connect() as db:
            db.execute("DELETE FROM settings WHERE key='ai_settings'")


def test_ai_manual_and_concurrent_edit(monkeypatch):
    import threading
    import app.ai as ai
    from app.db import connect
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(service, 'recognize', lambda p: ({'buyer':'原有购买方'}, '缺少字段'))
    try:
        client.put('/api/ai-settings', json={'base_url':'http://127.0.0.1:9999/v1','model':'test-model','auto_enabled':False})
        item = client.post('/api/invoices/upload', files=[('files',('manual.pdf',b'%PDF-1.7 AI-manual-test\n%%EOF','application/pdf'))]).json()[0]
        ident = item['id']
        calls = []
        def fake_ai(path, config):
            calls.append(1); started.set(); assert release.wait(5)
            return {'seller':'AI 销售方','amount_cents':500}
        monkeypatch.setattr(ai, 'recognize_ai', fake_ai)
        result = []
        worker = threading.Thread(target=lambda: result.append(client.post(f'/api/invoices/{ident}/ai')))
        worker.start()
        assert started.wait(5)
        assert client.post(f'/api/invoices/{ident}/retry').status_code == 409
        assert client.post(f'/api/invoices/{ident}/ai').status_code == 409
        assert client.patch(f'/api/invoices/{ident}',json={'buyer':'人工修改'}).status_code == 200
        release.set(); worker.join(5)
        assert not worker.is_alive() and result[0].status_code == 409 and len(calls) == 1
        invoice = next(i for i in client.get('/api/invoices').json() if i['id'] == ident)
        assert invoice['buyer'] == '人工修改' and invoice['seller'] == ''
        response = client.post(f'/api/invoices/{ident}/ai')
        assert response.status_code == 200
        invoice = next(i for i in client.get('/api/invoices').json() if i['id'] == ident)
        assert invoice['buyer'] == '人工修改' and invoice['seller'] == 'AI 销售方'
        assert invoice['recognition_status'] == '识别失败'
    finally:
        release.set()
        with connect() as db:
            db.execute("DELETE FROM settings WHERE key='ai_settings'")


def test_ai_response_validation_and_timeout(monkeypatch, tmp_path):
    import httpx
    import pytest
    import app.ai as ai
    assert ai.parse_answer('{"is_invoice":true,"amount":"￥12.50","invoice_date":"2026-10-01"}') == {'amount_cents':1250,'invoice_date':'2026-10-01'}
    with pytest.raises(ValueError): ai.parse_answer('{"is_invoice":false,"buyer":"x"}')
    with pytest.raises(ValueError): ai.parse_answer('{"is_invoice":true,"amount":"-12"}')
    xml = tmp_path / 'invoice.xml'
    xml.write_text('<invoice>虚构发票文本</invoice>')
    attempts = []
    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args, **kwargs):
            attempts.append(1)
            raise httpx.ReadTimeout('sensitive upstream detail')
    monkeypatch.setattr(ai.httpx, 'Client', FakeClient)
    with pytest.raises(ValueError, match='超时') as exc:
        ai.recognize_ai(xml, {'base_url':'http://127.0.0.1:9999/v1','model':'test-model'})
    assert len(attempts) == 1 and 'sensitive' not in str(exc.value)


def test_ai_uses_all_bounded_pdf_pages_and_ofd_text(tmp_path):
    import base64
    import fitz
    import app.ai as ai
    document = fitz.open()
    for _ in range(3): document.new_page(width=200, height=300)
    pdf = tmp_path / 'invoice.pdf'
    pdf.write_bytes(document.tobytes()); document.close()
    parts = ai.content_parts(pdf)
    assert len(parts) == 3 and all(part['type'] == 'image_url' for part in parts)
    assert all(base64.b64decode(part['image_url']['url'].split(',')[1]).startswith(b'\xff\xd8') for part in parts)
    ofd = tmp_path / 'invoice.ofd'
    with zipfile.ZipFile(ofd, 'w') as archive:
        archive.writestr('OFD.xml','<OFD><DocBody>合成测试</DocBody></OFD>')
        archive.writestr('Doc_0/Content.xml','<Content>虚构商户</Content>')
        archive.writestr('Doc_0/Signs.xml','<Sign>签章数据不发送</Sign>')
    text = ai.content_parts(ofd)[0]['text']
    assert '虚构商户' in text and '签章数据不发送' not in text


def test_dynamic_invoice_page_downloads_original_and_checks_returned_url(monkeypatch):
    import json
    import pytest
    import app.download as downloader
    from urllib.parse import parse_qs
    requests=[]
    page='https://billing.example/scan-invoice/printQrcode?paramList=synthetic%2Bvalue%3D&aliView=true&shortLinkSource=1'
    responses=[(302,'',b'',{'Location':page}),
               (200,'text/html',b'<html><script></script></html>',{}),
               (200,'application/json',json.dumps({'data':{'invoiceSimpleVo':{'url':'https://files.example/fake.pdf'}}}).encode(),{}),
               (200,'application/pdf',b'%PDF-1.7\nsynthetic\n%%EOF',{})]
    class Response:
        def __init__(self,spec): self.status,self.mime,self.data,self.headers=spec
        def getheader(self,key): return self.mime if key=='Content-Type' else self.headers.get(key)
        def read(self,n): return self.data[:n]
    class Connection:
        def __init__(self,host,*args,**kwargs): self.host=host
        def request(self,method,path,headers,body=None): requests.append((self.host,method,path,body))
        def getresponse(self): return Response(responses.pop(0))
        def close(self): pass
    monkeypatch.setattr(downloader,'PublicHTTPS',Connection)
    name,data=downloader.download('https://billing.example/short')
    assert name.endswith('.pdf') and data.startswith(b'%PDF-')
    assert requests[2][1:3]==('POST','/sapi/scan2/getIvcDetailShow.do')
    params=parse_qs(requests[2][3])
    assert params['paramList']==['synthetic%2Bvalue%3D'] and params['code']==params['paramList']
    assert requests[-1][0]=='files.example'
    responses.extend([(200,'text/html',b'<html></html>',{}),
                      (200,'application/json',b'{"data":{"invoiceSimpleVo":{"url":"http://127.0.0.1/internal"}}}',{})])
    with pytest.raises(ValueError,match='公网 HTTPS'):
        downloader.download(page)


def test_text_pdf_with_vertical_labels_and_unordered_contents():
    import fitz
    from app.recognition import recognize_pdf
    pdf=fitz.open(); page=pdf.new_page(width=700,height=350)
    def put(x,y,text,size=10): page.insert_text((x,y),text,fontname='china-s',fontsize=size)
    # Write values first to reproduce PDFs whose extraction order differs from their visual layout.
    put(60,110,'星辰贸易有限公司');put(346,110,'远航科技有限公司')
    put(440,55,'2026年10月08日');put(440,35,'12345678901234567890');put(450,200,'￥128.00')
    put(100,35,'电子发票');put(390,35,'发票号码：');put(390,55,'开票日期：')
    put(20,103,'购');put(20,113,'买');put(20,123,'方')
    put(304,103,'销');put(304,113,'售');put(304,123,'方')
    put(34,110,'名称：');put(322,110,'名称：');put(30,200,'价税合计（小写）')
    fields,reason=recognize_pdf(pdf)
    assert reason == ''
    assert fields['buyer']=='星辰贸易有限公司'
    assert fields['seller']=='远航科技有限公司'
    assert fields['amount_cents']==12800
    assert fields['invoice_date']=='2026-10-08'
    assert fields['invoice_number']=='12345678901234567890'
    pdf.close()


def test_invoice_date_is_a_calendar_date_and_ignores_other_timestamps():
    from app.formats import read_xml
    from app.recognition import parse_xml_invoice
    xml=b'''<Invoice><RequestTime>2026-04-22 15:58:24</RequestTime><IssueTime>2026-04-23T00:00:00+08:00</IssueTime><SignatureTime>2026-04-23 11:18:09</SignatureTime><BuyerName>A</BuyerName><SellerName>B</SellerName><TotalTaxIncludedAmount>10.00</TotalTaxIncludedAmount><InvoiceNumber>12345678</InvoiceNumber></Invoice>'''
    fields,reason=parse_xml_invoice(read_xml(xml))
    assert fields['invoice_date']=='2026-04-23' and not reason
    single=xml.replace(b'IssueTime',b'InvoiceDate')
    single_fields,single_reason=parse_xml_invoice(read_xml(single))
    assert single_fields['invoice_date']=='2026-04-23' and not single_reason
    xml=xml.replace(b'</Invoice>',b'<IssueDate>2026-04-22</IssueDate></Invoice>')
    fields,reason=parse_xml_invoice(read_xml(xml))
    assert fields['invoice_date']=='' and '开票日期存在不同值' in reason


def test_invoice_date_conflicting_labels_require_review():
    text='电子发票\n购买方名称：甲公司\n销售方名称：乙公司\n价税合计（小写）￥10.00\n开票日期：2026年04月23日\n开具日期：2026年04月22日\n发票号码：12345678'
    fields,reason=parse_invoice(text)
    assert fields['invoice_date']=='' and '开票日期存在不同值' in reason
    same=text.replace('2026年04月22日','2026年04月23日')
    fields,reason=parse_invoice(same)
    assert fields['invoice_date']=='2026-04-23' and not reason
    invalid=text.replace('2026年04月22日','2026年13月40日')
    fields,reason=parse_invoice(invalid)
    assert fields['invoice_date']=='' and reason


def test_pdf_with_conflicting_issue_dates_cannot_be_auto_confirmed():
    import fitz
    from app.recognition import recognize_pdf
    pdf=fitz.open(); page=pdf.new_page()
    page.insert_text((40,40),'电子发票',fontname='china-s')
    page.insert_text((40,60),'开票日期：2026年04月23日',fontname='china-s')
    page.insert_text((40,80),'开票日期：2026年04月22日',fontname='china-s')
    fields,reason=recognize_pdf(pdf)
    assert fields['invoice_date']=='' and '开票日期存在不同值' in reason
    pdf.close()


def test_static_assets_revalidate_after_updates():
    import re
    page=client.get('/')
    assert page.status_code==200 and page.headers['cache-control']=='no-cache'
    css=re.search(r'href="(/style\.css\?[^\"]+)"',page.text).group(1)
    js=re.search(r'src="(/app\.js\?[^\"]+)"',page.text).group(1)
    for path in (css,js,'/icons.svg?v=2','/line-ripple.svg?v=2'):
        response=client.get(path)
        assert response.status_code==200 and response.headers['cache-control']=='no-cache'
        cached=client.get(path,headers={'If-None-Match':response.headers['etag']})
        assert cached.status_code==304 and cached.headers['cache-control']=='no-cache'


def test_http_invoice_links_upgrade_without_changing_signature_or_unrelated_links():
    from app.download import candidates
    url='http://files.example/invoice.pdf?Expires=1234567890&AccessKey=TEST_ONLY&Signature=a%2Bb%2Fc%3D'
    secure=url.replace('http://','https://',1)
    html=f'<a href="{url.replace("&","&amp;")}">发票PDF文件下载</a>'
    assert candidates('电子发票已开具',html)==[secure]
    assert candidates(f'电子发票下载：{url}\n{secure}')==[secure]
    assert candidates('电子发票','<a href="http://files.example:80/get?id=1">下载发票</a>')==['https://files.example/get?id=1']
    assert candidates('电子发票','<a href="http://help.example/">帮助中心</a>')==[]
    assert candidates('http://files.example:8080/invoice.pdf')==[]
    assert candidates('http://user:password@files.example/invoice.pdf')==[]


def test_upgraded_http_invoice_download_uses_tls_and_still_rejects_private_hosts(monkeypatch):
    import socket,pytest
    import app.download as dl
    url=dl.candidates('http://files.example/invoice.pdf?Signature=TEST_ONLY')[0]
    calls=[]
    class Response:
        status=200
        def getheader(self,key): return 'application/pdf' if key=='Content-Type' else None
        def read(self,n): return b'%PDF-1.7\nexample\n%%EOF'
    class Connection:
        def __init__(self,host,port,**kwargs): calls.append((host,port,kwargs['context'].check_hostname))
        def request(self,method,path,headers): calls.append((method,path))
        def getresponse(self): return Response()
        def close(self): pass
    with monkeypatch.context() as mocked:
        mocked.setattr(dl,'PublicHTTPS',Connection)
        assert dl.download(url)[0].endswith('.pdf')
    assert calls==[('files.example',443,True),('GET','/invoice.pdf?Signature=TEST_ONLY')]
    connected=[]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    monkeypatch.setattr(socket,'create_connection',lambda *args,**kwargs:connected.append(True))
    with pytest.raises(ValueError,match='非公网'): dl.download(url)
    assert not connected
