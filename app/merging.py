"""Combine invoice records while preserving every original and mail association."""
from pathlib import Path


def merge_records(db, source, target):
    db.execute('INSERT INTO invoice_originals(invoice_id,sha256,original_name,stored_name) VALUES(?,?,?,?)',
               (target['id'],source['sha256'],source['original_name'],source['stored_name']))
    db.execute('UPDATE invoice_originals SET invoice_id=? WHERE invoice_id=?',(target['id'],source['id']))
    db.execute('INSERT OR IGNORE INTO invoice_emails(invoice_id,email_id) SELECT ?,email_id FROM invoice_emails WHERE invoice_id=?',
               (target['id'],source['id']))
    db.execute('UPDATE invoices SET email_id=COALESCE(email_id,?),has_local_upload=MAX(has_local_upload,?) WHERE id=?',
               (source['email_id'],source['has_local_upload'],target['id']))
    db.execute('UPDATE invoices SET duplicate_of=? WHERE duplicate_of=?',(target['id'],source['id']))
    db.execute('DELETE FROM invoices WHERE id=?',(source['id'],))
    if Path(target['stored_name']).suffix.lower() != '.pdf':
        originals = db.execute('SELECT * FROM invoice_originals WHERE invoice_id=? ORDER BY id',(target['id'],)).fetchall()
        pdf = next((row for row in originals if Path(row['stored_name']).suffix.lower()=='.pdf'), None)
        if pdf:
            db.execute('UPDATE invoice_originals SET sha256=?,original_name=?,stored_name=? WHERE id=?',
                       (target['sha256'],target['original_name'],target['stored_name'],pdf['id']))
            db.execute('UPDATE invoices SET sha256=?,original_name=?,stored_name=? WHERE id=?',
                       (pdf['sha256'],pdf['original_name'],pdf['stored_name'],target['id']))
