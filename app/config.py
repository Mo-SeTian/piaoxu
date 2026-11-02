"""Human-readable non-secret runtime settings."""
import json
import os

from .db import CONFIG

DEFAULT_INTERVAL = 300

def ensure_config():
    path=CONFIG/'config.json'
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        return
    with os.fdopen(fd,'w') as handle:
        json.dump({'mail_sync_interval_seconds':DEFAULT_INTERVAL},handle,ensure_ascii=False,indent=2)
        handle.write('\n')

def mail_sync_interval():
    try:
        value=json.loads((CONFIG/'config.json').read_text())['mail_sync_interval_seconds']
        if isinstance(value,bool) or not isinstance(value,int) or not 30<=value<=86400: raise ValueError
        return value
    except (OSError,ValueError,TypeError,KeyError):
        return DEFAULT_INTERVAL
