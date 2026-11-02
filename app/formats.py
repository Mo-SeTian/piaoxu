"""Validate invoice originals without changing their bytes or unpacking to disk."""
import io
import zipfile
from pathlib import PurePosixPath
from defusedxml import ElementTree as ET

LIMIT = 30 * 1024 * 1024
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024


def read_xml(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError('XML 文件过大')
    try:
        return ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as exc:
        raise ValueError('XML 文件无效或包含不安全内容') from exc


def open_ofd(content):
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if (len(infos) > 1000 or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES
                or len(names) != len(set(names)) or 'OFD.xml' not in names
                or any(i.flag_bits & 1 or i.file_size > MAX_XML_BYTES and i.filename.lower().endswith('.xml')
                       or '\\' in i.filename or PurePosixPath(i.filename).is_absolute()
                       or '..' in PurePosixPath(i.filename).parts for i in infos)):
            raise ValueError('OFD 文件结构无效或超过安全限制')
        root = read_xml(archive.read('OFD.xml'))
        if root.tag.split('}')[-1] != 'OFD':
            raise ValueError('OFD 文件结构无效')
        return archive
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError) as exc:
        raise ValueError('OFD 文件无效或已损坏') from exc


def validate(name, content):
    if not content or len(content) > LIMIT:
        raise ValueError('文件为空或超过 30 MB')
    ext = PurePosixPath(name.replace('\\', '/')).suffix.lower()
    if ext == '.pdf':
        if not content.startswith(b'%PDF-'): raise ValueError('文件不是有效的 PDF')
    elif ext == '.xml':
        read_xml(content)
    elif ext in ('.jpg', '.jpeg', '.png'):
        try:
            from PIL import Image
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in ('JPEG','PNG') or image.width * image.height > 30_000_000:
                    raise ValueError('图片格式无效或尺寸过大')
                if (ext == '.png') != (image.format == 'PNG'):
                    raise ValueError('图片扩展名与内容不符')
                image.verify()
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片无效或尺寸过大') from exc
    elif ext == '.ofd':
        with open_ofd(content): pass
    else:
        raise ValueError('仅支持 PDF、OFD、XML、JPG 和 PNG 文件')
    return ext
