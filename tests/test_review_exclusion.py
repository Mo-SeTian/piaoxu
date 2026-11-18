from email.message import EmailMessage

from test_backup import invoice_xml, isolated_storage, synthetic_imap


def test_excluded_mail_attachment_stays_out_of_review_until_restored(isolated_storage, monkeypatch):
    from app import mail

    _,_,client,_,_,_ = isolated_storage
    raw=invoice_xml('78123456789012345678').replace(b'<Jshj>200.35</Jshj>', b'')
    message=EmailMessage()
    message['Subject']='测试发票'
    message.set_content('请查收附件')
    message.add_attachment(raw,maintype='application',subtype='xml',filename='example.xml')
    monkeypatch.setattr(mail.imaplib,'IMAP4_SSL',synthetic_imap({b'1':message.as_bytes()},[],[b'123']))
    account=client.post('/api/accounts',json={'name':'测试','email':'excluded-test@qq.com','auth_code':'fake'}).json()['id']
    assert mail.sync_account(account)==1
    email_id=client.get('/api/emails').json()[0]['id']
    before=client.get('/api/invoices',params={'view':'review'}).json()[0]
    assert before['recognition_status']=='识别失败'
    url=f"/api/invoices/{before['id']}"
    payload={'excluded':True,'sha256':before['sha256']}
    assert client.patch(url+'/review',json={**payload,'sha256':'0'*64}).status_code==409
    assert client.patch(url+'/review',json=payload).json()=={'recognition_status':'已排除'}
    assert client.get('/api/dashboard').json()['summary']['failed_invoices']==0
    assert client.get('/api/invoices',params={'limit':50}).json()['total']==0
    assert client.get(url+'/original').content==raw
    assert client.post(url+'/retry').status_code==409
    assert client.post(url+'/ai').status_code==409
    assert client.patch(url,json={}).status_code==409

    retried=client.post(f'/api/emails/{email_id}/reprocess').json()
    assert retried['created']==retried['retried']==0 and retried['existing']==1
    detail=client.get(f'/api/emails/{email_id}').json()
    assert detail['status']=='发票已排除' and detail['excluded_count']==1 and detail['failed_count']==0
    [listed]=client.get('/api/emails').json()
    assert listed['excluded_count']==1 and listed['invoice_count']==0
    assert client.get('/api/emails',params={'invoice_status':'failed'}).json()==[]
    [excluded]=client.get('/api/invoices',params={'view':'review'}).json()
    assert excluded['id']==before['id'] and excluded['recognition_status']=='已排除'

    assert client.patch(url+'/review',json={**payload,'excluded':False}).json()=={'recognition_status':'识别失败'}
    assert client.get('/api/dashboard').json()['summary']['failed_invoices']==1
    assert client.get(f'/api/emails/{email_id}').json()['status']=='识别失败'
    [restored]=client.get('/api/invoices',params={'view':'review'}).json()
    assert restored==before


def test_cannot_exclude_recognized_invoice(isolated_storage):
    _,_,client,service_db,service,_=isolated_storage
    ident,_=service.import_file('recognized.xml',invoice_xml('78123456789012345678'),'本地上传')
    with service_db.connect() as db:
        sha=db.execute('SELECT sha256 FROM invoices WHERE id=?',(ident,)).fetchone()[0]
    assert client.patch(f'/api/invoices/{ident}/review',json={'excluded':True,'sha256':sha}).status_code==409
    assert client.get('/api/invoices',params={'limit':50}).json()['total']==1
