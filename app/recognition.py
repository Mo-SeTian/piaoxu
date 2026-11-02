"""Local PDF text extraction with OCR fallback; uncertain fields require review."""
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fitz


def _field(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            return match.group(1).strip(' :：\t')
    return ''


def _calendar_date(value):
    """Read the date written on the invoice without converting time zones."""
    match = re.match(r'^\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?(?=$|[T\s])', value)
    if not match:
        match = re.match(r'^\s*(\d{4})(\d{2})(\d{2})(?=$|[T\s])', value)
    if not match: return ''
    try: return datetime(*map(int, match.groups())).strftime('%Y-%m-%d')
    except ValueError: return ''


def parse_invoice(text):
    compact = re.sub(r'[ \t]+', '', text)
    is_invoice = bool(re.search(r'发票|电子客票', compact))
    buyer = _field(compact, [r'(?:购买方|购买方信息)(?:(?!销售方)[\s\S]){0,80}?名称[：:]?([^\n\r]+)', r'(?:购方名称|购买方名称)[：:]?([^\n\r]+)'])
    seller = _field(compact, [r'(?:销售方|销售方信息)[\s\S]{0,45}?名称[：:]?([^\n\r]+)', r'(?:销方名称|销售方名称)[：:]?([^\n\r]+)'])
    amount = _field(compact, [r'价税合计[\s\S]{0,100}?[（(]小写[）)]\s*[¥￥]?\s*([\d,]+\.\d{2})', r'[（(]小写[）)]\s*[:：]?\s*[¥￥]?\s*([\d,]+\.\d{2})', r'(?:价税合计|合计金额)[^\d\n]{0,15}[¥￥]?([\d,]+\.\d{2})'])
    date_values = re.findall(r'(?:开票日期|开具日期)[：:]?(\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?)(?!\d)', compact)
    dates = {_calendar_date(value) for value in date_values}
    date = next(iter(dates)) if len(dates) == 1 and '' not in dates else ''
    number = _field(compact, [r'(?:发票号码|发票号)[：:]?(\d{8,20})'])
    # Some electronic PDFs emit company names and tax identifiers on separate lines.
    # Only use this ordered fallback when the document also has invoice anchors.
    if is_invoice and number and date and (not buyer or not seller):
        candidates = []
        lines = [line.strip() for line in compact.splitlines() if line.strip()]
        for index in range(len(lines) - 1):
            name = re.sub(r'^(?:名称|名\s*称)[：:]?', '', lines[index]).strip()
            tax = lines[index + 1]
            if re.fullmatch(r'[0-9A-Z]{15,24}', tax) and re.search(r'[\u4e00-\u9fff]', name) and len(name) >= 3 and not re.search(r'购买方|销售方|发票|合计',name):
                candidates.append(name)
        if len(candidates) == 2 and candidates[0] != candidates[1]:
            buyer = buyer or candidates[0]
            seller = seller or candidates[1]

    try:
        cents = int(Decimal(amount.replace(',', '')) * 100) if amount else None
    except InvalidOperation:
        cents = None
    fields = {'buyer': buyer, 'seller': seller, 'amount_cents': cents, 'invoice_date': date, 'invoice_number': number}
    missing = [label for k, label in [('buyer','抬头'),('seller','销售方'),('amount_cents','金额'),('invoice_date','开票日期')] if fields[k] is None or fields[k] == '']
    reason = '' if is_invoice and not missing else ('未确认是发票；' if not is_invoice else '') + ('未识别：' + '、'.join(missing) if missing else '')
    if buyer and seller and buyer == seller:
        reason = (reason + '；' if reason else '') + '购买方与销售方相同，请核对'
    if len(dates) > 1:
        reason = (reason + '；' if reason else '') + '开票日期存在不同值，请人工核对'
    return fields, reason


def _field_score(fields):
    return sum(fields.get(key) not in (None,'') for key in ('buyer','seller','amount_cents','invoice_date','invoice_number'))


def _side_by_side_names(page):
    """Read names next to vertically written buyer/seller labels in electronic PDFs."""
    words = page.get_text('words')
    names = {}
    positions = []
    for key, first, second in (('buyer','购','买'), ('seller','销','售')):
        markers = [w for w in words if w[4] == first and any(
            v[4] == second and abs(v[0]-w[0]) < 3 and 0 < v[1]-w[1] < 25 for v in words)]
        if len(markers) != 1: return {}
        marker = markers[0]
        labels = [w for w in words if re.sub(r'[\s:：]', '', w[4]) == '名称'
                  and 0 < w[0]-marker[0] < 80 and abs(w[1]-marker[1]) < 20]
        if len(labels) != 1: return {}
        label = labels[0]
        values = [w for w in words if 0 < w[0]-label[0] < 70 and abs(w[1]-label[1]) < 5]
        if len(values) != 1 or not re.search(r'[\u4e00-\u9fff]', values[0][4]): return {}
        name = values[0][4].strip()
        if not 2 <= len(name) <= 100: return {}
        names[key] = name
        positions.append(marker)
    if positions[0][0] >= positions[1][0] or abs(positions[0][1]-positions[1][1]) > 10 or names['buyer'] == names['seller']:
        return {}
    return names


def _deskew(image):
    """Correct small scan tilt by maximizing horizontal text-row contrast."""
    from PIL import Image, ImageStat
    gray = image.convert('L')
    if gray.width < 100 or gray.height < 100:
        return gray
    scale = min(1, 600 / gray.width, 900 / gray.height)
    preview = gray.resize((max(1, int(gray.width*scale)), max(1, int(gray.height*scale))))
    def score(angle):
        probe = preview.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=255)
        rows = [ImageStat.Stat(probe.crop((0,y,probe.width,y+1))).mean[0] for y in range(probe.height)]
        return sum((255-value)**2 for value in rows)
    angles = (0, -1, 1, -2, 2, -3, 3)
    best = max(angles, key=score)
    return gray.rotate(best, resample=Image.Resampling.NEAREST, fillcolor=255) if best else gray


def _recognize_images(images, fields=None, reason='未识别到发票内容', deadline_seconds=120):
    import pytesseract
    from PIL import ImageOps
    keys = ('buyer','seller','amount_cents','invoice_date','invoice_number')
    labels = {'buyer':'抬头','seller':'销售方','amount_cents':'金额','invoice_date':'日期','invoice_number':'发票号码'}
    best, best_is_ocr = (fields or {}, reason), False
    readings = []
    deadline=time.monotonic()+deadline_seconds
    incomplete=False
    for mode, deskew in ((6, False), (3, False), (11, False), (6, True)):
        text = []
        for image in (images() if callable(images) else images):
            remaining=deadline-time.monotonic()
            if remaining<=0:
                incomplete=True
                break
            try:
                normalized = ImageOps.autocontrast(ImageOps.exif_transpose(image).convert('L'))
                if deskew:
                    normalized = _deskew(normalized)
                text.append(pytesseract.image_to_string(normalized, lang='chi_sim+eng', config=f'--psm {mode}', timeout=max(1,min(25,remaining))))
            except (RuntimeError, OSError):
                continue
        if not text:
            continue
        candidate = parse_invoice('\n'.join(text))
        readings.append(candidate[0])
        if _field_score(candidate[0]) > _field_score(best[0]) or (not candidate[1] and best[1]):
            best, best_is_ocr = candidate, True
        if len(readings) >= 2:
            conflict = any(len({r.get(key) for r in readings if r.get(key) not in (None,'')}) > 1 for key in keys)
            if not conflict and not candidate[1] and _field_score(candidate[0]) == len(keys):
                break
        if incomplete: break
    if best_is_ocr:
        result = dict(best[0])
        uncertain = []
        for key in keys:
            values = [r[key] for r in readings if r.get(key) not in (None,'')]
            if len(set(values)) > 1:
                result[key] = None if key == 'amount_cents' else ''
                uncertain.append(labels[key])
        for key in ('buyer','seller'):
            value = result.get(key) or ''
            if value and re.search(r'[\[\]{}<>|_]', value):
                result[key] = ''
                if labels[key] not in uncertain: uncertain.append(labels[key])
        message = ('OCR 结果不一致或字符异常：' + '、'.join(uncertain) + '；') if uncertain else ''
        return result, message + (best[1] + '；' if best[1] else '') + ('部分页面未完成 OCR；' if incomplete else '') + '扫描件 OCR 结果请人工核对'
    if incomplete: return best[0], (best[1]+'；' if best[1] else '')+'部分页面未完成 OCR，请人工核对'
    return best


def recognize_pdf(pdf):
    if pdf.needs_pass:
        return {}, 'PDF 已加密，请解密后上传'
    page_count=pdf.page_count
    pages = [pdf.load_page(i) for i in range(min(100, page_count))]
    fields, reason = parse_invoice('\n'.join(page.get_text() for page in pages))
    sorted_text = '\n'.join(page.get_text(sort=True) for page in pages)
    sorted_fields, sorted_reason = parse_invoice(sorted_text)
    if ('开票日期存在不同值' in reason or '开票日期存在不同值' in sorted_reason
            or fields.get('invoice_date') and sorted_fields.get('invoice_date') and fields['invoice_date'] != sorted_fields['invoice_date']):
        fields['invoice_date'] = ''
        return fields, '开票日期存在不同值，请人工核对'
    if not reason and page_count>100:
        return fields, '仅检查前 100 页，请人工核对其余页面'
    if not reason:
        return fields, reason
    names = _side_by_side_names(pages[0]) if pages else {}
    if names and all(sorted_fields.get(key) for key in ('invoice_number','invoice_date')) and sorted_fields.get('amount_cents') is not None:
        if any(sorted_fields.get(key) and sorted_fields[key] != value for key,value in names.items()):
            return sorted_fields, '购买方或销售方解析结果不一致，请人工核对'
        sorted_fields.update(names)
        if '发票' in sorted_text:
            return sorted_fields, '仅检查前 100 页，请人工核对其余页面' if page_count>100 else ''
    if _field_score(sorted_fields) > _field_score(fields):
        fields, reason = sorted_fields, sorted_reason
    if not reason:
        return fields, '仅检查前 100 页，请人工核对其余页面' if page_count>100 else reason
    try:
        import io
        from PIL import Image
        skipped=[]
        def images():
            for page in pages[:20]:
                rect = page.rect
                if rect.width <= 0 or rect.height <= 0 or rect.width * rect.height * 4 > 30_000_000:
                    skipped.append(True)
                    continue
                pix = page.get_pixmap(matrix=fitz.Matrix(2,2), alpha=False)
                yield Image.open(io.BytesIO(pix.tobytes('png')))
        result,ocr_reason=_recognize_images(images, fields, reason)
        if skipped: ocr_reason=(ocr_reason+'；' if ocr_reason else '')+'部分页面尺寸过大，未进行 OCR'
        if page_count>20: ocr_reason=(ocr_reason+'；' if ocr_reason else '')+'OCR 仅检查前 20 页，请人工核对其余页面'
        return result,ocr_reason
    except Exception:
        return fields, reason


def recognize(path):
    from pathlib import Path
    from .formats import read_xml
    path = Path(path)
    if path.suffix.lower() in ('.jpg','.jpeg','.png'):
        try:
            from PIL import Image
            with Image.open(path) as image: return _recognize_images([image])
        except Exception: return {}, '图片无法读取或 OCR 不可用'
    if path.suffix.lower() == '.ofd': return recognize_ofd(path)
    if path.suffix.lower() == '.xml':
        try: return parse_xml_invoice(read_xml(path.read_bytes()))
        except (ValueError,OSError): return {}, 'XML 无法读取或文件已损坏'
    try:
        with fitz.open(path) as pdf:
            return recognize_pdf(pdf)
    except Exception:
        return {}, 'PDF 无法读取或文件已损坏'


def parse_xml_invoice(root):
    """Extract explicitly named fields; never substitute an untaxed subtotal for the total."""
    aliases = {
        'buyer': {'gmfmc', 'buyername', 'buyer_name', 'purchaser', '购买方名称'},
        'seller': {'xfmc', 'xsfmc', 'sellername', 'seller_name', 'seller', '销售方名称'},
        'amount_cents': {'jshj', 'totaltaxincludedamount', 'taxinclusivetotalamount', 'total_amount', '价税合计'},
        'invoice_date': {'kprq', 'issuedate', 'issue_date', 'invoicedate', 'invoice_date', 'issuetime', '开票日期'},
        'subtotal': {'hjje', 'totalamwithouttax', 'taxexclusivetotalamount'},
        'tax': {'hjse', 'totaltaxam', 'taxtotalamount'},
        'invoice_number': {'fphm', 'invoice_no', 'invoiceno', 'invoicenumber', 'invoice_number', 'eiid', '发票号码'},
    }
    found = {}
    date_values = []
    for node in root.iter():
        if not isinstance(node.tag, str): continue
        name = node.tag.split('}')[-1].lower()
        text = (node.text or '').strip()
        for key, names in aliases.items():
            if key == 'invoice_date' and name in names and text:
                date_values.append(text)
            if name in names and text and key not in found:
                found[key] = text
    value = found.get('amount_cents', '').replace(',', '').replace('¥','').replace('￥','').strip()
    try:
        if not re.fullmatch(r'\d{1,12}(?:\.\d{1,2})?', value): raise InvalidOperation
        amount = Decimal(value)
        found['amount_cents'] = int(amount * 100)
    except InvalidOperation:
        found['amount_cents'] = None
    dates = {_calendar_date(value) for value in date_values}
    found['invoice_date'] = next(iter(dates)) if len(dates) == 1 and '' not in dates else ''
    mismatch = False
    if found.get('subtotal') and found.get('tax') and found.get('amount_cents') is not None:
        try:
            subtotal = Decimal(found['subtotal'].replace(',',''))
            tax = Decimal(found['tax'].replace(',',''))
            mismatch = (subtotal + tax) * 100 != found['amount_cents']
        except InvalidOperation:
            mismatch = True
    missing = [label for key,label in [('buyer','抬头'),('seller','销售方'),('amount_cents','金额'),('invoice_date','开票日期')] if found.get(key) in (None,'')]
    if not found.get('invoice_number'): missing.append('发票号码')
    reason = ('未识别：'+'、'.join(missing)) if missing else ''
    if len(dates) > 1:
        reason = (reason + '；' if reason else '') + '开票日期存在不同值，请人工核对'
    if mismatch: reason = (reason + '；' if reason else '') + '价税合计与金额、税额不一致'
    return found, reason


def recognize_ofd(path):
    from .formats import open_ofd, read_xml
    try:
        with open_ofd(path.read_bytes()) as archive:
            xml_names = [i.filename for i in archive.infolist() if i.filename.lower().endswith('.xml') and i.filename != 'OFD.xml']
            text = []
            best = ({}, '未识别 OFD 中的发票字段')
            for name in xml_names:
                try: root = read_xml(archive.read(name))
                except ValueError: continue
                fields, reason = parse_xml_invoice(root)
                if len([v for v in fields.values() if v not in (None,'')]) > len([v for v in best[0].values() if v not in (None,'')]): best = (fields,reason)
                for node in root.iter():
                    if isinstance(node.tag,str) and node.tag.split('}')[-1] == 'TextCode' and node.text:
                        text.append(node.text)
            if not best[1]: return best
            if text:
                parsed = parse_invoice('\n'.join(text))
                if not parsed[1]: return parsed
                if len([v for v in parsed[0].values() if v not in (None,'')]) > len([v for v in best[0].values() if v not in (None,'')]): return parsed
            return best
    except (ValueError,OSError):
        return {}, 'OFD 无法读取或文件已损坏'
