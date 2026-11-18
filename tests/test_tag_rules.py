from test_backup import isolated_storage, invoice_xml


def upload(client, suffix, buyer='示例购买方', seller='示例销售方'):
    xml = invoice_xml(f'2026111800000000{suffix:04d}')
    xml = xml.decode().replace('示例购买方', buyer).replace('示例销售方', seller).encode()
    result = client.post('/api/invoices/upload', files={'files':('example.xml', xml, 'application/xml')})
    assert result.status_code == 200
    return result.json()[0]['id']


def tags_for(client, invoice_id):
    invoice = next(row for row in client.get('/api/invoices').json() if row['id'] == invoice_id)
    return {tag['id'] for tag in invoice['tags']}


def test_recognized_upload_matches_only_complete_buyer_and_applies_all_rules(isolated_storage):
    _,_,client,_,_,_ = isolated_storage
    tag_ids = [client.post('/api/tags', json={'name':name}).json()['id'] for name in ('项目','差旅')]
    for tag_id in tag_ids:
        result = client.post('/api/tag-rules',json={'buyer':'  示例购买方  ','tag_id':tag_id})
        assert result.status_code == 201 and result.json()['buyer'] == '示例购买方'
    matching = upload(client,1)
    same_prefix = upload(client,2,buyer='示例购买方分公司')
    seller_only = upload(client,3,buyer='其他公司',seller='示例购买方')
    assert tags_for(client,matching) == set(tag_ids)
    assert tags_for(client,same_prefix) == tags_for(client,seller_only) == set()
    results = client.get('/api/invoices',params={'limit':50,'tag_id':tag_ids[0]}).json()
    assert [item['id'] for item in results['items']] == [matching]


def test_rule_backfill_is_opt_in_and_deleting_a_rule_keeps_existing_labels(isolated_storage):
    _,_,client,_,_,_ = isolated_storage
    existing = upload(client,1)
    original = client.post('/api/tags',json={'name':'手动添加'}).json()['id']
    future = client.post('/api/tags',json={'name':'后续发票'}).json()['id']
    backfill = client.post('/api/tags',json={'name':'现有发票'}).json()['id']
    assert client.put(f'/api/invoices/{existing}/tags',json={'tag_ids':[original]}).status_code == 200
    rule = client.post('/api/tag-rules',json={'buyer':'示例购买方','tag_id':future}).json()
    assert rule['applied'] == 0 and tags_for(client,existing) == {original}
    payload = {'buyer':'示例购买方','tag_id':backfill,'apply_existing':True}
    filled = client.post('/api/tag-rules',json=payload).json()
    assert filled['applied'] == 1 and tags_for(client,existing) == {original,backfill}
    assert client.post('/api/tag-rules',json=payload).status_code == 409
    assert client.delete(f"/api/tag-rules/{filled['id']}").status_code == 204
    assert tags_for(client,existing) == {original,backfill}
    assert tags_for(client,upload(client,2)) == {future}
    assert client.delete(f'/api/tags/{future}').status_code == 204
    assert client.get('/api/tag-rules').json() == []


def test_manual_correction_applies_rule_without_readding_on_unrelated_edit(isolated_storage):
    _,_,client,_,_,_ = isolated_storage
    tag_id = client.post('/api/tags',json={'name':'已核对'}).json()['id']
    assert client.post('/api/tag-rules',json={'buyer':' ','tag_id':tag_id}).status_code == 422
    assert client.post('/api/tag-rules',json={'buyer':'示例购买方','tag_id':999}).status_code == 404
    assert client.post('/api/tag-rules',json={'buyer':'示例购买方','tag_id':tag_id}).status_code == 201
    invoice = upload(client,1,buyer='待修正抬头')
    assert tags_for(client,invoice) == set()
    fields = {'buyer':'示例购买方','seller':'示例销售方','amount_cents':20035,
              'invoice_date':'2026-10-15','invoice_number':'20261118000000000001'}
    assert client.patch(f'/api/invoices/{invoice}',json=fields).status_code == 200
    assert tags_for(client,invoice) == {tag_id}
    assert client.put(f'/api/invoices/{invoice}/tags',json={'tag_ids':[]}).status_code == 200
    assert client.patch(f'/api/invoices/{invoice}',json={**fields,'amount_cents':20036}).status_code == 200
    assert tags_for(client,invoice) == set()


def test_deleting_an_old_rule_id_never_deletes_a_replacement_rule(isolated_storage):
    _,_,client,_,_,_ = isolated_storage
    tag_id = client.post('/api/tags',json={'name':'公司报销'}).json()['id']
    previous = client.post('/api/tag-rules',json={'buyer':'旧抬头','tag_id':tag_id}).json()
    url = f"/api/tag-rules/{previous['id']}"
    assert client.delete(url).status_code == 204
    replacement = client.post('/api/tag-rules',json={'buyer':'新抬头','tag_id':tag_id}).json()
    assert replacement['id'] != previous['id']
    assert client.delete(url).status_code == 404
    assert [rule['id'] for rule in client.get('/api/tag-rules').json()] == [replacement['id']]


def test_saving_stale_tag_editor_preserves_automatic_backfill(isolated_storage):
    _,_,client,_,_,_ = isolated_storage
    invoice = upload(client,1)
    url = f'/api/invoices/{invoice}/tags'
    original,manual,automatic = [client.post('/api/tags',json={'name':name}).json()['id']
                                 for name in ('原标签','手动新选','自动补打')]
    assert client.put(url,json={'tag_ids':[original]}).status_code == 200
    base = list(tags_for(client,invoice))
    rule = client.post('/api/tag-rules',json={'buyer':'示例购买方','tag_id':automatic,'apply_existing':True})
    assert rule.json()['applied'] == 1
    payload = {'base_tag_ids':base,'tag_ids':[manual]}
    for _ in range(2):
        saved = client.put(url,json=payload)
        assert saved.status_code == 200
        assert {tag['id'] for tag in saved.json()['tags']} == {manual,automatic}
