from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from test_backup import isolated_storage, invoice_xml


def test_configured_channels_encrypt_credentials_and_send_test(isolated_storage, monkeypatch):
    from app import notifications
    _, _, client, db, _, _ = isolated_storage
    from app.main import app

    anonymous = TestClient(app, headers={'X-Piaoxu-Request':'1'})
    assert anonymous.get('/api/notification-channels').status_code == 401

    calls = []

    def provider(host, path, **body):
        calls.append((host, path, body))
        if host == 'wxpusher.zjiecode.com':
            return {'code':1000}
        if '/gettoken?' in path:
            return {'errcode':0, 'access_token':'fake_access_token', 'expires_in':7200}
        if host == 'qyapi.weixin.qq.com':
            return {'errcode':0}
        return {'code':0}

    monkeypatch.setattr(notifications, '_request_json', provider)
    configs = [
        {'name':'个人微信', 'provider':'wxpusher', 'secret':'AT_test_only',
         'uids':'UID_test_a, UID_test_b', 'topics':'12'},
        {'name':'工作提醒', 'provider':'wecom', 'secret':'test_app_secret',
         'corp_id':'ww_test_corp', 'agent_id':'1000002', 'users':'user1|user2'},
        {'name':'Server酱', 'provider':'serverchan', 'secret':'SCT_test_only'},
        {'name':'Server酱³', 'provider':'serverchan', 'secret':'sctp123tTEST'},
    ]

    for config in configs:
        response = client.post('/api/notification-channels', json=config)
        assert response.status_code == 201
        assert config['secret'] not in response.text
        channel = response.json()
        assert channel['has_secret']
        with db.connect() as conn:
            encrypted = conn.execute('SELECT secret FROM notification_channels WHERE id=?',(channel['id'],)).fetchone()[0]
            assert config['secret'] != encrypted
        assert client.post(f"/api/notification-channels/{channel['id']}/test").status_code == 200

    assert calls[0][0:2] == ('wxpusher.zjiecode.com', '/api/send/message')
    assert calls[0][2]['payload']['uids'] == ['UID_test_a', 'UID_test_b']
    assert calls[0][2]['payload']['topicIds'] == [12]
    assert parse_qs(urlsplit(calls[1][1]).query) == {'corpid':['ww_test_corp'],'corpsecret':['test_app_secret']}
    assert calls[2][2]['payload']['touser'] == 'user1|user2'
    assert calls[2][2]['payload']['agentid'] == 1000002
    assert calls[3][0:2] == ('sctapi.ftqq.com','/SCT_test_only.send')
    assert calls[4][0:2] == ('123.push.ft07.com','/send/sctp123tTEST.send')

    wx_id = client.get('/api/notification-channels').json()[0]['id']
    assert client.put(f'/api/notification-channels/{wx_id}', json={
        **configs[0], 'secret':'', 'enabled':False
    }).json()['enabled'] is False
    assert client.post(f'/api/notification-channels/{wx_id}/test').status_code == 200
    assert calls[-1][2]['payload']['appToken'] == 'AT_test_only'
    response = client.post('/api/notification-channels', json={**configs[0], 'provider':'bogus', 'secret':'sensitive_test_value'})
    assert response.status_code == 422 and 'sensitive_test_value' not in response.text
    assert client.delete(f'/api/notification-channels/{wx_id}').status_code == 204
    assert client.post(f'/api/notification-channels/{wx_id}/test').status_code == 404

    # A backup containing only notification credentials must still verify its key.
    backup = client.get('/api/backup/export')
    assert backup.status_code == 200
    assert client.post('/api/backup/inspect', files={'file':('backup.zip',backup.content,'application/zip')}).status_code == 200


def test_sync_queues_both_outcomes_without_repeats_and_isolates_channel_failures(isolated_storage, monkeypatch):
    from app import mail, notifications
    _, _, client, db, service, _ = isolated_storage
    for name, enabled in [('broken', True), ('working', True), ('paused', False)]:
        assert client.post('/api/notification-channels', json={
            'name':name,'provider':'serverchan','secret':f'SCT_test_{name}','enabled':enabled
        }).status_code == 201

    with db.connect() as conn:
        account_id = conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@qq.com','')").lastrowid

    message = EmailMessage()
    message['Subject'] = '发票'
    message.set_content('附件包含两张发票')
    message.add_attachment(invoice_xml('12345678901234567890'),maintype='application',subtype='xml',filename='private.xml')
    message.add_attachment(b'<Invoice/>',maintype='application',subtype='xml',filename='needs-review.xml')

    class Mailbox:
        def uid(self, *_):
            return 'OK', [(None,message.as_bytes())]

    sent = []

    def provider(host, path, *, form):
        if 'broken' in path:
            raise notifications.PushError('模拟平台超时')
        sent.append(form)
        return {'code':0}

    monkeypatch.setattr(notifications, '_request_json', provider)
    first = mail.process_message(Mailbox(), account_id, '1', 'test', 'INBOX')
    assert len(first['created_ids']) == 2
    old = (first['email_id'], first['status'])
    mail.process_message(Mailbox(), account_id, '1', 'test', 'INBOX', old)
    mail.process_message(Mailbox(), account_id, '1', 'test', 'INBOX', old, force=True)
    assert client.post('/api/invoices/upload', files={
        'files':('local.xml',invoice_xml('12345678901234567891'),'application/xml')
    }).status_code == 200

    assert all(notifications.deliver_next() for _ in range(4))
    assert not notifications.deliver_next()
    assert [item['title'] for item in sent] == ['票序 · 收到新发票','票序 · 发票自动识别失败']
    assert '1 张' in sent[0]['desp']
    assert all('private.xml' not in str(item) for item in sent)
    assert '12345678901234567890' in sent[0]['desp']
    assert '含税金额：¥200.35' in sent[0]['desp']
    assert '购买方税号：未识别' in sent[0]['desp']
    assert [channel['last_result'] for channel in client.get('/api/notification-channels').json()] == [
        '模拟平台超时', '平台已受理', None
    ]


def test_notification_fields_snapshot_and_multiformat_dedup_on_all_channels(isolated_storage,monkeypatch):
    from app import mail, notifications
    from test_backup import discounted_invoice_files
    _,_,client,db,service,_=isolated_storage
    configs=[{'name':'wx','provider':'wxpusher','secret':'AT_test_details','uids':'UID_test_details'},
             {'name':'work','provider':'wecom','secret':'test_details_secret','corp_id':'ww_details','agent_id':'1000001','users':'test_user'},
             {'name':'sc','provider':'serverchan','secret':'SCT_test_details'}]
    for config in configs: assert client.post('/api/notification-channels',json=config).status_code==201
    fields,pdf,xml=discounted_invoice_files()
    xml=xml.replace(b'</BuyerInformation>',b'<BuyerIdNum>TTTTTTTTTTTTTTTTTT</BuyerIdNum></BuyerInformation>')
    xml=xml.replace(b'</SellerInformation>',b'<SellerIdNum>SSSSSSSSSSSSSSSSSS</SellerIdNum></SellerInformation>')
    partial='<Invoice><BuyerName>待核对购买方</BuyerName><BuyerIdNum>FFFFFFFFFFFFFFFFFF</BuyerIdNum></Invoice>'.encode()
    message=EmailMessage();message['Subject']='发票';message.set_content('不应推送的邮件正文')
    for name,content,kind in [('a.xml',xml,'xml'),('a.pdf',pdf,'pdf'),('partial.xml',partial,'xml')]:
        message.add_attachment(content,maintype='application',subtype=kind,filename=name)
    class Mail:
        def uid(self,*args): return 'OK',[(None,message.as_bytes())]
    with db.connect() as conn:
        account=conn.execute("INSERT INTO accounts(name,email,secret) VALUES('test','test@example.test','')").lastrowid
    result=mail.process_message(Mail(),account,'1','test','INBOX')
    assert len(result['created_ids'])==2
    records=client.get('/api/invoices').json()
    recognized=next(row for row in records if row['recognition_status']=='已识别')
    assert recognized['buyer_tax_id']=='T'*18 and recognized['seller_tax_id']=='S'*18
    assert recognized['original_name']=='a.pdf' and len(recognized['other_originals'])==1
    # An edit while queued must not change what was actually received.
    with db.connect() as conn:
        conn.execute("UPDATE invoices SET buyer='后续修改',buyer_tax_id='',amount_cents=99999 WHERE id=?",(recognized['id'],))
    sent=[]
    def provider(host,path,**body):
        if '/gettoken?' in path: return {'errcode':0,'access_token':'fake_details_token','expires_in':7200}
        if host=='wxpusher.zjiecode.com': sent.append(('wx',body['payload']['summary'],body['payload']['content']));return {'code':1000}
        if host=='qyapi.weixin.qq.com':
            content=body['payload']['text']['content'];assert len(content.encode())<=2048
            sent.append(('work','',content));return {'errcode':0}
        sent.append(('sc',body['form']['title'],body['form']['desp']));return {'code':0}
    monkeypatch.setattr(notifications,'_request_json',provider)
    assert all(notifications.deliver_next() for _ in range(6))
    assert not notifications.deliver_next()
    for name in ['wx','work','sc']:
        messages=[(title,body) for channel,title,body in sent if channel==name]
        assert len(messages)==2
        successful=next(body for title,body in messages if '新增 1 张已识别发票' in body)
        for expected in [fields['buyer'],fields['seller'],'T'*18,'S'*18,'含税金额：¥16.90','开票日期：2026-11-09',fields['invoice_number']]:
            assert expected in successful
        assert successful.count('发票 1')==1 and '发票 2' not in successful
        failed=next(body for title,body in messages if '自动识别失败' in title+body)
        for expected in ['待核对购买方','F'*18,'含税金额：未识别','销售方：未识别','信息待核对']:
            assert expected in failed
        assert all('后续修改' not in body and '不应推送的邮件正文' not in body and '999.99' not in body for _,body in messages)


def test_notification_length_limits_keep_complete_details_and_safe_markdown(isolated_storage,monkeypatch):
    from app import notifications
    _,_,client,_,_,_=isolated_storage
    item={'buyer':'公司'*60,'seller':'商户'*60,'buyer_tax_id':'T'*18,'seller_tax_id':'S'*18,
          'amount_cents':0,'invoice_date':'2026-11-09','invoice_number':'12345678901234567890'}
    title='票序 · 收到新发票'
    body=notifications._message('received',7,[item]*7,2048-len((title+'\n').encode()))
    assert len((title+'\n'+body).encode())<=2048
    import re
    shown=body.count('发票号码：12345678901234567890')
    remaining=int(re.search(r'其余 (\d+) 张明细',body)[1])
    assert 0<shown<7 and shown+remaining==7
    assert body.count('含税金额：¥0.00')==shown
    channel=client.post('/api/notification-channels',json={'name':'test','provider':'serverchan','secret':'SCT_test_safe'}).json()
    sent=[]
    monkeypatch.setattr(notifications,'_request_json',lambda *args,**kwargs:(sent.append(kwargs['form']) or {'code':0}))
    with notifications.connect() as conn: row=conn.execute('SELECT * FROM notification_channels WHERE id=?',(channel['id'],)).fetchone()
    raw=notifications._invoice_detail({**item,'buyer':'![image](https://example.test/track)\n含税金额：伪造'},1)
    assert 'track) 含税金额' in raw
    notifications._send(row,title,raw)
    assert r'\!\[image\]' in sent[0]['desp'] and r'\<' not in sent[0]['desp']
    assert sent[0]['desp'].count('含税金额：¥0.00')==1


def test_notification_schema_migration_and_backup_keep_details(isolated_storage):
    from app import notifications
    _,_,client,db,service,_=isolated_storage
    ident=service.import_file('tax.xml',invoice_xml('12345678901234567890'),'本地上传')[0]
    with db.connect() as conn:
        for key in ['buyer_tax_id','seller_tax_id']: conn.execute(f'ALTER TABLE invoices DROP COLUMN {key}')
        conn.execute('ALTER TABLE notification_deliveries DROP COLUMN details')
    db.init_db();db.init_db()
    with db.connect() as conn:
        row=conn.execute('SELECT * FROM invoices WHERE id=?',(ident,)).fetchone()
        assert row['buyer_tax_id']==row['seller_tax_id']=='' and row['amount_cents']==20035
        conn.execute('UPDATE invoices SET buyer_tax_id=?,seller_tax_id=? WHERE id=?',('T'*18,'S'*18,ident))
    account=client.post('/api/accounts',json={'name':'测试邮箱','email':'backup-details@qq.com','auth_code':'fake'}).json()['id']
    client.post('/api/notification-channels',json={'name':'test','provider':'serverchan','secret':'SCT_test_backup'})
    with db.connect() as conn:
        email_id=conn.execute("INSERT INTO emails(account_id,uidvalidity,uid,sender,subject) VALUES(?,?,?,?,?)",(account,'test','1','','发票')).lastrowid
        notifications.queue_notification(conn,email_id,'received',[ident])
    # Tax IDs persist through the same full backup/restore path as invoice fields.
    archive=client.get('/api/backup/export');assert archive.status_code==200
    with db.connect() as conn: conn.execute("UPDATE invoices SET buyer_tax_id='',seller_tax_id='' WHERE id=?",(ident,))
    assert client.post('/api/backup/restore',files={'file':('backup.zip',archive.content,'application/zip')}).status_code==200
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code==200
    row=client.get('/api/invoices').json()[0]
    assert row['buyer_tax_id']=='T'*18 and row['seller_tax_id']=='S'*18
    import json
    with db.connect() as conn:
        detail=json.loads(conn.execute('SELECT details FROM notification_deliveries').fetchone()[0])[0]
        assert detail['buyer_tax_id']=='T'*18 and detail['seller_tax_id']=='S'*18


def test_tax_ids_extracted_from_pdf_xml_ofd_and_ai(tmp_path):
    import io,json,zipfile,fitz
    from app.recognition import recognize
    from app.ai import parse_answer
    from test_backup import discounted_invoice_files
    fields,pdf,xml=discounted_invoice_files()
    xml=xml.replace(b'</BuyerInformation>',b'<BuyerIdNum>TTTTTTTTTTTTTTTTTT</BuyerIdNum></BuyerInformation>')
    xml=xml.replace(b'</SellerInformation>',b'<SellerIdNum>SSSSSSSSSSSSSSSSSS</SellerIdNum></SellerInformation>')
    document=fitz.open(stream=pdf,filetype='pdf');page=document[0]
    for x,value in [(41,'T'*18),(326,'S'*18)]:
        page.insert_text((x,130),'统一社会信用代码/纳税人识别号:',fontname='china-s',fontsize=8)
        page.insert_text((x+139,130),value,fontname='helv',fontsize=8)
    pdf=document.tobytes();document.close()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('OFD.xml','<OFD/>');archive.writestr('Doc_0/CustomTags.xml',xml)
    for ext,content in [('pdf',pdf),('xml',xml),('ofd',output.getvalue())]:
        path=tmp_path/('tax.'+ext);path.write_bytes(content)
        result,reason=recognize(path)
        assert not reason
        assert all(result[key]==value for key,value in fields.items())
        assert result['buyer_tax_id']=='T'*18 and result['seller_tax_id']=='S'*18
    conflict=tmp_path/'conflicting-tax.xml'
    conflict.write_bytes(xml.replace(b'</EInvoice>',b'<BuyerIdNum>CCCCCCCCCCCCCCCCCC</BuyerIdNum></EInvoice>'))
    result,reason=recognize(conflict)
    assert not reason and result['buyer_tax_id']=='' and result['seller_tax_id']=='S'*18
    for buyer,seller,expected in [(' tttttttttttttttttt ','S'*18,True),('123','fake-invalid',False)]:
        result=parse_answer(json.dumps({'is_invoice':True,'buyer':'测试公司','buyer_tax_id':buyer,'seller_tax_id':seller}))
        if expected: assert result['buyer_tax_id']=='T'*18 and result['seller_tax_id']=='S'*18
        else: assert not result.get('buyer_tax_id') and not result.get('seller_tax_id')


def test_tax_ids_are_optional_and_company_edits_clear_previous_ids(isolated_storage):
    _,_,client,db,service,_=isolated_storage
    content=invoice_xml('12345678901234567890').replace(b'</Invoice>',b'<Gmfnsrsbh>TTTTTTTTTTTTTTTTTT</Gmfnsrsbh><Xsfnsrsbh>SSSSSSSSSSSSSSSSSS</Xsfnsrsbh></Invoice>')
    ident=service.import_file('tax.xml',content,'本地上传')[0]
    item=client.get('/api/invoices').json()[0]
    assert item['recognition_status']=='已识别' and item['buyer_tax_id']=='T'*18
    assert client.patch(f'/api/invoices/{ident}',json={**item,'buyer':'另一家公司'}).status_code==200
    item=client.get('/api/invoices').json()[0]
    assert item['buyer_tax_id']=='' and item['seller_tax_id']=='S'*18
    assert item['recognition_status']=='已识别'
    assert client.post(f'/api/invoices/{ident}/retry').status_code==200
    item=client.get('/api/invoices').json()[0]
    assert item['buyer']=='另一家公司' and item['buyer_tax_id']==''


def test_ai_does_not_attach_tax_id_from_a_different_company(monkeypatch):
    from app import service, ai
    monkeypatch.setattr(ai,'settings',lambda **kwargs:{'auto_enabled':True})
    monkeypatch.setattr(ai,'recognize_ai',lambda *args:{'buyer':'票面原公司','buyer_tax_id':'T'*18})
    fields,reason,suggestion=service.assist(None,{'buyer':'人工修正的公司'},'请核对')
    assert fields['buyer']=='人工修正的公司' and not fields.get('buyer_tax_id')
    assert suggestion['buyer_tax_id']=='T'*18 and reason


def test_merge_preserves_tax_ids_and_rejects_known_tax_conflicts(isolated_storage):
    _,_,client,db,service,_=isolated_storage
    from test_backup import merge_payload
    original=invoice_xml('12345678901234567890')
    first=service.import_file('first.xml',original,'本地上传')[0]
    extra=b'<Gmfnsrsbh>TTTTTTTTTTTTTTTTTT</Gmfnsrsbh><Xsfnsrsbh>SSSSSSSSSSSSSSSSSS</Xsfnsrsbh>'
    second=service.import_file('second.xml',original.replace(b'</Invoice>',extra+b'</Invoice>'),'本地上传')[0]
    assert client.post(f'/api/invoices/{second}/merge',json=merge_payload(client,second,first)).status_code==200
    item=client.get('/api/invoices').json()[0]
    assert item['buyer_tax_id']=='T'*18 and item['seller_tax_id']=='S'*18
    third=service.import_file('third.xml',original.replace(b'</Invoice>',extra.replace(b'TTTTTTTTTTTTTTTTTT',b'CCCCCCCCCCCCCCCCCC')+b'</Invoice>'),'本地上传')[0]
    assert client.post(f'/api/invoices/{third}/merge',json=merge_payload(client,third,first)).status_code==409
    assert len(client.get('/api/invoices').json())==2
