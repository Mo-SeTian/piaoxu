import hashlib
import json
from contextlib import contextmanager
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from cryptography.fernet import Fernet
from .db import CONFIG, FILES, connect
from .recognition import recognize
from .formats import validate


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


_key_lock = threading.Lock()

def cipher():
    with _key_lock:
        return _load_cipher()

def _load_cipher():
    keyfile = CONFIG / '.mail-key'
    if not keyfile.exists():
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(keyfile.read_bytes())


_import_guard = threading.Lock()
_import_locks = {}

@contextmanager
def import_lock(digest):
    with _import_guard:
        entry = _import_locks.setdefault(digest, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _import_guard:
            entry[1] -= 1
            if not entry[1]:
                del _import_locks[digest]


def assist(path, fields, reason, force=False):
    from .ai import recognize_ai, settings
    try:
        config = settings()
        if not force and (not reason or not config['auto_enabled']):
            return fields, reason, {}
        config = settings(include_key=True)
        suggestion = recognize_ai(path, config)
        combined = dict(fields)
        for key, value in suggestion.items():
            if combined.get(key) in (None, '') and value not in (None, ''):
                combined[key] = value
        return combined, 'AI 建议已生成，请人工核对', suggestion
    except Exception:
        if force:
            raise
        return fields, (reason + '；' if reason else '') + 'AI 辅助失败，请检查设置后手动重试', {}


# The cache contains only server-generated recognition results, keyed by file bytes.
_preview_cache = OrderedDict()
_preview_lock = threading.Lock()

def cache_preview(digest, fields, reason):
    with _preview_lock:
        _preview_cache[digest] = (time.monotonic(), dict(fields), reason)
        _preview_cache.move_to_end(digest)
        while len(_preview_cache) > 500:
            _preview_cache.popitem(last=False)

def take_preview(digest):
    with _preview_lock:
        item = _preview_cache.pop(digest, None)
    if item and time.monotonic() - item[0] < 3600:
        return item[1], item[2]
    return None

def import_file(name, content, source, email_id=None):
    ext = validate(name, content)
    digest = hashlib.sha256(content).hexdigest()
    with import_lock(digest):
        return _import_file_locked(name, content, source, email_id, ext, digest)


def duplicate_invoice(db, number, exclude_id=0):
    if not number: return None
    return db.execute('SELECT id,original_name FROM invoices WHERE invoice_number=? AND id<>? AND (duplicate_of IS NULL OR duplicate_of<>?) ORDER BY id LIMIT 1',(number,exclude_id,exclude_id)).fetchone()


DUPLICATE_REASON = '发票号码与已有发票相同，请核对是否重复'
UNVERIFIED_REASON = '未识别发票号码，无法判断是否重复，请核对'

def duplicate_reason(reason):
    return (reason + '；' if reason else '') + DUPLICATE_REASON


def _import_file_locked(name, content, source, email_id, ext, digest):
    with connect() as db:
        found = db.execute('SELECT id FROM invoices WHERE sha256=? UNION ALL SELECT invoice_id AS id FROM invoice_originals WHERE sha256=?', (digest,digest)).fetchone()
        if found:
            if email_id is not None:
                db.execute('INSERT OR IGNORE INTO invoice_emails(invoice_id,email_id) VALUES(?,?)', (found['id'], email_id))
                db.execute('UPDATE invoices SET email_id=COALESCE(email_id,?) WHERE id=?', (email_id,found['id']))
            if source == '本地上传':
                db.execute('UPDATE invoices SET has_local_upload=1 WHERE id=?', (found['id'],))
            return found['id'], False
    stored = digest + ext
    target = FILES / stored
    tmp = FILES / (secrets.token_hex(16) + '.tmp')
    try:
        tmp.write_bytes(content)
        os.replace(tmp, target)
        preview = take_preview(digest) if source == '本地上传' else None
        fields, reason = preview if preview is not None else recognize(target)
        fields, reason, suggestion = assist(target, fields, reason)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            number = fields.get('invoice_number') or ''
            # Merge only different formats from this exact email when all key fields agree.
            if email_id is not None and not reason and all(fields.get(k) not in (None, '') for k in ('invoice_number','buyer','seller','amount_cents','invoice_date')):
                matches = db.execute('''SELECT i.* FROM invoices i JOIN invoice_emails e ON e.invoice_id=i.id
                    WHERE e.email_id=? AND i.invoice_number=? AND i.recognition_status='已识别' AND i.duplicate_state='' ''', (email_id,number)).fetchall()
                matches = [row for row in matches if Path(row['stored_name']).suffix.lower() != ext and all(row[k] == fields[k] for k in ('buyer','seller','amount_cents','invoice_date'))]
                if len(matches) == 1:
                    db.execute('INSERT INTO invoice_originals(invoice_id,sha256,original_name,stored_name) VALUES(?,?,?,?)',
                               (matches[0]['id'],digest,Path(name).name[:255],stored))
                    return matches[0]['id'], False
            duplicate = duplicate_invoice(db,number)
            if duplicate: reason = duplicate_reason(reason)
            unverified = not number
            if unverified: reason = (reason + '；' if reason else '') + UNVERIFIED_REASON
            db.execute('''INSERT OR IGNORE INTO invoices(sha256,original_name,stored_name,source,email_id,imported_at,buyer,seller,amount_cents,invoice_date,invoice_number,recognition_status,failure_reason,ai_suggestion,duplicate_of,duplicate_state)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (digest, Path(name).name[:255],stored,source,email_id,now(),fields.get('buyer',''),fields.get('seller',''),fields.get('amount_cents'),fields.get('invoice_date',''),number,'识别失败' if reason else '已识别',reason,json.dumps(suggestion,ensure_ascii=False),duplicate['id'] if duplicate else None,'pending' if duplicate else 'unverified' if unverified else ''))
            result = db.execute('SELECT id FROM invoices WHERE sha256=?', (digest,)).fetchone()
            if source == '本地上传':
                db.execute('UPDATE invoices SET has_local_upload=1 WHERE id=?', (result['id'],))
            if email_id is not None:
                db.execute('INSERT OR IGNORE INTO invoice_emails(invoice_id,email_id) VALUES(?,?)',(result['id'],email_id))
        return result['id'], True
    except Exception:
        tmp.unlink(missing_ok=True)
        try:
            with connect() as db:
                exists=db.execute('SELECT 1 FROM invoices WHERE sha256=? UNION ALL SELECT 1 FROM invoice_originals WHERE sha256=?',(digest,digest)).fetchone()
            if not exists: target.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value or '').strip()).strip('. ')
    return value[:120] or '未填写'


def export_filename(invoice, template):
    ext = Path(invoice['stored_name']).suffix.lower()
    if invoice['export_name']:
        name = safe_name(invoice['export_name'])
        if Path(name).suffix.lower() in ('.pdf', '.ofd', '.xml', '.jpg', '.jpeg', '.png'):
            name = name[:-len(Path(name).suffix)]
        return name + ext
    values = {'抬头': invoice['buyer'], '商户': invoice['seller'], '金额': f"{invoice['amount_cents']/100:.2f}" if invoice['amount_cents'] is not None else '', '开票日期': invoice['invoice_date']}
    name = '_'.join(safe_name(values[k]) for k in template if k in values)
    return (name or '发票')[:180] + ext
