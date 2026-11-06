"""Verified backups and crash-recoverable restoration of local storage."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet, InvalidToken

from .db import CONFIG, DATA, DB, FILES

FORMAT = 'piaoxu-backup'
VERSION = 1
MAX_ENTRIES = 100000
MAX_TOTAL = 20 * 1024**3
MAX_MANIFEST = 32 * 1024**2
CHUNK = 1024 * 1024
WORK = '.piaoxu-restore'
JOURNAL = '.piaoxu-restore.json'
RECOVERY = '.piaoxu-recovery'
RESERVED = {WORK, JOURNAL, JOURNAL + '.tmp', RECOVERY, '.piaoxu.lock'}


def _roots():
    return [('config', CONFIG), ('data', DATA)]


def _selected(prefix, root):
    for child in sorted(root.iterdir()):
        if child.name in RESERVED: continue
        if prefix == 'config' and child.name in (DB.name+'-wal', DB.name+'-shm', DB.name+'-journal'): continue
        if CONFIG == DATA:
            if prefix == 'config' and child.name == FILES.name: continue
            if prefix == 'data' and child.name != FILES.name: continue
        yield child


def _walk(prefix, root):
    def visit(path):
        if path.is_symlink(): raise ValueError('存储目录包含符号链接，无法安全备份')
        if path.is_dir():
            yield path, prefix + '/' + path.relative_to(root).as_posix() + '/', True
            for child in sorted(path.iterdir()): yield from visit(child)
        elif path.is_file():
            yield path, prefix + '/' + path.relative_to(root).as_posix(), False
        else:
            raise ValueError('存储目录包含不支持的文件类型')
    for item in _selected(prefix, root): yield from visit(item)


def _snapshot_database(destination):
    source = sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)
    target = sqlite3.connect(destination)
    try: source.backup(target)
    finally: target.close();source.close()


def _write_entry(archive, name, path, directory=False):
    digest = hashlib.sha256()
    if directory:
        archive.writestr(name,b'')
        return {'path':name,'size':0,'sha256':digest.hexdigest()}
    before = path.stat()
    size = 0
    with path.open('rb') as source, archive.open(name,'w',force_zip64=True) as target:
        while True:
            block = source.read(CHUNK)
            if not block: break
            target.write(block);digest.update(block);size += len(block)
    after = path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
        raise ValueError('备份期间文件发生变化，请重试')
    return {'path':name,'size':size,'sha256':digest.hexdigest()}


def create_backup(output):
    with tempfile.TemporaryDirectory(prefix='piaoxu-backup-') as temporary:
        snapshot = Path(temporary)/'piaoxu.sqlite3'
        _snapshot_database(snapshot)
        entries=[];total=0
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
            for prefix,root in _roots():
                for path,name,is_dir in _walk(prefix,root):
                    if prefix=='config' and path==DB: path=snapshot
                    entry=_write_entry(archive,name,path,is_dir)
                    entries.append(entry);total+=entry['size']
                    if len(entries)>MAX_ENTRIES or total>MAX_TOTAL:
                        raise ValueError('数据量超过单次备份限制')
            if not any(item['path']=='config/piaoxu.sqlite3' for item in entries):
                raise ValueError('找不到数据库')
            key=CONFIG/'.mail-key'
            if key.exists(): shutil.copy2(key,Path(temporary)/'.mail-key')
            _check_database(Path(temporary),DATA,{item['path']:item for item in entries})
            manifest={'format':FORMAT,'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(), 'entries':entries}
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,separators=(',',':')).encode())
    output.seek(0)


def _safe_name(name):
    if not isinstance(name,str) or not name or '\\' in name or '\x00' in name or name.startswith('/'):
        raise ValueError('备份包含无效路径')
    path=PurePosixPath(name)
    if any(part in ('','.','..') for part in name.rstrip('/').split('/')) or len(path.parts)<2 or path.parts[0] not in ('config','data'):
        raise ValueError('备份包含无效路径')
    if path.parts[1] in RESERVED: raise ValueError('备份包含保留路径')
    return path


def _check_database(config_dir, data_dir, manifest_by_path):
    database=config_dir/'piaoxu.sqlite3'
    if not database.is_file(): raise ValueError('备份缺少数据库')
    try:
        db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('数据库校验失败')
            required={'accounts','emails','invoices','email_attachments','invoice_emails','groups','group_invoices','settings'}
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required.issubset(tables): raise ValueError('备份数据库结构不完整')
            if db.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('数据库关联校验失败')
            accounts=db.execute('SELECT secret FROM accounts').fetchall()
            settings=db.execute("SELECT value FROM settings WHERE key='ai_settings'").fetchone()
            secret=json.loads(settings['value']).get('secret','') if settings else ''
            if accounts or secret:
                key=config_dir/'.mail-key'
                if not key.is_file(): raise ValueError('备份缺少配置密钥')
                cipher=Fernet(key.read_bytes())
                for row in accounts: cipher.decrypt(row['secret'].encode())
                if secret: cipher.decrypt(secret.encode())
            invoices=db.execute('SELECT sha256,stored_name FROM invoices').fetchall()
            for row in invoices:
                name=row['stored_name'];digest=row['sha256']
                if not re.fullmatch(r'[0-9a-f]{64}\.(?:pdf|ofd|xml|jpg|jpeg|png)',name) or name.split('.')[0]!=digest:
                    raise ValueError('备份中的发票文件名无效')
                item=manifest_by_path.get('data/files/'+name)
                if not item or item['sha256']!=digest or not (data_dir/'files'/name).is_file():
                    raise ValueError('备份缺少发票原件或原件校验失败')
            return {'invoices':len(invoices),'accounts':len(accounts),
                    'emails':db.execute('SELECT count(*) FROM emails').fetchone()[0],
                    'groups':db.execute('SELECT count(*) FROM groups').fetchone()[0]}
        finally: db.close()
    except (sqlite3.Error,InvalidToken,KeyError,TypeError,ValueError,UnicodeError,OSError) as exc:
        if isinstance(exc,ValueError) and str(exc) in ('备份缺少数据库','数据库校验失败','备份数据库结构不完整','数据库关联校验失败','备份缺少配置密钥','备份中的发票文件名无效','备份缺少发票原件或原件校验失败'):
            raise
        raise ValueError('备份数据库或配置密钥无效') from None


class _SeekableInput:
    def __init__(self, source): self.source=source
    def __getattr__(self, name): return getattr(self.source,name)
    def seekable(self): return True


def extract_backup(source, destination):
    source.seek(0)
    destination=Path(destination)
    try:
        with zipfile.ZipFile(_SeekableInput(source)) as archive:
            infos=archive.infolist()
            if len(infos)>MAX_ENTRIES+1: raise ValueError('备份文件数量过多')
            names=[item.filename for item in infos]
            if len(names)!=len(set(names)) or len({name.casefold() for name in names})!=len(names): raise ValueError('备份包含重复路径')
            if names.count('manifest.json')!=1: raise ValueError('备份清单缺失')
            info=archive.getinfo('manifest.json')
            if info.file_size>MAX_MANIFEST: raise ValueError('备份清单过大')
            manifest=json.loads(archive.read(info))
            if not isinstance(manifest,dict) or manifest.get('format')!=FORMAT or manifest.get('version')!=VERSION or not isinstance(manifest.get('entries'),list):
                raise ValueError('备份格式不受支持')
            items=manifest['entries']
            if len(items)>MAX_ENTRIES or len(items)!=len(infos)-1: raise ValueError('备份清单不完整')
            by_path={}
            total=0
            for item in items:
                if not isinstance(item,dict): raise ValueError('备份清单无效')
                name=item.get('path');path=_safe_name(name)
                size=item.get('size');digest=item.get('sha256')
                if name in by_path or not isinstance(size,int) or isinstance(size,bool) or size<0 or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest): raise ValueError('备份清单无效')
                info=archive.getinfo(name)
                mode=info.external_attr>>16
                if stat.S_ISLNK(mode) or info.flag_bits & 1 or info.file_size!=size or info.is_dir()!=name.endswith('/'):
                    raise ValueError('备份文件无效')
                total+=size
                if total>MAX_TOTAL: raise ValueError('备份解压后过大')
                target=destination.joinpath(*path.parts)
                if info.is_dir():
                    if size: raise ValueError('备份目录无效')
                    target.mkdir(parents=True,exist_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    checksum=hashlib.sha256();written=0
                    with archive.open(info) as src, target.open('xb') as out:
                        while True:
                            block=src.read(CHUNK)
                            if not block: break
                            written+=len(block)
                            if written>size: raise ValueError('备份文件大小不符')
                            out.write(block);checksum.update(block)
                    if written!=size or checksum.hexdigest()!=digest: raise ValueError('备份文件校验失败')
                    if path.parts[0]=='config': os.chmod(target,0o600)
                by_path[name]=item
            if set(names)-{'manifest.json'}!=set(by_path): raise ValueError('备份清单不完整')
            (destination/'config').mkdir(exist_ok=True)
            (destination/'data'/'files').mkdir(parents=True,exist_ok=True)
            counts=_check_database(destination/'config',destination/'data',by_path)
            return {'created_at':manifest.get('created_at',''), 'counts':counts,'files':sum(not name.endswith('/') for name in by_path),'size':total}
    except (zipfile.BadZipFile,zipfile.LargeZipFile,KeyError,json.JSONDecodeError,UnicodeError,RuntimeError,OSError,FileExistsError) as exc:
        raise ValueError('备份文件损坏或格式无效') from None


def inspect_backup(source):
    with tempfile.TemporaryDirectory(prefix='piaoxu-inspect-backup-') as temp:
        return extract_backup(source,temp)


def _remove(path):
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.is_dir(): shutil.rmtree(path)


def _sync_directory(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _sync_tree(path):
    for root,dirs,files in os.walk(path,topdown=False):
        for name in files:
            with open(Path(root)/name,'rb') as handle: os.fsync(handle.fileno())
        _sync_directory(root)


def _write_journal(value):
    path=CONFIG/JOURNAL
    temp=CONFIG/(JOURNAL+'.tmp')
    with temp.open('w') as handle:
        json.dump(value,handle,ensure_ascii=False)
        handle.flush();os.fsync(handle.fileno())
    os.replace(temp,path)
    _sync_directory(CONFIG)


def _restore_roots():
    return [CONFIG] if CONFIG==DATA else [CONFIG,DATA]


def recover_interrupted_restore():
    marker=CONFIG/JOURNAL
    if not marker.exists():
        for root in _restore_roots():
            work=root/WORK
            if work.exists(): shutil.rmtree(work)
        return
    try:
        journal=json.loads(marker.read_text())
        roots=_restore_roots()
        if journal.get('version')!=1 or journal.get('phase') not in ('applying','committed') or len(journal.get('roots',[]))!=len(roots): raise ValueError
        if journal['phase']=='applying':
            for root,record in zip(roots,journal['roots']):
                old=record['old'];new=record['new']
                if any(not isinstance(name,str) or name in ('','.','..') or Path(name).name!=name or name in RESERVED for name in old+new): raise ValueError
                previous=root/WORK/'previous';incoming=root/WORK/'incoming'
                for name in set(old)|set(new):
                    current=root/name;prior=previous/name;pending=incoming/name
                    if prior.exists():
                        _remove(current);os.replace(prior,current)
                    elif name not in old and not pending.exists():
                        _remove(current)
        for root in roots:
            work=root/WORK
            if work.exists(): shutil.rmtree(work)
        marker.unlink()
    except Exception:
        raise RuntimeError('上次恢复未完成，自动回滚失败；请保留 config 和 data 目录并检查备份') from None


def restore_backup(source):
    with tempfile.TemporaryDirectory(prefix='piaoxu-restore-check-') as temp:
        result=extract_backup(source,temp)
        recovery=CONFIG/RECOVERY
        recovery.mkdir(mode=0o700,exist_ok=True)
        recovery_file=recovery/('before-restore-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.zip')
        try:
            fd=os.open(recovery_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w+b') as backup: create_backup(backup);backup.flush();os.fsync(backup.fileno())
        except ValueError:
            recovery_file.unlink(missing_ok=True)
            # Preserve damaged storage as-is when a valid application backup cannot be made.
            recovery_file = recovery_file.with_name(recovery_file.stem + '-raw.zip')
            try:
                fd = os.open(recovery_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w+b') as output:
                    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                        for prefix, root in _roots():
                            for child in sorted(root.iterdir()):
                                if child.name in RESERVED: continue
                                if CONFIG == DATA and ((prefix == 'config' and child.name == FILES.name) or (prefix == 'data' and child.name != FILES.name)): continue
                                if child.is_symlink(): raise ValueError('存储目录包含符号链接，无法安全保留')
                                paths = [child] + (sorted(child.rglob('*')) if child.is_dir() else [])
                                for path in paths:
                                    if path.is_symlink(): raise ValueError('存储目录包含符号链接，无法安全保留')
                                    if not path.is_file() and not path.is_dir(): raise ValueError('存储目录包含不支持的文件类型')
                                    name = prefix + '/' + path.relative_to(root).as_posix() + ('/' if path.is_dir() else '')
                                    _write_entry(archive, name, path, path.is_dir())
                    output.flush(); os.fsync(output.fileno())
                result['recovery_incomplete'] = True
            except Exception:
                recovery_file.unlink(missing_ok=True)
                raise
        except Exception:
            recovery_file.unlink(missing_ok=True)
            raise
        roots=_restore_roots()
        try:
            for root in roots:
                work=root/WORK
                if work.exists(): raise ValueError('存在未完成的恢复操作')
                (work/'incoming').mkdir(parents=True)
                (work/'previous').mkdir()
            pairs=[(CONFIG,Path(temp)/'config'),(DATA,Path(temp)/'data')]
            for root,staged in pairs:
                for item in staged.iterdir():
                    target=root/WORK/'incoming'/item.name
                    if target.exists(): raise ValueError('备份目录存在冲突')
                    if item.is_dir(): shutil.copytree(item,target)
                    else: shutil.copy2(item,target)
            for root in roots: _sync_tree(root/WORK)
            records=[]
            for root in roots:
                old=[p.name for p in root.iterdir() if p.name not in RESERVED]
                new=[p.name for p in (root/WORK/'incoming').iterdir()]
                old_cases={name.casefold():name for name in old}
                if any(name.casefold() in old_cases and old_cases[name.casefold()]!=name for name in new):
                    raise ValueError('备份与当前目录存在大小写冲突，请调整文件名后重试')
                records.append({'old':old,'new':new})
            journal={'version':1,'phase':'applying','roots':records}
            _write_journal(journal)
            for root,record in zip(roots,records):
                previous=root/WORK/'previous';incoming=root/WORK/'incoming'
                for name in record['old']:
                    os.replace(root/name,previous/name);_sync_directory(root);_sync_directory(previous)
                for name in record['new']:
                    os.replace(incoming/name,root/name);_sync_directory(root);_sync_directory(incoming)
            journal['phase']='committed';_write_journal(journal)
            recover_interrupted_restore()
            return result
        except Exception:
            if (CONFIG/JOURNAL).exists(): recover_interrupted_restore()
            else:
                for root in roots:
                    work=root/WORK
                    if work.exists(): shutil.rmtree(work)
            raise
