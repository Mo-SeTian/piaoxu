"""Reproducible synthetic OCR smoke test; contains no actual invoice data."""
import io
import tempfile
from pathlib import Path
import fitz
from PIL import Image, ImageEnhance
from app.recognition import recognize


def build_scan(folder, index, contrast=1.0, rotation=0, title="电子发票", seller="示例销售方公司", amount="268.00"):
    lines = [
        title,
        f'发票号码：1234567890123456789{index}',
        '开票日期：2026年10月28日',
        '购买方信息', '名称：示例购买方公司',
        '销售方信息', f'名称：{seller}',
        f'价税合计（小写）￥{amount}',
    ]
    doc = fitz.open(); page = doc.new_page(width=595,height=842)
    for i, line in enumerate(lines):
        page.insert_text((45,60+i*38),line,fontname='china-s',fontsize=18)
    image = Image.open(io.BytesIO(page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False).tobytes('png'))).convert('RGB')
    doc.close()
    if contrast != 1: image=ImageEnhance.Contrast(image).enhance(contrast)
    if rotation: image=image.rotate(rotation,expand=False,fillcolor='white')
    blob=io.BytesIO(); image.save(blob,format='PNG')
    pdf=fitz.open(); p=pdf.new_page(width=595,height=842); p.insert_image(p.rect,stream=blob.getvalue())
    out=Path(folder)/f'scan-{index}.pdf'; pdf.save(out); pdf.close()
    return out


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='piaoxu-ocr-') as folder:
        cases = ((1,0,'电子发票','示例销售方公司','268.00'),
                 (.65,0,'电子发票','示例销售方公司','268.00'),
                 (1,1.2,'电子发票','示例销售方公司','268.00'),
                 (1,0,'电子发票（普通发票）','示例餐饮有限公司','88.50'),
                 (1,0,'电子发票（专用发票）','示例住宿有限公司','956.40'),
                 (1,-1,'铁路电子客票','示例铁路有限公司','128.00'))
        for index,(contrast,angle,title,seller,amount) in enumerate(cases,start=1):
            path=build_scan(folder,index,contrast,angle,title,seller,amount)
            fields,reason=recognize(path)
            expected={'buyer':'示例购买方公司','seller':seller,'amount_cents':int(round(float(amount)*100)),'invoice_date':'2026-10-28','invoice_number':f'1234567890123456789{index}'}
            correct=sum(fields.get(k)==v for k,v in expected.items())
            wrong=sum(fields.get(k) not in (None,'',v) for k,v in expected.items())
            print(f'scan-{index}: {correct}/5 correct; {wrong} wrong; {"review" if reason else "auto"}; mismatched: {[k for k,v in expected.items() if fields.get(k) not in (None,"",v)]}; missing: {[k for k,v in expected.items() if fields.get(k) in (None,"")]}')
