"""Invoice labels, shared management and associations."""
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import connect

router = APIRouter()


class TagName(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class InvoiceTags(BaseModel):
    tag_ids: list[int] = Field(max_length=500)


def tag_name(value):
    name = value.strip()
    if not name or any(ord(char) < 32 for char in name):
        raise HTTPException(422, '标签名称不能为空或包含换行等控制字符')
    return name


def add_invoice_tags(db, rows):
    by_id = {row['id']:row for row in rows}
    for row in rows: row['tags'] = []
    if not rows: return
    sql = '''SELECT it.invoice_id,t.id,t.name FROM invoice_tags it JOIN tags t ON t.id=it.tag_id
             WHERE it.invoice_id IN (''' + ','.join('?' * len(rows)) + ') ORDER BY t.id'
    for tag in db.execute(sql, tuple(by_id)):
        by_id[tag['invoice_id']]['tags'].append({'id':tag['id'], 'name':tag['name']})


@router.get('/api/tags')
def list_tags():
    with connect() as db:
        return [dict(row) for row in db.execute('''SELECT t.id,t.name,COUNT(it.invoice_id) invoice_count
            FROM tags t LEFT JOIN invoice_tags it ON it.tag_id=t.id GROUP BY t.id ORDER BY t.id''')]


@router.post('/api/tags', status_code=201)
def create_tag(value: TagName):
    name = tag_name(value.name)
    with connect() as db:
        try: ident = db.execute('INSERT INTO tags(name) VALUES(?)', (name,)).lastrowid
        except sqlite3.IntegrityError: raise HTTPException(409, '标签名称已存在') from None
    return {'id':ident, 'name':name, 'invoice_count':0}


@router.patch('/api/tags/{tag_id}')
def rename_tag(tag_id: int, value: TagName):
    name = tag_name(value.name)
    with connect() as db:
        try: updated = db.execute('UPDATE tags SET name=? WHERE id=?', (name,tag_id))
        except sqlite3.IntegrityError: raise HTTPException(409, '标签名称已存在') from None
        if not updated.rowcount: raise HTTPException(404, '标签不存在')
    return {'id':tag_id, 'name':name}


@router.delete('/api/tags/{tag_id}', status_code=204)
def delete_tag(tag_id: int):
    with connect() as db:
        if not db.execute('DELETE FROM tags WHERE id=?', (tag_id,)).rowcount:
            raise HTTPException(404, '标签不存在')


@router.put('/api/invoices/{invoice_id}/tags')
def update_invoice_tags(invoice_id: int, value: InvoiceTags):
    ids = list(dict.fromkeys(value.tag_ids))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM invoices WHERE id=?', (invoice_id,)).fetchone():
            raise HTTPException(404, '发票不存在')
        if ids and db.execute('SELECT COUNT(*) FROM tags WHERE id IN (' + ','.join('?' * len(ids)) + ')', ids).fetchone()[0] != len(ids):
            raise HTTPException(422, '部分标签已不存在，请刷新后重试')
        db.execute('DELETE FROM invoice_tags WHERE invoice_id=?', (invoice_id,))
        db.executemany('INSERT INTO invoice_tags(invoice_id,tag_id) VALUES(?,?)', [(invoice_id,ident) for ident in ids])
        rows = [{'id':invoice_id}]
        add_invoice_tags(db, rows)
        return {'tags':rows[0]['tags']}
