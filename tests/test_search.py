from test_backup import isolated_storage


def seed_invoices(db, records):
    ids = []
    with db.connect() as conn:
        for index, record in enumerate(records):
            values = {'seller':'海得生示例商贸(深圳)有限公司','buyer':'上海澄远设计有限公司',
                      'amount_cents':12850,'original_name':f'document-{index}.pdf',
                      'invoice_number':f'INVOICE-{index}','reimbursed':0,
                      'imported_at':'2026-11-18T08:00:00', **record}
            cursor = conn.execute('''INSERT INTO invoices
                (sha256,stored_name,source,recognition_status,seller,buyer,amount_cents,original_name,invoice_number,reimbursed,imported_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (f'search-{index}',f'{index}.pdf','本地上传','已识别',
                 *(values[key] for key in ('seller','buyer','amount_cents','original_name','invoice_number','reimbursed','imported_at'))))
            ids.append(cursor.lastrowid)
    return ids


def result_ids(client, query):
    response = client.get('/api/invoices',params={'query':query,'limit':50})
    assert response.status_code == 200
    return {row['id'] for row in response.json()['items']}


def test_names_match_chinese_homophones_without_arbitrary_character_substitution(isolated_storage):
    _,_,client,db,_,_ = isolated_storage
    target,exact,other = seed_invoices(db,[{}, {'seller':'海德商贸有限公司'}, {'seller':'海云科技有限公司'}])
    assert result_ids(client,'海德') == {target,exact}
    assert result_ids(client,'海得生') == {target}
    assert result_ids(client,'海生') == set()
    assert result_ids(client,'德') == {exact}
    assert result_ids(client,'海云') == {other}


def test_joint_search_requires_all_keywords_and_matches_amount_in_yuan(isolated_storage):
    _,_,client,db,_,_ = isolated_storage
    target,other_amount,other_buyer,other_seller,_ = seed_invoices(db,[{},
        {'amount_cents':12851,'original_name':'备注 128.50.pdf'}, {'buyer':'上海明朗设计有限公司'},
        {'seller':'山河咖啡有限公司'}, {'amount_cents':None,'invoice_number':'12850123456'}])
    assert result_ids(client,'海德 128.50 澄远') == {target}
    assert result_ids(client,'成远 ￥128.5 海德') == {target}
    assert result_ids(client,'海德 128.50 不存在') == set()
    assert result_ids(client,'海德 128元') == set()
    assert result_ids(client,'海德 128.51') == {other_amount}
    assert result_ids(client,'128.50') == {target,other_buyer,other_seller}
    assert result_ids(client,'海德 0元') == set()
    assert {row['id'] for row in client.get('/api/invoices',params={'query':'海德 128.50 澄远'}).json()} == {target}


def test_literal_names_files_numbers_and_currency_formats_remain_searchable(isolated_storage):
    _,_,client,db,_,_ = isolated_storage
    target,other,zero = seed_invoices(db,[
        {'seller':'星河科技','buyer':'ACME (Shenzhen) Ltd','amount_cents':123450,
         'original_name':'Travel_100%\\A.pdf','invoice_number':'998877'},
        {'seller':'其他商户','buyer':'其他买方','amount_cents':None},
        {'seller':'免费商户','buyer':'其他买方','amount_cents':0}])
    for query in ('acme （shenzhen）','星河\t1234.5\nACME','￥1,234.50元','1234.5','998877','%','_','\\','１００％'):
        assert result_ids(client,query) == {target}
    assert result_ids(client,'0元') == {zero}
    assert result_ids(client,"%' OR 1=1 --") == set()
    assert result_ids(client,' \t\n ') == {target,other,zero}


def test_phonetic_matching_keeps_syllable_and_field_boundaries(isolated_storage):
    _,_,client,db,_,_ = isolated_storage
    target,_,_ = seed_invoices(db,[{'seller':'西安实业有限公司'},
        {'seller':'先实业有限公司'}, {'seller':'得科技有限公司','buyer':'上海海有限公司'}])
    assert result_ids(client,'西安') == {target}
    assert result_ids(client,'海德') == set()


def test_search_pagination_tags_and_import_day_selection_use_the_same_matches(isolated_storage):
    _,_,client,db,_,_ = isolated_storage
    ids = seed_invoices(db,[{'reimbursed':index%2} for index in range(55)]+[{'amount_cents':999} for _ in range(5)])
    query = '海德 128.50 澄远'
    pages = [client.get('/api/invoices',params={'query':query,'limit':50,'offset':offset}).json() for offset in (0,50)]
    assert [len(page['items']) for page in pages] == [50,5]
    assert all(page['total']==55 and set(page['import_day_ids']['2026-11-18'])==set(ids[:55]) for page in pages)
    assert {row['id'] for page in pages for row in page['items']} == set(ids[:55])
    tag = client.post('/api/tags',json={'name':'联合匹配'}).json()['id']
    with db.connect() as conn:
        conn.executemany('INSERT INTO invoice_tags(invoice_id,tag_id) VALUES(?,?)',[(ident,tag) for ident in ids[:10]])
    params = {'query':query,'limit':500,'tag_id':tag,'reimbursement':'reimbursed',
              'imported_from':'2026-11-18','imported_to':'2026-11-18'}
    selected = client.get('/api/invoices',params=params).json()
    assert selected['total']==5
    assert {row['id'] for row in selected['items']} == set(ids[1:10:2])
    assert set(selected['import_day_ids']['2026-11-18']) == set(ids[1:10:2])
