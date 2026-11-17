import io

from fastapi.testclient import TestClient

from test_backup import isolated_storage, invoice_xml


def add_invoice(client, number, suffix=''):
    content = invoice_xml(number) + suffix.encode()
    ident = client.post('/api/invoices/upload', files={'files':('invoice.xml',content,'application/xml')}).json()[0]['id']
    return ident,content


def test_tags_persist_rename_and_remove_without_changing_invoice(isolated_storage):
    config,data,client,db,service,backup = isolated_storage
    ident,original = add_invoice(client,'99000000000000000031')
    before = client.get('/api/invoices').json()[0]
    assert before['tags'] == []
    tag = client.post('/api/tags',json={'name':'  杭州项目年度交流会  '}).json()
    assert tag['name'] == '杭州项目年度交流会' and tag['invoice_count'] == 0
    assert client.post('/api/tags',json={'name':tag['name']}).status_code == 409
    saved = client.put(f'/api/invoices/{ident}/tags',json={'tag_ids':[tag['id'],tag['id']]})
    assert saved.json()['tags'] == [{'id':tag['id'],'name':tag['name']}]
    after = client.get('/api/invoices?limit=50').json()['items'][0]
    assert {k:v for k,v in after.items() if k!='tags'} == {k:v for k,v in before.items() if k!='tags'}
    assert client.get('/api/tags').json()[0]['invoice_count'] == 1
    assert client.patch(f"/api/tags/{tag['id']}",json={'name':'项目 <设计稿> & 差旅'}).status_code == 200
    assert client.get('/api/invoices').json()[0]['tags'][0]['name'] == '项目 <设计稿> & 差旅'
    assert client.put(f'/api/invoices/{ident}/tags',json={'tag_ids':[999999]}).status_code == 422
    assert client.get('/api/invoices').json()[0]['tags'][0]['id'] == tag['id']
    group = client.post('/api/groups',json={'name':'报销','ids':[ident]}).json()['id']
    assert client.patch(f'/api/groups/{group}',json={'reimbursed':True}).status_code == 200
    assert client.put(f'/api/invoices/{ident}/tags',json={'tag_ids':[]}).json() == {'tags':[]}
    assert client.get('/api/tags').json()[0]['invoice_count'] == 0
    assert client.put(f'/api/invoices/{ident}/tags',json={'tag_ids':[tag['id']]}).status_code == 200
    assert client.delete(f"/api/tags/{tag['id']}").status_code == 204
    assert client.get('/api/tags').json() == []
    row = client.get('/api/invoices').json()[0]
    assert row['tags'] == [] and row['reimbursed'] == 1
    assert client.get(f'/api/invoices/{ident}/original').content == original


def test_tag_validation_auth_and_unknown_records(isolated_storage):
    config,data,client,db,service,backup = isolated_storage
    for name in ('','   ','a\nb','x'*101):
        assert client.post('/api/tags',json={'name':name}).status_code == 422
    first = client.post('/api/tags',json={'name':'Project'}).json()['id']
    second = client.post('/api/tags',json={'name':'差旅'}).json()['id']
    assert client.post('/api/tags',json={'name':'project'}).status_code == 409
    assert client.patch(f'/api/tags/{second}',json={'name':'PROJECT'}).status_code == 409
    assert client.patch('/api/tags/99999',json={'name':'新名称'}).status_code == 404
    assert client.delete('/api/tags/99999').status_code == 404
    assert client.put('/api/invoices/99999/tags',json={'tag_ids':[first]}).status_code == 404
    assert client.put('/api/invoices/99999/tags',json={'tag_ids':None}).status_code == 422
    from app.main import app
    anonymous = TestClient(app,headers={'X-Piaoxu-Request':'1'})
    assert anonymous.get('/api/tags').status_code == 401
    assert anonymous.post('/api/tags',json={'name':'不可创建'}).status_code == 401
    assert anonymous.delete(f'/api/tags/{first}').status_code == 401
    assert TestClient(app,cookies=client.cookies).post('/api/tags',json={'name':'CSRF'}).status_code == 403


def test_tag_filters_keep_pagination_search_and_day_selection_consistent(isolated_storage):
    config,data,client,db,service,backup = isolated_storage
    first = client.post('/api/tags',json={'name':'差旅'}).json()['id']
    second = client.post('/api/tags',json={'name':'杭州项目'}).json()['id']
    with db.connect() as conn:
        for number in range(1,61):
            digest = f'{number:064x}'
            conn.execute('''INSERT INTO invoices(id,sha256,original_name,stored_name,source,imported_at,seller,amount_cents,invoice_date,recognition_status,reimbursed)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(number,digest,'demo.xml',digest+'.xml','本地上传','2026-11-18 10:00:00','示例商户',number*100,'2026-11-17','已识别',int(number==60)))
            if number<=55: conn.execute('INSERT INTO invoice_tags VALUES(?,?)',(number,first))
            if number%2==0: conn.execute('INSERT INTO invoice_tags VALUES(?,?)',(number,second))
    base=f'/api/invoices?limit=50&tag_id={first}&query=示例&reimbursement=unreimbursed'
    page = client.get(base).json()
    assert page['total'] == 55 and len(page['items']) == 50
    assert len(page['import_day_ids']['2026-11-18']) == 55
    assert all(row['tags'] for row in page['items'])
    last = client.get(base+'&offset=50').json()
    assert len(last['items']) == 5 and not ({r['id'] for r in last['items']} & {r['id'] for r in page['items']})
    selected = client.get(base+'&limit=500&imported_from=2026-11-18&imported_to=2026-11-18').json()
    assert selected['total'] == len(selected['items']) == 55
    union = client.get(f'/api/invoices?limit=500&tag_id={first}&tag_id={second}').json()
    assert union['total'] == 58 and len({r['id'] for r in union['items']}) == 58
    untagged = client.get('/api/invoices?limit=500&untagged=true').json()
    assert {r['id'] for r in untagged['items']} == {57,59}
    assert client.get(f'/api/invoices?limit=500&tag_id={first}&untagged=true').json()['total'] == 57
    assert client.get('/api/invoices?tag_id=0').status_code == 422
    assert client.get('/api/invoices?limit=50&tag_id=99999').json()['total'] == 0


def test_merge_and_backup_preserve_tag_associations(isolated_storage):
    config,data,client,db,service,backup = isolated_storage
    target,_ = add_invoice(client,'99000000000000000041')
    source,_ = add_invoice(client,'99000000000000000041','<!--second original-->')
    tag_ids = [client.post('/api/tags',json={'name':name}).json()['id'] for name in ('差旅','杭州项目')]
    client.put(f'/api/invoices/{target}/tags',json={'tag_ids':[tag_ids[0]]})
    client.put(f'/api/invoices/{source}/tags',json={'tag_ids':tag_ids})
    rows = {r['id']:r for r in client.get('/api/invoices').json()}
    result = client.post(f'/api/invoices/{source}/merge',json={'target_id':target,'sha256':rows[source]['sha256'],'target_sha256':rows[target]['sha256']})
    assert result.status_code == 200
    assert {t['id'] for t in client.get('/api/invoices').json()[0]['tags']} == set(tag_ids)
    assert [t['invoice_count'] for t in client.get('/api/tags').json()] == [1,1]
    archive = io.BytesIO()
    backup.create_backup(archive)
    client.delete(f'/api/tags/{tag_ids[0]}')
    restored = client.post('/api/backup/restore',files={'file':('backup.zip',archive.getvalue(),'application/zip')})
    assert restored.status_code == 200
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code == 200
    assert {t['id'] for t in client.get('/api/invoices').json()[0]['tags']} == set(tag_ids)
    assert len(client.get('/api/tags').json()) == 2


def test_initializing_legacy_storage_adds_tags_without_losing_invoices(isolated_storage):
    config,data,client,db,service,backup = isolated_storage
    ident,_ = add_invoice(client,'99000000000000000051')
    with db.connect() as conn:
        conn.execute('DROP TABLE invoice_tags')
        conn.execute('DROP TABLE tags')
    archive = io.BytesIO()
    backup.create_backup(archive)
    assert client.post('/api/backup/restore',files={'file':('legacy.zip',archive.getvalue(),'application/zip')}).status_code == 200
    assert client.post('/api/auth/login',json={'username':'admin','password':'admin'}).status_code == 200
    db.init_db();db.init_db()
    assert client.get('/api/tags').json() == []
    assert client.get('/api/invoices').json()[0]['id'] == ident
