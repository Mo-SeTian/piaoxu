import base64
import io
import zipfile

import pytest
from PIL import Image, ImageDraw

def graphic_ofd(path, *, box='0 0 50 40', drawing=None, pages=1):
    """Synthetic outlined invoice: no embedded text or private invoice bytes."""
    drawing = drawing if drawing is not None else '''
      <PathObject ID="5" Boundary="5 5 10 10" Fill="true" Stroke="false" DrawParam="4">
        <AbbreviatedData>M 0 0 L 10 0 Q 10 5 10 10 L 0 10 B 0 8 0 2 0 0 C</AbbreviatedData>
      </PathObject>
      <ImageObject ID="6" Boundary="25 5 10 10" CTM="10 0 0 10 0 0" ResourceID="7"/>'''
    picture = io.BytesIO()
    image = Image.new('RGBA', (20,20), (0,255,0,0))
    ImageDraw.Draw(image).rectangle((5,5,14,14), fill='blue')
    image.save(picture, format='PNG')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('OFD.xml', '<OFD><DocBody><DocRoot>Doc_0/Document.xml</DocRoot></DocBody></OFD>')
        archive.writestr('Doc_0/Document.xml', f'''<Document>
          <CommonData><PageArea><PhysicalBox>{box}</PhysicalBox></PageArea><DocumentRes>DocumentRes.xml</DocumentRes></CommonData>
          <Pages>{''.join(f'<Page ID="{index+1}" BaseLoc="Pages/Page_{index}/Content.xml"/>' for index in range(pages))}</Pages>
          </Document>''')
        archive.writestr('Doc_0/DocumentRes.xml', '''<Res BaseLoc="/Doc_0/Res">
          <DrawParams><DrawParam ID="4"><FillColor Value="255 0 0"/></DrawParam></DrawParams>
          <MultiMedias><MultiMedia ID="7" Type="Image"><MediaFile>image.png</MediaFile></MultiMedia></MultiMedias></Res>''')
        archive.writestr('Doc_0/Res/image.png', picture.getvalue())
        for index in range(pages):
            archive.writestr(f'Doc_0/Pages/Page_{index}/Content.xml',
                f'<Page><Content><Layer Type="Body"><PageBlock ID="3">{drawing}</PageBlock></Layer></Content></Page>')
    return path


def test_graphic_ofd_renders_paths_images_and_positioning(tmp_path):
    from app.ofd import graphic_ofd_images
    path = graphic_ofd(tmp_path/'outlined.ofd')
    before = path.read_bytes()
    image, = graphic_ofd_images(path)
    assert image.size == (591,473)
    scale = 300/25.4
    assert image.getpixel((int(10*scale),int(10*scale))) == (255,0,0)
    assert image.getpixel((int(30*scale),int(10*scale))) == (0,0,255)
    assert image.getpixel((int(26*scale),int(6*scale))) == (255,255,255)
    assert image.getpixel((int(40*scale),int(30*scale))) == (255,255,255)
    assert path.read_bytes() == before


@pytest.mark.parametrize('box,drawing', [
    ('0 0 10000 10000', None), ('0 0 nan 40', None), ('0 0 -1 40', None),
    ('0 0 50 40', '<TextObject ID="1"><TextCode>不完整的文字渲染</TextCode></TextObject>'),
    ('0 0 50 40', '<PathObject><AbbreviatedData>M 0 0 X 1 2</AbbreviatedData></PathObject>'),
    ('0 0 50 40', '<ImageObject ResourceID="missing"/>'),
])
def test_graphic_ofd_rejects_oversized_or_unsupported_pages(tmp_path, box, drawing):
    from app.ofd import graphic_ofd_images
    path = graphic_ofd(tmp_path/'invalid.ofd', box=box, drawing=drawing)
    with pytest.raises(ValueError): list(graphic_ofd_images(path))


def test_graphic_ofd_rejects_page_truncation(tmp_path):
    from app.ofd import graphic_ofd_images
    path = graphic_ofd(tmp_path/'multipage.ofd', pages=2)
    with pytest.raises(ValueError, match='最多支持 1 页'):
        list(graphic_ofd_images(path, max_pages=1))


def test_graphic_ofd_ocr_recovers_side_by_side_party_names(tmp_path, monkeypatch):
    from app.recognition import recognize_ofd
    import pytesseract
    reading = '''电子发票（普通发票） 发票号码：99000000000000000001
开票日期：2026年11月18日
2 名称：示例采购有限公司                  a 名称：示例餐饮有限公司
信 统一社会信用代码/纳税人识别号：123456789012345678      信 统一社会信用代码/纳税人识别号：123456789012345679
价税合计（小写）￥28.30'''
    calls = []
    def ocr(image, **kwargs):
        calls.append(image.size)
        return reading
    monkeypatch.setattr(pytesseract, 'image_to_string', ocr)
    fields, reason = recognize_ofd(graphic_ofd(tmp_path/'outlined.ofd'))
    assert fields == {'buyer':'示例采购有限公司', 'seller':'示例餐饮有限公司', 'amount_cents':2830,
                      'invoice_date':'2026-11-18', 'invoice_number':'99000000000000000001'}
    assert 'OCR 结果请人工核对' in reason
    assert len(calls) == 2 and all(size == (591,473) for size in calls)


def test_graphic_ofd_ai_receives_page_images_instead_of_path_coordinates(tmp_path):
    from app.ai import content_parts
    parts = content_parts(graphic_ofd(tmp_path/'outlined.ofd'))
    assert [part['type'] for part in parts] == ['image_url']
    url = parts[0]['image_url']['url']
    assert url.startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1]))) as image:
        assert image.size == (591,473)
        assert image.getpixel((int(10*300/25.4),int(10*300/25.4)))[0] > 240


def test_ocr_combines_partial_readings_only_when_invoice_anchors_agree(monkeypatch):
    import pytesseract
    from app.recognition import _recognize_images
    parties = '''电子发票
开票日期：2026年11月18日
名称：示例采购有限公司                  名称：示例餐饮有限公司
统一社会信用代码/纳税人识别号：123456789012345678      统一社会信用代码/纳税人识别号：123456789012345679
价税合计（小写）￥28.30'''
    numbered = '''电子发票 发票号码：99000000000000000001
开票日期：2026年11月18日
价税合计（小写）￥28.30'''
    outputs = iter([parties, numbered, '电子发票', parties])
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *args,**kwargs:next(outputs))
    fields, reason = _recognize_images([Image.new('RGB',(100,100),'white')])
    assert fields['buyer'] == '示例采购有限公司' and fields['seller'] == '示例餐饮有限公司'
    assert fields['invoice_number'] == '99000000000000000001'
    assert fields['amount_cents'] == 2830 and fields['invoice_date'] == '2026-11-18'
    assert reason == '扫描件 OCR 结果请人工核对'
    outputs = iter([parties.replace('电子发票','版式单据'), numbered, '电子发票', parties.replace('电子发票','版式单据')])
    fields, reason = _recognize_images([Image.new('RGB',(100,100),'white')])
    assert fields['buyer'] == '示例采购有限公司' and fields['invoice_number'] == '99000000000000000001'
    assert reason == '扫描件 OCR 结果请人工核对'
    outputs = iter([parties, numbered.replace('2026年11月18日','2026年11月19日'), '电子发票', parties])
    fields, reason = _recognize_images([Image.new('RGB',(100,100),'white')])
    assert fields['invoice_number'] == '' and fields['invoice_date'] == ''
    assert '不一致' in reason


def test_unrenderable_ofd_stays_available_for_manual_review(tmp_path):
    from app.recognition import recognize_ofd
    path = graphic_ofd(tmp_path/'unsupported.ofd', drawing='<ImageObject ResourceID="missing"/>')
    fields, reason = recognize_ofd(path)
    assert not any(value not in (None,'') for value in fields.values())
    assert 'OFD 图形无法完整识别' in reason
