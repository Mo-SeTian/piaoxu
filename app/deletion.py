"""Delete invoice records and originals without leaving broken references on restart."""
import os
import re
from .db import FILES, connect
from .service import DUPLICATE_REASON, duplicate_invoice

PREFIX = '.piaoxu-delete-'
STORED_NAME = re.compile(r'^[0-9a-f]{64}\.(?:pdf|ofd|xml|jpg|jpeg|png)$')


def recover_deletions():
    with connect() as db:
        for staged in FILES.glob(PREFIX + '*'):
            name = staged.name[len(PREFIX):]
            if not STORED_NAME.fullmatch(name):
                continue
            original = FILES / name
            if db.execute('SELECT 1 FROM invoices WHERE stored_name=?', (name,)).fetchone():
                if original.exists():
                    raise RuntimeError('待恢复的发票原件与现有文件冲突')
                os.replace(staged, original)
            else:
                staged.unlink()


def delete_invoices(ids):
    ids = sorted(set(ids))
    marks = ','.join('?' for _ in ids)
    staged = []
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(f'SELECT id,stored_name FROM invoices WHERE id IN ({marks})', ids).fetchall()
            if len(rows) != len(ids):
                raise ValueError('部分发票不存在，请刷新后重试')
            affected = [row for row in db.execute(f'SELECT * FROM invoices WHERE duplicate_of IN ({marks})', ids) if row['id'] not in ids]
            email_ids = {row['email_id'] for row in db.execute(f'SELECT DISTINCT email_id FROM invoice_emails WHERE invoice_id IN ({marks})', ids)}
            for row in affected:
                email_ids.update(item['email_id'] for item in db.execute('SELECT email_id FROM invoice_emails WHERE invoice_id=?', (row['id'],)))
            for row in rows:
                name = row['stored_name']
                if not STORED_NAME.fullmatch(name):
                    raise ValueError('发票原件路径无效，无法删除')
                original = FILES / name
                target = FILES / (PREFIX + name)
                if target.exists():
                    raise ValueError('存在未完成的删除操作，请重启服务后重试')
                if original.exists():
                    os.replace(original, target)
                    staged.append((original, target))
            db.execute(f'UPDATE invoices SET duplicate_of=NULL WHERE duplicate_of IN ({marks})', ids)
            db.execute(f'DELETE FROM group_invoices WHERE invoice_id IN ({marks})', ids)
            db.execute(f'DELETE FROM invoices WHERE id IN ({marks})', ids)
            for row in affected:
                duplicate = duplicate_invoice(db, row['invoice_number'], row['id'])
                parts = [part for part in row['failure_reason'].split('；') if part and part != DUPLICATE_REASON]
                if duplicate:
                    parts.append(DUPLICATE_REASON)
                reason = '；'.join(parts)
                db.execute('UPDATE invoices SET duplicate_of=?,duplicate_state=?,failure_reason=?,recognition_status=? WHERE id=?',
                           (duplicate['id'] if duplicate else None, 'pending' if duplicate else '', reason, '识别失败' if reason else '已识别', row['id']))
            for email_id in email_ids:
                statuses = db.execute('SELECT i.recognition_status,i.duplicate_state FROM invoices i JOIN invoice_emails ie ON i.id=ie.invoice_id WHERE ie.email_id=?', (email_id,)).fetchall()
                state = ('识别失败' if any(row['recognition_status']=='识别失败' and row['duplicate_state']!='confirmed' for row in statuses)
                         else '重复发票' if any(row['duplicate_state']=='confirmed' for row in statuses)
                         else '已识别' if statuses else '发票已删除')
                db.execute("UPDATE emails SET status=? WHERE id=? AND status NOT IN ('导入失败','待处理','发票链接待处理')", (state, email_id))
    except Exception:
        for original, target in reversed(staged):
            if target.exists():
                os.replace(target, original)
        raise
    remaining = 0
    for _, target in staged:
        try:
            target.unlink()
        except OSError:
            remaining += 1
    return {'deleted':len(rows), 'cleanup_pending':remaining}
