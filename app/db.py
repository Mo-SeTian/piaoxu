import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DATA = Path(os.environ.get('PIAOXU_DATA', 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = Path(os.environ.get('PIAOXU_CONFIG', str(DATA))).resolve()
CONFIG.mkdir(parents=True, exist_ok=True)
FILES = DATA / 'files'
FILES.mkdir(exist_ok=True)
DB = CONFIG / 'piaoxu.sqlite3'

@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT, last_error TEXT, uidvalidity TEXT);
        CREATE TABLE IF NOT EXISTS emails(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, uidvalidity TEXT NOT NULL, uid TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL, received_at TEXT, body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '普通邮件', UNIQUE(account_id, uidvalidity, uid));
        CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL, stored_name TEXT NOT NULL, source TEXT NOT NULL, email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL, imported_at TEXT NOT NULL, buyer TEXT NOT NULL DEFAULT '', seller TEXT NOT NULL DEFAULT '', amount_cents INTEGER, invoice_date TEXT NOT NULL DEFAULT '', invoice_number TEXT NOT NULL DEFAULT '', recognition_status TEXT NOT NULL DEFAULT '待处理', failure_reason TEXT NOT NULL DEFAULT '', reimbursed INTEGER NOT NULL DEFAULT 0, export_name TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS email_attachments(id INTEGER PRIMARY KEY, email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE, name TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS email_sync_failures(account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, uidvalidity TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY(account_id,uidvalidity,uid));
        CREATE TABLE IF NOT EXISTS invoice_emails(invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE, email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE, PRIMARY KEY(invoice_id,email_id));
        CREATE TABLE IF NOT EXISTS invoice_originals(id INTEGER PRIMARY KEY,invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,sha256 TEXT NOT NULL UNIQUE,original_name TEXT NOT NULL,stored_name TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS invoice_originals_invoice ON invoice_originals(invoice_id);
        CREATE INDEX IF NOT EXISTS invoice_emails_by_email ON invoice_emails(email_id,invoice_id);
        CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, reimbursed INTEGER NOT NULL DEFAULT 0, reimbursed_at TEXT);
        CREATE TABLE IF NOT EXISTS group_invoices(group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE, invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id), PRIMARY KEY(group_id, invoice_id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, provider TEXT NOT NULL,
            enabled INTEGER NOT NULL, options TEXT NOT NULL, secret TEXT NOT NULL,
            last_result TEXT, last_attempt_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
            channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
            event TEXT NOT NULL, invoice_count INTEGER NOT NULL, pending INTEGER NOT NULL DEFAULT 1,
            UNIQUE(email_id, channel_id, event)
        );
        CREATE INDEX IF NOT EXISTS notification_pending ON notification_deliveries(id) WHERE pending=1;
        ''')

        invoice_cols = {r['name'] for r in db.execute('PRAGMA table_info(invoices)')}
        if 'ai_suggestion' not in invoice_cols:
            db.execute("ALTER TABLE invoices ADD COLUMN ai_suggestion TEXT NOT NULL DEFAULT '{}'")
        if 'has_local_upload' not in invoice_cols:
            db.execute('ALTER TABLE invoices ADD COLUMN has_local_upload INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE invoices SET has_local_upload=1 WHERE source='本地上传'")
        if 'duplicate_of' not in invoice_cols:
            db.execute('ALTER TABLE invoices ADD COLUMN duplicate_of INTEGER REFERENCES invoices(id)')
        if 'duplicate_state' not in invoice_cols:
            db.execute("ALTER TABLE invoices ADD COLUMN duplicate_state TEXT NOT NULL DEFAULT ''")
        cols = {r['name'] for r in db.execute('PRAGMA table_info(emails)')}
        if 'folder' not in cols:
            db.execute("ALTER TABLE emails ADD COLUMN folder TEXT NOT NULL DEFAULT 'INBOX'")
        if 'html_body' not in cols:
            db.execute("ALTER TABLE emails ADD COLUMN html_body TEXT NOT NULL DEFAULT ''")
        if 'html_checked' not in cols:
            db.execute('ALTER TABLE emails ADD COLUMN html_checked INTEGER NOT NULL DEFAULT 0')
        if 'html_version' not in cols:
            db.execute('ALTER TABLE emails ADD COLUMN html_version INTEGER NOT NULL DEFAULT 0')
        db.execute('INSERT OR IGNORE INTO invoice_emails(invoice_id,email_id) SELECT id,email_id FROM invoices WHERE email_id IS NOT NULL')
