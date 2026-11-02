"""Retrieve invoice files over validated public HTTPS; never render a web page as an invoice."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from .formats import validate

MAX_BYTES = 30 * 1024 * 1024
MAX_HTML_BYTES = 1024 * 1024
DIRECT_EXTENSIONS = ('.pdf', '.ofd', '.xml')
INVOICE_HINT = re.compile(r'发票|票据|invoice', re.I)
DOWNLOAD_HINT = re.compile(r'下载|download|查看|获取|pdf|ofd|xml', re.I)
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'


class InvoiceLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.label = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            self.current = dict(attrs).get('href') or ''
            self.label = []

    def handle_data(self, data):
        if self.current is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'a' and self.current is not None:
            self.links.append((self.current, ''.join(self.label)))
            self.current = None


def direct_url(url):
    try: return urlsplit(url).path.lower().endswith(DIRECT_EXTENSIONS)
    except ValueError: return False


def candidates(text, html='', base_url=''):
    """Extract direct links and clearly labelled download anchors, without fetching here."""
    found = []
    def add(url, label=''):
        url = urljoin(base_url, unescape(url or '').strip()).rstrip('.,;，。；)）]')
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() != 'https' or not parts.hostname:
                return
        except ValueError:
            return
        if (direct_url(url) or (DOWNLOAD_HINT.search(label) and
                (INVOICE_HINT.search(label) or INVOICE_HINT.search(text) or INVOICE_HINT.search(parts.path)))) and url not in found:
            found.append(url)
    plain = unescape(text or '')
    for match in re.finditer(r'https://[^\s<>"\']+', plain, flags=re.I):
        context = plain[max(0, match.start()-80):match.start()]
        add(match.group(0), context if DOWNLOAD_HINT.search(context) else '')
    if html:
        parser = InvoiceLinks()
        parser.feed(html[:MAX_HTML_BYTES])
        for url, label in parser.links:
            add(url, label)
    return found[:5]


def public_socket(host, port, timeout):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses: raise ValueError('域名无法解析')
    ips = []
    for info in addresses:
        address = ipaddress.ip_address(info[4][0].split('%')[0])
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError('拒绝访问非公网地址')
        ips.append(str(address))
    return socket.create_connection((ips[0], port), timeout)

class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = public_socket(self.host, self.port, self.timeout)
        try: self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise

class PublicHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = public_socket(self.host,self.port,self.timeout)

def public_image(url):
    """Fetch bounded image bytes through validated, pinned public addresses."""
    from PIL import Image
    import io
    visited=set()
    for _ in range(4):
        if len(url)>4096 or url in visited: raise ValueError('图片链接无效')
        visited.add(url)
        try:
            parts=urlsplit(url)
            if parts.scheme.lower() not in ('http','https') or not parts.hostname or parts.username or parts.password:
                raise ValueError('图片链接无效')
            port=parts.port or (443 if parts.scheme.lower()=='https' else 80)
            if port not in (80,443): raise ValueError('图片端口不受支持')
        except ValueError: raise ValueError('图片链接无效') from None
        host=parts.hostname.encode('idna').decode('ascii')
        conn=PublicHTTPS(host,port,timeout=10,context=ssl.create_default_context()) if parts.scheme.lower()=='https' else PublicHTTP(host,port,timeout=10)
        try:
            path=parts.path or '/'
            if parts.query: path+='?'+parts.query
            conn.request('GET',path,headers={'Accept':'image/png,image/jpeg,image/gif,image/webp','User-Agent':USER_AGENT})
            response=conn.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location: raise ValueError('图片跳转无效')
                url=urljoin(url,location);continue
            if response.status!=200: raise ValueError('图片无法读取')
            content=response.read(5*1024*1024+1)
            if len(content)>5*1024*1024: raise ValueError('图片过大')
            with Image.open(io.BytesIO(content)) as image:
                media={'PNG':'image/png','JPEG':'image/jpeg','GIF':'image/gif','WEBP':'image/webp'}.get(image.format)
                if not media or image.width*image.height>30_000_000: raise ValueError('图片格式无效')
                image.verify()
            return content,media
        finally:
            conn.close()
    raise ValueError('图片跳转次数过多')


def file_suffix(data):
    if data.startswith(b'%PDF-'):
        return '.pdf'
    if data.startswith(b'PK\x03\x04'):
        try:
            validate('invoice.ofd', data)
            return '.ofd'
        except ValueError:
            return None
    try:
        validate('invoice.xml', data)
        # HTML landing pages must never be stored as XML invoices.
        from .formats import read_xml
        if read_xml(data).tag.split('}')[-1].lower() not in ('html', 'svg'):
            return '.xml'
    except ValueError:
        pass
    return None


def platform_pdf(url):
    """Resolve the PDF offered by the scan-invoice page without executing its scripts."""
    parts = urlsplit(url)
    if not parts.path.endswith('/scan-invoice/printQrcode'):
        return None
    query = parse_qs(parts.query)
    if len(query.get('paramList', [])) != 1:
        return None
    parameter = quote(query['paramList'][0], safe="-_.!~*'()")
    payload = urlencode({'paramList':parameter, 'code':parameter,
                         'aliView':query.get('aliView',[''])[0],
                         'invoiceDetailMiddleUri':parts.path.rsplit('/',1)[-1]+'?'+parts.query,
                         'shortLinkSource':query.get('shortLinkSource',[''])[0],
                         '_timestamp':str(int(time.time()*1000))})
    conn = PublicHTTPS(parts.hostname.encode('idna').decode('ascii'), 443, timeout=15, context=ssl.create_default_context())
    try:
        conn.request('POST', '/sapi/scan2/getIvcDetailShow.do', body=payload,
                     headers={'Accept':'*/*', 'User-Agent':USER_AGENT,
                              'Content-Type':'application/x-www-form-urlencoded', 'Referer':url})
        response = conn.getresponse()
        if response.status != 200: return None
        data = response.read(MAX_HTML_BYTES+1)
        if len(data) > MAX_HTML_BYTES: raise ValueError('下载内容超过大小限制')
        try:
            result = json.loads(data)
            file_url = result['data']['invoiceSimpleVo']['url']
            return file_url if isinstance(file_url, str) else None
        except (ValueError, KeyError, TypeError):
            return None
    finally:
        conn.close()


def download(url):
    visited = set()
    followed_page = False
    for _ in range(6):
        if len(url) > 4096 or url in visited: raise ValueError('链接过长或循环跳转')
        visited.add(url)
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in (None,443):
                raise ValueError('链接必须是公网 HTTPS 地址')
        except ValueError as exc:
            raise ValueError('链接必须是公网 HTTPS 地址') from exc
        host = parts.hostname.encode('idna').decode('ascii')
        conn = PublicHTTPS(host,443,timeout=15,context=ssl.create_default_context())
        try:
            target = parts.path or '/'
            if parts.query: target += '?' + parts.query
            conn.request('GET', target, headers={'Accept':'*/*','User-Agent':USER_AGENT})
            response = conn.getresponse()
            if response.status in (301,302,303,307,308):
                location = response.getheader('Location')
                if not location: raise ValueError('链接跳转无效')
                url = urljoin(url,location)
                continue
            if response.status != 200: raise ValueError('下载未成功')
            content_type = (response.getheader('Content-Type') or '').lower()
            is_html = 'text/html' in content_type
            limit = MAX_HTML_BYTES if is_html else MAX_BYTES
            data = response.read(limit+1)
            if len(data)>limit: raise ValueError('下载内容超过大小限制')
            suffix = file_suffix(data)
            if suffix:
                return '邮件链接发票'+suffix, data
            if not followed_page and (is_html or data.lstrip().lower().startswith((b'<!doctype html',b'<html'))):
                followed_page = True
                platform_url = platform_pdf(url)
                if platform_url:
                    url = urljoin(url, platform_url)
                    continue
                page = data.decode('utf-8', errors='replace')
                links = candidates('发票下载', page, url)
                if links:
                    url = links[0]
                    continue
            raise ValueError('需要登录或手动下载发票')
        finally:
            conn.close()
    raise ValueError('链接跳转次数过多')
