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
    assert all('private.xml' not in str(item) and '12345678901234567890' not in str(item) for item in sent)
    assert [channel['last_result'] for channel in client.get('/api/notification-channels').json()] == [
        '模拟平台超时', '平台已受理', None
    ]
