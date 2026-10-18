from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.system import UserBook
from app.plugins.installer import install
from app.plugins.manifest import table_prefix
from app.plugins.public_api import context_for, current_principal
from tests.plugin_fixtures import plugin_env
from tests.plugin_helpers import package
from fastapi import HTTPException
import pytest


def test_context_exposes_only_declared_capabilities_and_requires_request():
    _, manifest, _, _ = package()
    ctx = context_for(manifest)
    assert vars(ctx) == {'plugin_id': manifest['id']}
    manifest['permissions'] = ['book:read']
    ctx = context_for(manifest)
    assert set(vars(ctx)) == {'plugin_id', 'book_read'}
    with pytest.raises(HTTPException):
        ctx.book_read(1)


def test_plugin_data_book_scope_roles_and_host_helpers(plugin_env, client, auth, plain_auth, plain_user, book, db):
    build, restart = plugin_env
    pid = 'com.test.scoped'
    name = table_prefix(pid) + 'items'
    code = f'''def register(registrar, ctx):
    def rows(book_id: int):
        return ctx.data.read("{name}", book_id=book_id)
    def insert(book_id: int, payload: dict):
        return ctx.data.insert("{name}", payload, book_id=book_id)
    def update(book_id: int, ident: int, payload: dict):
        return ctx.data.update("{name}", ident, payload, book_id=book_id)
    def host_book(book_id: int):
        return ctx.book_read(book_id)
    def ledger(book_id: int, period_id: int):
        return ctx.ledger_read(book_id, period_id)
    def voucher(book_id: int, payload: dict):
        return ctx.voucher_create(book_id, payload)
    registrar.get("/rows", rows)
    registrar.post("/rows", insert)
    registrar.add("/rows", update, ["PUT"])
    registrar.get("/book", host_book)
    registrar.get("/ledger", ledger)
    registrar.post("/voucher", voucher)
'''.encode()
    raw, manifest, _, _ = build(plugin_id=pid, tables=True, files={'backend/main.py': code, 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['plugin_data:rw', 'book:read', 'ledger:read', 'voucher:write']))
    assert client.post('/api/system/plugins/install', files={'file': ('scope.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth, json={'user_ids': [plain_user.id]}).status_code == 200
    path = f'/api/plugins/{pid}'
    assert client.get(path + '/rows', params={'book_id': book.id}, headers=plain_auth).status_code == 403
    link = UserBook(user_id=plain_user.id, book_id=book.id, role='VIEWER')
    db.add(link); db.commit()
    assert client.get(path + '/book', params={'book_id': book.id}, headers=plain_auth).json()['id'] == book.id
    assert client.get(path + '/rows', params={'book_id': book.id}, headers=plain_auth).json() == []
    assert client.post(path + '/rows', params={'book_id': book.id}, json={'label': 'x'}, headers=plain_auth).status_code == 403
    link.role = 'ACCOUNTANT'; db.commit()
    r = client.post(path + '/rows', params={'book_id': book.id}, json={'label': 'x'}, headers=plain_auth)
    assert r.status_code == 200, r.text
    ident = r.json()['id']
    assert r.json()['book_id'] == book.id
    assert client.post(path + '/rows', params={'book_id': book.id}, json={'label': 'x', 'book_id': book.id + 1}, headers=plain_auth).status_code == 400
    assert client.put(path + '/rows', params={'book_id': book.id, 'ident': ident}, json={'book_id': book.id + 1}, headers=plain_auth).status_code == 400
    assert client.put(path + '/rows', params={'book_id': book.id, 'ident': ident}, json={'label': 'y'}, headers=plain_auth).json() == {'updated': 1}
    # 宿主 voucher schema 和角色均未被绕过：错误凭证不产生任何会计写入。
    r = client.post(path + '/voucher', params={'book_id': book.id}, json={}, headers=plain_auth)
    assert r.status_code == 422
    assert client.get('/api/health').status_code == 200
