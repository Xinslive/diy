from sqlalchemy import func, select
from app.core.db import SessionLocal
from app.models.accounting import Subject, Voucher
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


# 插件按外部文件批量制证要用的那条配方，逐段单独测过：
# 读科目表 → 编码换 id → 整批建凭证 → 失败整批回滚。
# 这个插件代码就是 docs/plugin-data.md 里给出去的那一份，两者必须同源。
_BATCH_VOUCHER_PLUGIN = '''from fastapi import HTTPException


def register(registrar, ctx):
    def chart(book_id: int):
        return ctx.subject_read(book_id)

    def batch(book_id: int, payload: dict):
        rows = payload.get("rows") or []
        by_code = {s["code"]: s for s in ctx.subject_read(book_id)}
        made = []
        for r in rows:
            entries = []
            for e in r["entries"]:
                s = by_code.get(e["code"])
                if s is None:
                    raise HTTPException(400, "科目 %s 不在这个账套的科目表里" % e["code"])
                if not s["is_leaf"] or not s["active"]:
                    raise HTTPException(400, "科目 %s 不能记账" % e["code"])
                entries.append({"subject_id": s["id"], "direction": e["direction"],
                                "amount": e["amount"], "summary": e.get("summary", "")})
            made.append({"voucher_date": r["date"], "entries": entries,
                         "remark": r.get("remark", ""), "source": "IMPORT"})
        return ctx.voucher_batch_create(book_id, made)

    registrar.get("/chart", chart)
    registrar.post("/batch", batch)
'''


def _leaf_pair(db, book_id):
    """一个可用借方末级科目、一个可用贷方末级科目。
    不写死编码：标准科目表将来调整，写死的测试会跟着假失败。"""
    rows = db.execute(
        select(Subject.code, Subject.direction).where(
            Subject.book_id == book_id,
            Subject.is_leaf.is_(True),
            Subject.active.is_(True),
        )
    ).all()
    debit = next(r.code for r in rows if r.direction == "DEBIT")
    credit = next(r.code for r in rows if r.direction == "CREDIT")
    return debit, credit


def test_按科目编码批量建凭证_成功与整批回滚(plugin_env, client, auth, plain_auth, plain_user, book, db):
    """这是 ``voucher_create`` 第一次被真正跑通：此前唯一一条测试只断言空
    payload 返 422，没有任何一张真凭证被创建过的证据。

    覆盖三件事：科目编码能换成 subject_id、一批多张能连号建出来、
    中间一张不合法时**整批都不落库**（而不是留下半批）。"""
    build, restart = plugin_env
    pid = 'com.test.batchvoucher'
    raw, manifest, _, _ = build(
        plugin_id=pid, files={'backend/main.py': _BATCH_VOUCHER_PLUGIN.encode(),
                              'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['subject:read', 'voucher:write']))
    assert client.post('/api/system/plugins/install', files={'file': ('batch.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth, json={'user_ids': [plain_user.id]}).status_code == 200
    assert db.execute(select(UserBook).where(UserBook.user_id == plain_user.id,
                                            UserBook.book_id == book.id)).scalar() is None
    debit, credit = _leaf_pair(db, book.id)
    path = f'/api/plugins/{pid}'
    params = {'book_id': book.id}
    one = {"date": "2026-03-01", "entries": [
        {"code": debit, "direction": "DEBIT", "amount": "1.00", "summary": ""},
        {"code": credit, "direction": "CREDIT", "amount": "1.00", "summary": ""}]}

    # VIEWER 能读科目，但不能写凭证 —— 角色闸门对批量与单张一样。
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role='VIEWER'))
    db.commit()
    assert client.get(path + '/chart', params=params, headers=plain_auth).status_code == 200
    assert client.post(path + '/batch', params=params, headers=plain_auth,
                       json={"rows": [one]}).status_code == 403
    assert db.execute(select(func.count()).select_from(Voucher).where(Voucher.book_id == book.id)).scalar() == 0

    link = db.execute(select(UserBook).where(UserBook.user_id == plain_user.id,
                                             UserBook.book_id == book.id)).scalar()
    link.role = 'ACCOUNTANT'
    db.commit()

    chart = client.get(path + '/chart', params=params, headers=plain_auth).json()
    codes = {s['code'] for s in chart}
    assert {debit, credit} <= codes
    assert all({'id', 'code', 'name', 'direction', 'level', 'category', 'is_leaf', 'active'} <= set(s)
               for s in chart)

    def voucher(date, amount, summary):
        return {"date": date, "remark": summary, "entries": [
            {"code": debit, "direction": "DEBIT", "amount": amount, "summary": summary},
            {"code": credit, "direction": "CREDIT", "amount": amount, "summary": summary}]}

    r = client.post(path + '/batch', params=params, headers=plain_auth, json={"rows": [
        voucher('2026-03-01', '100.00', '第一张'),
        voucher('2026-03-02', '250.50', '第二张'),
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    # 整批一个事务：后一张必须接在前一张的号上，而不是都拿到同一个号
    # （create_voucher 里取号与撞号检查都在同一 session 内，flush 后对
    # 后续 SELECT 可见，所以连号成立）。
    assert [v['voucher_no'] for v in body] == ['记0001', '记0002']
    assert all(v['source'] == 'IMPORT' and v['status'] == 'UNPOSTED' for v in body)
    assert body[0]['total_debit'] == '100.00'

    made = db.execute(select(func.count()).select_from(Voucher).where(Voucher.book_id == book.id)).scalar()
    assert made == 2

    # 第三张借贷不平：整批回滚，库里一张都不能多。
    bad = {"date": "2026-03-03", "entries": [
        {"code": debit, "direction": "DEBIT", "amount": "10.00", "summary": "只有借方"},
        {"code": credit, "direction": "CREDIT", "amount": "9.00", "summary": "对不上"}]}
    good = voucher('2026-03-04', '80.00', '第四张')
    r = client.post(path + '/batch', params=params, headers=plain_auth, json={"rows": [good, bad]})
    assert r.status_code == 400, r.text
    assert '第 2 张' in r.json()['detail']
    assert '回滚' in r.json()['detail']
    assert db.execute(select(func.count()).select_from(Voucher).where(Voucher.book_id == book.id)).scalar() == 2

    # 文件里写了账套里没有的科目：要在建之前就报出来，同样不落库。
    r = client.post(path + '/batch', params=params, headers=plain_auth, json={"rows": [
        {"date": "2026-03-05", "entries": [
            {"code": "9999", "direction": "DEBIT", "amount": "1.00", "summary": "查无此科目"},
            {"code": credit, "direction": "CREDIT", "amount": "1.00", "summary": "查无此科目"}]}]})
    assert r.status_code == 400, r.text
    assert '9999' in r.json()['detail']
    assert db.execute(select(func.count()).select_from(Voucher).where(Voucher.book_id == book.id)).scalar() == 2


def test_批量与科目能力不随权限漏出():
    """新能力不能因为「加进词表」就默认人人可得。"""
    _, manifest, _, _ = package()
    manifest['permissions'] = ['subject:read', 'voucher:write']
    ctx = context_for(manifest)
    assert set(vars(ctx)) == {'plugin_id', 'subject_read', 'voucher_create', 'voucher_batch_create'}
    manifest['permissions'] = ['book:read']
    ctx = context_for(manifest)
    assert not hasattr(ctx, 'subject_read')
    assert not hasattr(ctx, 'voucher_batch_create')
