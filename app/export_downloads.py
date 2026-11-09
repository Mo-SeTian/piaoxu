"""Short-lived ZIP files for native browser and download-manager requests."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time

_directory = tempfile.TemporaryDirectory(prefix='piaoxu-exports-')
ROOT = Path(_directory.name)
TTL = 30 * 60


def session_directory(session):
    return ROOT / hashlib.sha256(session.encode()).hexdigest()


def prepare(output, session, filename):
    cutoff = time.time() - TTL
    for path in ROOT.glob('*/*.zip'):
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
    directory = session_directory(session)
    directory.mkdir(mode=0o700, exist_ok=True)
    download_id = secrets.token_hex(24)
    path = directory / (download_id + '-' + filename)
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as target:
            shutil.copyfileobj(output, target)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return download_id


def resolve(download_id, session):
    if not re.fullmatch(r'[0-9a-f]{48}', download_id):
        return None
    path = next(session_directory(session).glob(download_id + '-*.zip'), None)
    if path is None:
        return None
    try:
        if path.stat().st_mtime > time.time() - TTL:
            return path
        path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    return None
