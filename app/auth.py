"""Single-user login and revocable sessions stored with the application database."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .db import connect

SESSION_SECONDS = 7 * 24 * 60 * 60
_attempts = {}
_attempt_lock = threading.Lock()


def allow_attempt(client):
    now = time.monotonic()
    with _attempt_lock:
        if len(_attempts) > 4096:
            for key in list(_attempts):
                if not _attempts[key] or _attempts[key][-1] <= now - 60:
                    del _attempts[key]
        attempts = _attempts.setdefault(client, deque())
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            return False
        attempts.append(now)
        return True


def clear_attempts(client):
    with _attempt_lock:
        _attempts.pop(client, None)


def password_hash(password, salt):
    return Scrypt(salt=bytes.fromhex(salt), length=32, n=16384, r=8, p=1).derive(password.encode('utf-8')).hex()


def ensure_auth():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS login_user (id INTEGER PRIMARY KEY CHECK (id=1), username TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS login_sessions (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL)')
        if not db.execute('SELECT 1 FROM login_user WHERE id=1').fetchone():
            salt = secrets.token_hex(16)
            db.execute('INSERT OR IGNORE INTO login_user VALUES (1,?,?,?)', ('admin', salt, password_hash('admin', salt)))


def verify(username, password):
    with connect() as db:
        row = db.execute('SELECT username,salt,password_hash FROM login_user WHERE id=1').fetchone()
    return bool(row and hmac.compare_digest(row['username'].encode(), username.encode()) and hmac.compare_digest(row['password_hash'], password_hash(password, row['salt'])))


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def new_session():
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute('DELETE FROM login_sessions WHERE expires<=?', (int(time.time()),))
        db.execute('INSERT INTO login_sessions VALUES (?,?)', (token_hash(token), int(time.time()) + SESSION_SECONDS))
    return token


def current_user(token):
    if not token or len(token) > 256:
        return None
    with connect() as db:
        row = db.execute('SELECT username FROM login_user WHERE id=1 AND EXISTS (SELECT 1 FROM login_sessions WHERE token_hash=? AND expires>?)', (token_hash(token), int(time.time()))).fetchone()
    return row['username'] if row else None


def revoke(token):
    if token and len(token) <= 256:
        with connect() as db:
            db.execute('DELETE FROM login_sessions WHERE token_hash=?', (token_hash(token),))


def change_user(username, password):
    salt = secrets.token_hex(16)
    digest = password_hash(password, salt)
    with connect() as db:
        db.execute('UPDATE login_user SET username=?, salt=?, password_hash=? WHERE id=1', (username, salt, digest))
        db.execute('DELETE FROM login_sessions')


def revoke_all():
    with connect() as db:
        db.execute('DELETE FROM login_sessions')
