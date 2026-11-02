import os
import sys


def user_id(name):
    value = os.environ.get(name, '10001')
    if not value.isdecimal() or int(value) > 4294967294:
        raise SystemExit(f'{name} must be a numeric user or group ID')
    return int(value)


uid, gid = user_id('PUID'), user_id('PGID')
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
elif (os.geteuid(), os.getegid()) != (uid, gid):
    raise SystemExit('PUID/PGID must match the configured container user')

os.execvp(sys.argv[1], sys.argv[1:])
