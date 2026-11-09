"""Deliver invoice summaries through configured push platforms."""
import hashlib
import http.client
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, Field, SecretStr

from .db import connect
from .service import cipher, now


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: Literal['wxpusher', 'wecom', 'serverchan']
    enabled: bool = True
    secret: SecretStr = SecretStr('')
    uids: str = Field(default='', max_length=4096)
    topics: str = Field(default='', max_length=100)
    corp_id: str = Field(default='', max_length=128)
    agent_id: str = Field(default='', max_length=12)
    users: str = Field(default='', max_length=4096)


def _split(value):
    return list(dict.fromkeys(part for part in re.split(r'[\s,，|]+', value) if part))


def _options(value):
    if value.provider == 'wxpusher':
        uids, topics = _split(value.uids), _split(value.topics)
        if not uids and not topics:
            raise ValueError('请至少填写一个 UID 或主题 ID')
        if any(not re.fullmatch(r'UID_[\w-]{1,100}', uid, re.ASCII) for uid in uids):
            raise ValueError('WxPusher UID 格式不正确')
        if len(topics) > 5 or any(not re.fullmatch(r'[1-9][0-9]{0,9}', topic) for topic in topics):
            raise ValueError('主题 ID 应为正整数，最多填写 5 个')
        return {'uids':uids, 'topics':[int(topic) for topic in topics]}
    if value.provider == 'wecom':
        corp_id, agent_id, users = value.corp_id.strip(), value.agent_id.strip(), _split(value.users)
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', corp_id) or not re.fullmatch(r'[1-9][0-9]{0,9}', agent_id):
            raise ValueError('请填写有效的企业 ID 和 AgentId')
        if not users or len(users) > 1000 or ('@all' in users and len(users) != 1):
            raise ValueError('请填写成员 UserID，或单独填写 @all')
        if users != ['@all'] and any(not re.fullmatch(r'[\w@.\-/]{1,128}', user, re.ASCII) for user in users):
            raise ValueError('企业微信成员 UserID 格式不正确')
        return {'corp_id':corp_id, 'agent_id':int(agent_id), 'users':users}
    return {}


def _public(row):
    result = {key:row[key] for key in ('id','name','provider','last_result','last_attempt_at')}
    result.update(enabled=bool(row['enabled']), options=json.loads(row['options']), has_secret=bool(row['secret']))
    return result


def channels():
    with connect() as db:
        return [_public(row) for row in db.execute('SELECT * FROM notification_channels ORDER BY id')]


def save_channel(value, channel_id=None):
    name = value.name.strip()
    if not name:
        raise ValueError('请填写渠道名称')
    options = json.dumps(_options(value), ensure_ascii=False)
    secret = value.secret.get_secret_value().strip()
    if secret and not re.fullmatch(r'[a-zA-Z0-9_-]{1,512}', secret):
        raise ValueError('推送凭证格式不正确')
    if secret and value.provider == 'serverchan' and not re.fullmatch(r'(?:SCT[a-zA-Z0-9_-]+|sctp[1-9][0-9]{0,19}t[a-zA-Z0-9_-]+)', secret):
        raise ValueError('请填写 Server酱 Turbo 或 Server酱³ 的 SendKey')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if channel_id is None:
            if not secret:
                raise ValueError('请填写推送凭证')
            if db.execute('SELECT count(*) FROM notification_channels').fetchone()[0] >= 20:
                raise ValueError('最多配置 20 个通知渠道')
            channel_id = db.execute('''INSERT INTO notification_channels(name,provider,enabled,options,secret)
                VALUES(?,?,?,?,?)''', (name,value.provider,value.enabled,options,cipher().encrypt(secret.encode()).decode())).lastrowid
        else:
            row = db.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone()
            if row is None:
                raise LookupError
            if row['provider'] != value.provider:
                raise ValueError('请新建渠道以使用其他推送平台')
            encrypted = cipher().encrypt(secret.encode()).decode() if secret else row['secret']
            changed_destination = options != row['options'] or bool(secret)
            db.execute('UPDATE notification_channels SET name=?,enabled=?,options=?,secret=? WHERE id=?',
                       (name,value.enabled,options,encrypted,channel_id))
            if changed_destination or not value.enabled:
                db.execute('UPDATE notification_deliveries SET pending=0 WHERE channel_id=? AND pending=1',(channel_id,))
            if changed_destination:
                db.execute('UPDATE notification_channels SET last_result=NULL,last_attempt_at=NULL WHERE id=?',(channel_id,))
        return _public(db.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone())


def delete_channel(channel_id):
    with connect() as db:
        if not db.execute('DELETE FROM notification_channels WHERE id=?',(channel_id,)).rowcount:
            raise LookupError


class PushError(Exception):
    pass


def _request_json(host, path, *, payload=None, form=None):
    method, body, headers = 'GET', None, {}
    if payload is not None:
        method, body = 'POST', json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    elif form is not None:
        method, body = 'POST', urlencode(form).encode('ascii')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    connection = http.client.HTTPSConnection(host, timeout=6)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise PushError(f'平台返回 HTTP {response.status}')
        raw = response.read(65537)
        data = json.loads(raw) if len(raw) <= 65536 else None
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, http.client.HTTPException):
        raise PushError('无法连接推送平台，请检查网络后重试') from None
    except ValueError:
        raise PushError('推送平台返回了无法读取的响应') from None
    finally:
        connection.close()


def _check_code(response, field, expected):
    code = response.get(field)
    if type(code) is int and code == expected:
        return
    detail = f'（错误码 {code}）' if type(code) is int else ''
    raise PushError(f'平台拒绝了请求{detail}，请检查凭证、接收人和平台配置')


_wecom_tokens = OrderedDict()
_token_lock = threading.Lock()


def _wecom_token(corp_id, secret, *, refresh=False):
    key = (corp_id, hashlib.sha256(secret.encode()).digest())
    with _token_lock:
        cached = _wecom_tokens.get(key)
    if cached and cached[1] > time.monotonic() and not refresh:
        return cached[0]
    query = urlencode({'corpid':corp_id,'corpsecret':secret})
    response = _request_json('qyapi.weixin.qq.com', '/cgi-bin/gettoken?' + query)
    _check_code(response, 'errcode', 0)
    token, expires = response.get('access_token'), response.get('expires_in')
    if not isinstance(token, str) or not token or type(expires) is not int:
        raise PushError('企业微信未返回有效的访问令牌')
    with _token_lock:
        _wecom_tokens[key] = (token,time.monotonic() + max(0, expires - 60))
        _wecom_tokens.move_to_end(key)
        if len(_wecom_tokens) > 20:
            _wecom_tokens.popitem(last=False)
    return token


def _send(row, title, message):
    try:
        secret = cipher().decrypt(row['secret'].encode()).decode()
        options = json.loads(row['options'])
    except Exception:
        raise PushError('渠道配置无法读取，请重新保存') from None
    if row['provider'] == 'wxpusher':
        response = _request_json('wxpusher.zjiecode.com', '/api/send/message', payload={
            'appToken':secret, 'summary':title, 'content':message, 'contentType':1,
            'uids':options['uids'], 'topicIds':options['topics'],
        })
        _check_code(response, 'code', 1000)
    elif row['provider'] == 'wecom':
        payload = {'touser':'|'.join(options['users']), 'agentid':options['agent_id'],
                   'msgtype':'text', 'text':{'content':f'{title}\n{message}'}}
        for refresh in (False, True):
            token = _wecom_token(options['corp_id'], secret, refresh=refresh)
            response = _request_json('qyapi.weixin.qq.com',
                                     '/cgi-bin/message/send?' + urlencode({'access_token':token}),
                                     payload=payload)
            if response.get('errcode') not in (40014, 42001):
                break
        _check_code(response, 'errcode', 0)
        if response.get('invaliduser') or response.get('unlicenseduser'):
            raise PushError('部分成员无法接收，请核对 UserID 和应用可见范围')
    else:
        # Validate again here because the SendKey determines the Server酱³ host.
        sc3 = re.fullmatch(r'sctp([1-9][0-9]{0,19})t[a-zA-Z0-9_-]+', secret)
        if sc3:
            host, path = f'{sc3[1]}.push.ft07.com', f'/send/{secret}.send'
        elif re.fullmatch(r'SCT[a-zA-Z0-9_-]+', secret):
            host, path = 'sctapi.ftqq.com', f'/{secret}.send'
        else:
            raise PushError('Server酱 SendKey 格式不正确')
        # Invoice fields are plain text; do not turn a merchant name into Markdown links or HTML.
        description = '\n\n'.join(re.sub(r'([\\`*_{}\[\]<>#!|])', r'\\\1', line) for line in message.splitlines())
        _check_code(_request_json(host, path, form={'title':title,'desp':description}), 'code', 0)


def _deliver(row, title, message):
    error = None
    try:
        _send(row, title, message)
    except PushError as exc:
        error = str(exc)
    except Exception:
        error = '推送失败，请检查渠道设置'
    with connect() as db:
        db.execute('''UPDATE notification_channels SET last_result=?,last_attempt_at=?
            WHERE id=? AND options=? AND secret=?''',
                   (error or '平台已受理',now(),row['id'],row['options'],row['secret']))
    return error


def test_channel(channel_id):
    with connect() as db:
        row = db.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone()
    if row is None:
        raise LookupError
    sample = {'buyer':'示例购买方公司','buyer_tax_id':'T'*18,'seller':'示例销售方公司',
              'seller_tax_id':'S'*18,'amount_cents':12345,'invoice_date':'2026-01-15','invoice_number':'12345678901234567890'}
    message = '这是一条测试消息。以下为虚拟发票示例，非真实发票。\n\n' + _invoice_detail(sample, 1)
    return _deliver(row, '票序 · 测试通知', message)


def _invoice_detail(item, index):
    def text(key):
        value = ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', str(item.get(key) or '')).split())
        return (value[:120] + ('…' if len(value)>120 else '')) or '未识别'
    amount = item.get('amount_cents')
    amount = f'¥{amount//100:,}.{amount%100:02d}' if type(amount) is int and amount>=0 else '未识别'
    return (f'发票 {index}\n购买方：{text("buyer")}\n购买方税号：{text("buyer_tax_id")}\n'
            f'销售方：{text("seller")}\n销售方税号：{text("seller_tax_id")}\n'
            f'含税金额：{amount}\n开票日期：{text("invoice_date")}\n发票号码：{text("invoice_number")}')


def _message(event, count, details, byte_limit):
    if event=='received':
        header, footer = f'新增 {count} 张已识别发票。', '请到票序发票库查看。'
    else:
        header = f'{count} 张发票自动识别失败。' if count else '邮件中的附件或链接处理失败，尚未提取到发票信息。'
        footer = '以上信息待核对，请到票序「识别失败」查看。'
    blocks = []
    for index, item in enumerate(details, 1):
        block = _invoice_detail(item, index)
        remaining = f'其余 {len(details)-index} 张明细请到票序查看。'
        if len('\n\n'.join([header,*blocks,block,remaining,footer]).encode('utf-8')) > byte_limit:
            break
        blocks.append(block)
    if len(blocks)<len(details): blocks.append(f'其余 {len(details)-len(blocks)} 张明细请到票序查看。')
    return '\n\n'.join([header,*blocks,footer])


def queue_notification(db, email_id, event, invoice_ids):
    details = []
    if invoice_ids:
        columns = 'buyer,seller,buyer_tax_id,seller_tax_id,amount_cents,invoice_date,invoice_number'
        details = [dict(row) for row in db.execute(f'''SELECT {columns} FROM invoices
            WHERE id IN ({','.join('?'*len(invoice_ids))}) ORDER BY id''', invoice_ids)]
    db.execute('''INSERT OR IGNORE INTO notification_deliveries(email_id,channel_id,event,invoice_count,details)
        SELECT ?,id,?,?,? FROM notification_channels WHERE enabled=1''',
               (email_id,event,len(details),json.dumps(details,ensure_ascii=False)))


def deliver_next():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('''SELECT c.*,d.id AS delivery_id,d.event,d.invoice_count,d.details
            FROM notification_deliveries d JOIN notification_channels c ON c.id=d.channel_id
            WHERE d.pending=1 AND c.enabled=1 ORDER BY d.id LIMIT 1''').fetchone()
        if row is None:
            return False
        # Claim before sending: after a timeout or restart, the platform may already
        # have accepted the message. Do not deliver the same alert twice.
        db.execute('UPDATE notification_deliveries SET pending=0 WHERE id=?',(row['delivery_id'],))
        db.execute('UPDATE notification_channels SET last_result=?,last_attempt_at=? WHERE id=?',
                   ('发送结果待确认',now(),row['id']))
    if row['event'] == 'received':
        title = '票序 · 收到新发票'
    else:
        title = '票序 · 发票自动识别失败'
    limit = 2048-len((title+'\n').encode('utf-8')) if row['provider']=='wecom' else 30000
    message = _message(row['event'],row['invoice_count'],json.loads(row['details']),limit)
    _deliver(row, title, message)
    return True
