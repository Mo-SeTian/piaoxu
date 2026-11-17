"""Rasterize image/path-only OFD invoices locally for OCR and vision models."""
import base64
import io
import math
import re
from posixpath import dirname, join, normpath
from xml.etree import ElementTree as ET

import fitz
from PIL import Image

from .formats import open_ofd, read_xml


def _location(base, value):
    return normpath(value.lstrip('/') if value.startswith('/') else join(base, value))


def _numbers(value, count=None):
    numbers = [float(part) for part in value.split()]
    if (count is not None and len(numbers) != count
            or not all(math.isfinite(number) and abs(number) <= 1_000_000 for number in numbers)):
        raise ValueError('OFD 图形坐标无效')
    return numbers


def _transform(node):
    transforms = []
    if node.get('Boundary'):
        x, y, _, _ = _numbers(node.get('Boundary'), 4)
        transforms.append(f'translate({x} {y})')
    if node.get('CTM'):
        transforms.append('matrix(' + ' '.join(map(str, _numbers(node.get('CTM'), 6))) + ')')
    return ' '.join(transforms)


def _path(node):
    value = node.findtext('{*}AbbreviatedData', '')
    # OFD uses B for a cubic curve and C to close a subpath.
    tokens = re.findall(r'[A-Z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', value)
    if re.sub(r'[\s,]', '', value) != ''.join(tokens) or any(token.isalpha() and token not in 'SMLQBCA' for token in tokens):
        raise ValueError('OFD 包含暂不支持的图形路径')
    _numbers(' '.join(token for token in tokens if not token.isalpha()))
    return ' '.join({'S':'M', 'B':'C', 'C':'Z'}.get(token, token) for token in tokens)


def graphic_ofd_images(path, max_pages=20):
    """Yield bounded page images; do not substitute incomplete text rendering."""
    with open_ofd(path.read_bytes()) as archive:
        roots = {}
        for name in archive.namelist():
            if name.lower().endswith('.xml'):
                try: roots[name] = read_xml(archive.read(name))
                except ValueError: continue
        documents = [normpath(node.text.lstrip('/')) for node in roots['OFD.xml'].iter()
                     if node.tag.split('}')[-1] == 'DocRoot' and node.text]
        if not documents:
            raise ValueError('OFD 未包含可渲染的页面')
        page_count = 0
        for document_name in documents:
            document = roots.get(document_name)
            if document is None: raise ValueError('OFD 文档引用无效')
            resources, media = {}, {}
            for ref in document.iter():
                if ref.tag.split('}')[-1] not in ('DocumentRes', 'PublicRes') or not ref.text: continue
                location = _location(dirname(document_name), ref.text)
                resource = roots.get(location)
                if resource is None: raise ValueError('OFD 资源引用无效')
                for item in resource.iter():
                    if item.get('ID'):
                        resources[item.get('ID')] = item
                    if item.tag.split('}')[-1] == 'MultiMedia':
                        filename = item.findtext('{*}MediaFile', '')
                        media[item.get('ID')] = _location(_location(dirname(location), resource.get('BaseLoc', '')), filename)

            def color(node, key):
                element = node.find('{*}' + key)
                if element is None: return None
                values = _numbers(element.get('Value', ''))
                if len(values) == 1: values *= 3
                if len(values) != 3 or any(value < 0 or value > 255 for value in values):
                    raise ValueError('OFD 颜色暂不支持')
                return 'rgb(' + ','.join(str(round(value)) for value in values) + ')'

            def draw(node, parent):
                tag = node.tag.split('}')[-1]
                if tag not in ('Content', 'Layer', 'PageBlock', 'PathObject', 'ImageObject'):
                    raise ValueError('OFD 包含暂不支持的绘图对象')
                group = ET.SubElement(parent, 'g', transform=_transform(node))
                param = resources.get(node.get('DrawParam'))
                if param is not None:
                    for key, attr in (('FillColor','fill'), ('StrokeColor','stroke')):
                        value = color(param, key)
                        if value: group.set(attr, value)
                if tag == 'PathObject':
                    attrs = {'d':_path(node), 'fill-rule':'evenodd' if node.get('Rule') == 'Even-Odd' else 'nonzero'}
                    for key, attr, enabled in (('FillColor','fill',node.get('Fill','false')), ('StrokeColor','stroke',node.get('Stroke','true'))):
                        if enabled != 'true': attrs[attr] = 'none'
                        elif color(node, key): attrs[attr] = color(node, key)
                    attrs['stroke-width'] = str(_numbers(node.get('LineWidth', param.get('LineWidth','0.353') if param is not None else '0.353'), 1)[0])
                    ET.SubElement(group, 'path', attrs)
                elif tag == 'ImageObject':
                    filename = media.get(node.get('ResourceID'))
                    if not filename: raise ValueError('OFD 图片引用无效')
                    with Image.open(io.BytesIO(archive.read(filename))) as image:
                        if image.width * image.height > 30_000_000: raise ValueError('OFD 图片尺寸过大')
                        output = io.BytesIO()
                        image.convert('RGBA').save(output, format='PNG')
                    # Image coordinates are normalized to a unit square before CTM.
                    ET.SubElement(group, 'image', {'width':'1', 'height':'1', 'preserveAspectRatio':'none',
                        'href':'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode('ascii')})
                else:
                    for child in node: draw(child, group)

            for ref in document.findall('{*}Pages/{*}Page'):
                page_count += 1
                if page_count > max_pages: raise ValueError(f'OFD 图形识别最多支持 {max_pages} 页，请人工核对')
                location = _location(dirname(document_name), ref.get('BaseLoc',''))
                page = roots.get(location)
                if page is None: raise ValueError('OFD 页面引用无效')
                if page.find('{*}Template') is not None: raise ValueError('OFD 图形模板暂不支持')
                box = page.findtext('{*}Area/{*}PhysicalBox') or document.findtext('{*}CommonData/{*}PageArea/{*}PhysicalBox', '')
                x, y, width, height = _numbers(box, 4)
                if width <= 0 or height <= 0 or width * height * (300 / 25.4) ** 2 > 30_000_000:
                    raise ValueError('OFD 页面尺寸过大')
                svg = ET.Element('svg', {'xmlns':'http://www.w3.org/2000/svg', 'width':f'{width}mm', 'height':f'{height}mm',
                    'viewBox':f'{x} {y} {width} {height}', 'fill':'black', 'stroke':'black'})
                content = page.find('{*}Content')
                if content is None: raise ValueError('OFD 页面没有图形内容')
                draw(content, svg)
                with fitz.open(stream=ET.tostring(svg), filetype='svg') as rendered:
                    pixmap = rendered[0].get_pixmap(matrix=fitz.Matrix(300/72, 300/72), alpha=False)
                    yield Image.frombytes('RGB', (pixmap.width,pixmap.height), pixmap.samples)
        if not page_count: raise ValueError('OFD 未包含可渲染的页面')
