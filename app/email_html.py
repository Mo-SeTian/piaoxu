"""Prepare email HTML for an isolated, script-free preview."""
import base64
import html
import io
import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from PIL import Image

TAGS = set('a abbr b blockquote br caption center code col colgroup dd del div dl dt em font h1 h2 h3 h4 h5 h6 hr i img li ol p pre s small span strike strong style sub sup table tbody td tfoot th thead tr u ul'.split())
VOID = {'br','col','hr','img'}
SKIP = {'script','iframe','object','embed','svg','math','video','audio','canvas','template','noscript'}
ATTRS = set('align alt bgcolor border cellpadding cellspacing class color colspan face height href hspace id src srcset style title valign vspace width background'.split())
HTML_VERSION = 2

def url_scheme(value):
    try: return urlsplit(value).scheme.lower()
    except ValueError: return ''


def inline_images(message):
    images = {}
    total = 0
    for part in message.walk():
        cid = (part.get('Content-ID') or '').strip().strip('<>')
        location = (part.get('Content-Location') or '').strip()
        if not cid and not location: continue
        content = part.get_payload(decode=True) or b''
        if not content or len(content) > 2*1024*1024 or total + len(content) > 5*1024*1024: continue
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > 30_000_000: continue
                mime = {'PNG':'image/png','JPEG':'image/jpeg','GIF':'image/gif','WEBP':'image/webp'}.get(image.format)
                if not mime: continue
                image.verify()
        except Exception:
            continue
        data = 'data:' + mime + ';base64,' + base64.b64encode(content).decode('ascii')
        for key in (cid, location):
            if key: images[unquote(key).lower()] = data
        total += len(content)
    return images


class EmailHTML(HTMLParser):
    def __init__(self, images, proxy=None):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = images
        self.proxy = proxy
        self.skip = 0
        self.style = False
        self.image_budget = 8*1024*1024

    def replace_cid(self, text):
        def replace(match):
            return self.inline_image(match.group(1))
        return re.sub(r'cid:([^\s"\'<>\)]+)', replace, text, flags=re.I)

    def inline_image(self, key):
        image = self.images.get(unquote(key).strip('<>').lower(), '')
        if len(image) > self.image_budget: return ''
        self.image_budget -= len(image)
        return image

    def image_source(self, value):
        if value.lower().startswith('data:'): return value
        embedded = self.inline_image(value)
        if embedded: return embedded
        value = 'https:' + value if value.startswith('//') else value
        return self.proxy(value) if self.proxy and url_scheme(value) in ('http','https') else value

    def css_images(self, value):
        value = self.replace_cid(value)
        def replace(match):
            source = self.image_source(match.group(2).strip())
            return 'url(' + json.dumps(source) + ')'
        return re.sub(r'url\(\s*(["\']?)([^)"\']+)\1\s*\)', replace, value, flags=re.I)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag in SKIP: self.skip += 1
            return
        if tag in SKIP:
            self.skip = 1
            return
        if tag == 'body':
            tag = 'div'
            attrs = list(attrs)
            background = next((value for key,value in attrs if key.lower()=='background' and value),None)
            bgcolor = next((value for key,value in attrs if key.lower()=='bgcolor' and value),None)
            extra = []
            if background:
                background = self.image_source(self.replace_cid(background))
                if url_scheme(background) in ('http','https') or re.match(r'^data:image/(?:png|jpeg|gif|webp);base64,',background,re.I):
                    extra.append('background-image:url('+json.dumps(background)+')')
            if bgcolor and re.fullmatch(r'#[0-9a-fA-F]{3,8}|[a-zA-Z]+',bgcolor): extra.append('background-color:'+bgcolor)
            if extra:
                style = next((value or '' for key,value in attrs if key.lower()=='style'),'')
                attrs = [(key,value) for key,value in attrs if key.lower() not in ('background','bgcolor','style')]
                attrs.append(('style',style+';'+ ';'.join(extra)))
        if tag not in TAGS: return
        values = []
        for key, value in attrs:
            key = key.lower()
            if key not in ATTRS or value is None: continue
            value = self.css_images(value) if key == 'style' else self.replace_cid(value)
            if key in ('src','background'): value = self.image_source(value)
            if key == 'srcset':
                entries=[]
                for entry in value.split(','):
                    bits=entry.strip().split()
                    if not bits or len(bits)>2 or (len(bits)==2 and not re.fullmatch(r'(?:\d+w|\d+(?:\.\d+)?x)',bits[1])): continue
                    source=self.image_source(bits[0])
                    if url_scheme(source) in ('http','https'): entries.append(' '.join([source,*bits[1:]]))
                value=', '.join(entries)
                if not value: continue
            if key in ('src','background','href'):
                scheme = url_scheme(value)
                if scheme not in ('http','https','mailto') and not re.match(r'^data:image/(?:png|jpeg|gif|webp);base64,',value,re.I): continue
                if key != 'href' and scheme == 'mailto': continue
            values.append(' ' + key + '="' + html.escape(value,quote=True) + '"')
        self.parts.append('<' + tag + ''.join(values) + '>')
        if tag == 'style': self.style = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag in SKIP: self.skip -= 1
            return
        if tag == 'body': tag = 'div'
        if tag in TAGS and tag not in VOID:
            self.parts.append('</' + tag + '>')
            if tag == 'style': self.style = False

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(self.css_images(data) if self.style else html.escape(data))


def prepare_html(message, html_parts):
    if not html_parts: return ''
    parser = EmailHTML(inline_images(message))
    try:
        parser.feed('\n'.join(html_parts)[:1000000])
        return ''.join(parser.parts)
    except Exception:
        return ''

def proxy_html_images(markup, proxy):
    parser = EmailHTML({},proxy=proxy)
    try:
        parser.feed(markup[:12*1024*1024])
        return ''.join(parser.parts)
    except Exception:
        return ''
