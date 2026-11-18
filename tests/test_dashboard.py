from datetime import date

from test_backup import isolated_storage


def test_dashboard_counts_only_recognized_invoices_and_groups_by_issue_month(isolated_storage, monkeypatch):
    from app import main

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 2, 10)

    monkeypatch.setattr(main, 'date', FixedDate)
    _,_,client,db,_,_ = isolated_storage
    records = [
        ('旧发票','2025-08-31','2026-02-10T10:00:00',200,0,'已识别'),
        ('去年九月','2025-09-01','2026-02-10T11:00:00',100,1,'已识别'),
        ('本月发票','2026-02-10','2026-02-10T09:00:00',300,0,'已识别'),
        ('红冲','2026-02-08','2026-02-10T09:00:00',-50,0,'已识别'),
        ('识别失败','2026-02-10','2026-02-10T12:00:00',9999,0,'识别失败'),
        ('未来日期','2026-02-11','2026-02-10T08:00:00',400,0,'已识别'),
    ]
    with db.connect() as conn:
        conn.executemany('''INSERT INTO invoices(sha256,original_name,stored_name,source,seller,invoice_date,
            imported_at,amount_cents,reimbursed,recognition_status) VALUES(?,?,?, '本地上传',?,?,?,?,?,?)''',
            [(str(index), f'{index}.pdf', f'{index}.pdf', *row) for index,row in enumerate(records,1)])
        conn.execute("INSERT INTO groups(name,created_at) VALUES ('示例报销组','2026-02-10')")
        conn.execute("INSERT INTO tags(name) VALUES ('已标记')")
        conn.execute('INSERT INTO invoice_tags(invoice_id,tag_id) VALUES (3,1)')
    response=client.get('/api/dashboard')
    assert response.status_code == 200
    dashboard=response.json()
    assert dashboard['summary'] == client.get('/api/summary').json() == {
        'failed_invoices':1,'pending_emails':0,'invoices':5,'total_amount_cents':950,
        'unreimbursed':4,'unreimbursed_amount_cents':850,'untagged_invoices':4,
    }
    assert [month['month'] for month in dashboard['monthly']] == ['2025-09','2025-10','2025-11','2025-12','2026-01','2026-02']
    assert [month['amount_cents'] for month in dashboard['monthly']] == [100,0,0,0,0,250]
    assert dashboard['monthly'][-1]['count'] == 2
    assert [item['seller'] for item in dashboard['recent']] == ['去年九月','旧发票','红冲','本月发票','未来日期']
    assert dashboard['group_count'] == 1


def test_dashboard_empty_database_returns_zeroes_and_no_recent_invoices(isolated_storage):
    _,_,client,_,_,_ = isolated_storage
    dashboard=client.get('/api/dashboard').json()
    assert all(value == 0 for value in dashboard['summary'].values())
    assert len(dashboard['monthly']) == 6
    assert all(month['amount_cents'] == month['count'] == 0 for month in dashboard['monthly'])
    assert dashboard['recent'] == [] and dashboard['group_count'] == 0
