"""Coordinate normal operations with backup and restore across app processes."""
import asyncio
import fcntl
import os
from contextlib import asynccontextmanager, contextmanager

from .db import CONFIG

@contextmanager
def storage_lock_sync(exclusive=False):
    fd = os.open(CONFIG / '.piaoxu.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

@asynccontextmanager
async def storage_lock(exclusive=False):
    fd = os.open(CONFIG / '.piaoxu.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
