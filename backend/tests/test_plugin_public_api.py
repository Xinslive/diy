from sqlalchemy import func, select
import hashlib
from datetime import datetime, timezone
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
    # ``period_list`` 与 ``book_read`` / ``book_list`` 同一个权限位：
    # 它只是「这本账有哪些会计期间」，也是读。
    assert set(vars(ctx)) == {'plugin_id', 'book_read', 'book_list', 'period_list'}
    with pytest.raises(HTTPException):
        ctx.book_read(1)
    manifest['permissions'] = ['upload:read']
    ctx = context_for(manifest)
    assert set(vars(ctx)) == {'plugin_id', 'upload_read'}
    with pytest.raises(HTTPException):
        ctx.upload_read(object())


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
    def host_books():
        return ctx.book_list()
    def ledger(book_id: int, period_id: int):
        return ctx.ledger_read(book_id, period_id)
    def voucher(book_id: int, payload: dict):
        return ctx.voucher_create(book_id, payload)
    registrar.get("/rows", rows)
    registrar.post("/rows", insert)
    registrar.add("/rows", update, ["PUT"])
    registrar.get("/book", host_book)
    registrar.get("/books", host_books)
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
    books = client.get(path + '/books', headers=plain_auth)
    assert books.status_code == 200, books.text
    assert books.json() == [{
        'id': book.id, 'name': book.name, 'company_name': book.company_name,
        'status': book.status, 'role': 'VIEWER', 'can_write': False,
    }]
    assert client.get(path + '/book', params={'book_id': book.id}, headers=plain_auth).json()['id'] == book.id
    assert client.get(path + '/rows', params={'book_id': book.id}, headers=plain_auth).json() == []
    assert client.post(path + '/rows', params={'book_id': book.id}, json={'label': 'x'}, headers=plain_auth).status_code == 403
    link.role = 'ACCOUNTANT'; db.commit()
    assert client.get(path + '/books', headers=plain_auth).json()[0]['can_write'] is True
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


def test_plugin_upload_read_returns_bounded_content_and_metadata(plugin_env, client, auth, plain_auth, plain_user):
    build, restart = plugin_env
    pid = 'com.test.upload'
    code = '''from fastapi import File, UploadFile


def register(registrar, ctx):
    def upload(file: UploadFile = File(...)):
        value = ctx.upload_read(file)
        return {key: value[key] for key in ("filename", "content_type", "size", "sha256")}

    registrar.post("/upload", upload)
'''.encode()
    raw, manifest, _, _ = build(
        plugin_id=pid,
        files={'backend/main.py': code, 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['upload:read']),
    )
    assert client.post('/api/system/plugins/install', files={'file': ('upload.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [plain_user.id]}).status_code == 200

    content = b'hello plugin'
    response = client.post(
        f'/api/plugins/{pid}/upload',
        files={'file': ('../hello.txt', content, 'text/plain')},
        headers=plain_auth,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        'filename': 'hello.txt',
        'content_type': 'text/plain',
        'size': len(content),
        'sha256': hashlib.sha256(content).hexdigest(),
    }


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


# ---------------------------------------------------------------------------
# 1.3.0 新增的两条能力：配对（entry:read）与导出（export:write）
#
# **为什么这两条要在这里单独测，而不是只靠共享卡那一份端到端用例**：
# 共享卡的用例住在 ``tests/test_plugin_sharedcard.py``，而那个文件在**干净
# checkout 里会被跳过**（插件源码 ``/plugins/`` 不入库）。只靠它，这两条新
# 能力在 CI 上就是**零覆盖** —— 推上线的��东没有任何机器证据。所以宿主这一侧
# 单独钉一遍：能力是否按权限露脸、配对是不是用宿主那把尺子、导出是不是真
# .xlsx。
# ---------------------------------------------------------------------------

_MATCH_PLUGIN = '''def register(registrar, ctx):
    def match(book_id: int, payload: dict):
        return ctx.entry_match(book_id, payload.get("codes") or [], payload.get("rows") or [])

    registrar.post("/match", match)
'''

_EXPORT_PLUGIN = '''def register(registrar, ctx):
    def export(payload: dict):
        return ctx.export_xlsx(payload.get("filename") or "",
                               payload.get("columns") or [],
                               payload.get("rows") or [],
                               payload.get("sheet_name") or "Sheet1")

    registrar.post("/export", export)
'''


def test_配对能力用宿主那把尺子_并按权限露脸(plugin_env, client, auth, plain_auth,
                                           plain_user, book, db, make_voucher):
    """``ctx.entry_match`` 配的是「账上已经记过的那一笔」。

    **为什么宿主出这条能力**：插件读不到分录表，而「一行流水对应账上哪张凭证」
    这件事宿主的流水对账那条线早就在做（``app/bankstatement.allocate``）——
    插件自己再写一份就是第二套口径。所以这里要钉住的是：**宿主真的用自己那把
    尺子**，而且**按权限露脸**（``entry:read`` 没声明就不给这条能力）。
    """
    # 账上记一笔：2026-09-12 收钱 9,780（现金侧是借方 —— 收钱 = 余额增加）。
    voucher = make_voucher('2026-09-12',
                            dr=[('1002', '9780.00', '收到代扣款')],
                            cr=[('2202', '9780.00', '代扣个人所得税')])
    assert voucher is not None

    build, restart = plugin_env
    pid = 'com.test.match'
    raw, manifest, _, _ = build(
        plugin_id=pid,
        files={'backend/main.py': _MATCH_PLUGIN.encode(),
               'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['entry:read']))
    assert client.post('/api/system/plugins/install', files={'file': ('match.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [plain_user.id]}).status_code == 200
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT'))
    db.commit()

    path = f'/api/plugins/{pid}/match'
    rows = [
        {'id': 1, 'tx_date': '2026-09-12', 'amount': '9780.00', 'direction': 'IN'},
        # 同一笔钱、方向对、但日期差 20 天：金额对得上，**不是同一笔**（要人判）。
        {'id': 2, 'tx_date': '2026-10-02', 'amount': '9780.00', 'direction': 'IN'},
        # 日期差 40 天：出了复核窗口，连「像」都不像。
        {'id': 3, 'tx_date': '2026-10-22', 'amount': '9780.00', 'direction': 'IN'},
        # 账上压根没这笔钱。
        {'id': 4, 'tx_date': '2026-09-12', 'amount': '123.45', 'direction': 'IN'},
        # 方向对不上：收钱 vs 付钱不是同一笔。
        {'id': 5, 'tx_date': '2026-09-12', 'amount': '9780.00', 'direction': 'OUT'},
    ]
    response = client.post(path, params={'book_id': book.id}, json={'codes': ['1002'], 'rows': rows},
                           headers=plain_auth)
    assert response.status_code == 200, response.text
    body = response.json()
    pairs = {pair['row_id']: pair for pair in body['pairs']}
    # 只有第 1 行配上，且**在主窗口内**（同一笔）。
    assert set(pairs) == {1}, body
    assert pairs[1]['in_primary'] is True, body
    assert pairs[1]['days_diff'] == 0, body
    assert pairs[1]['voucher_no'] == voucher.voucher_no, body
    assert pairs[1]['amount'] == '9780.00', body
    # 没配上的三行，宿主要**点名**说出来（插件据此报「待复核」）。
    assert sorted(body['unmatched_row_ids']) == [2, 3, 4, 5], body

    # 换个科目：这条分录不在 1001 之下，就不该配上。
    response = client.post(path, params={'book_id': book.id},
                           json={'codes': ['1001'], 'rows': [rows[0]]}, headers=plain_auth)
    assert response.status_code == 200, response.text
    assert response.json()['pairs'] == [], response.json()

    # **权限露脸**：没声明 ``entry:read`` 的插件拿不到这条能力。
    _, manifest, _, _ = package()
    manifest['permissions'] = ['book:read']
    ctx = context_for(manifest)
    assert not hasattr(ctx, 'entry_match')

    # **账套边界**：别人的账配不了（``get_my_book`` 那一道闸）。
    response = client.post(path, params={'book_id': book.id + 999},
                           json={'codes': ['1002'], 'rows': [rows[0]]}, headers=plain_auth)
    assert response.status_code in (403, 404), response.text


def test_导出能力在宿主生成真xlsx_并按权限露脸(plugin_env, client, auth, plain_auth,
                                             plain_user, book, db):
    """``ctx.export_xlsx`` 回传的是**真的 .xlsx**，不是一张 CSV 改个后缀。

    **为什么在宿主生成**：``.xlsx`` 是 ZIP 包，插件沙箱只放行
    ``openpyxl.load_workbook``（读）不放行 ``Workbook``（写）。所以这条能力
    必须真的造出一个能被 openpyxl 读回来的工作簿 —— 断言的是**文件内容**，
    不是返回值里那个 base64 字符串的长度。
    """
    import base64
    import io

    from openpyxl import load_workbook

    build, restart = plugin_env
    pid = 'com.test.export'
    raw, manifest, _, _ = build(
        plugin_id=pid,
        files={'backend/main.py': _EXPORT_PLUGIN.encode(),
               'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['export:write']))
    assert client.post('/api/system/plugins/install', files={'file': ('export.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [plain_user.id]}).status_code == 200
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT'))
    db.commit()

    response = client.post(f'/api/plugins/{pid}/export', headers=plain_auth, json={
        'filename': '对账单',
        'sheet_name': '流水明细',
        'columns': [{'key': 'seq', 'title': '行', 'width': 6},
                    {'key': 'amount', 'title': '金额'},
                    {'key': 'note', 'title': '备注'}],
        'rows': [{'seq': 1, 'amount': '9780.00'},
                 {'seq': 2, 'amount': '123.45', 'note': '出纳待确认'}],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # 文件名自己补后缀：前端拿到什么就下什么，补后缀是宿主的事。
    assert body['filename'] == '对账单.xlsx', body
    blob = base64.b64decode(body['content_base64'])
    assert blob[:2] == b'PK', '不是 ZIP 头 —— 那就不是 .xlsx'
    sheet = load_workbook(io.BytesIO(blob))
    assert sheet.sheetnames == ['流水明细'], sheet.sheetnames
    ws = sheet['流水明细']
    assert [cell.value for cell in ws[1]] == ['行', '金额', '备注']
    # **取不到的键留空，不猜**：这一行没有「备注」，格子里就该是空的。
    assert [cell.value for cell in ws[2]] == [1, '9780.00', None]
    assert [cell.value for cell in ws[3]] == [2, '123.45', '出纳待确认']

    # **权限露脸**：没声明 ``export:write`` 的插件拿不到这条能力。
    _, manifest, _, _ = package()
    manifest['permissions'] = ['book:read']
    ctx = context_for(manifest)
    assert not hasattr(ctx, 'export_xlsx')


def test_plugin_data_read_supports_filter_paging_and_count(plugin_env, client, auth, plain_auth, plain_user, book, db):
    """过滤 + 分页 + count：插件不必把整表读进内存自己筛。"""
    build, restart = plugin_env
    pid = 'com.test.paged'
    name = table_prefix(pid) + 'items'
    code = f'''def register(registrar, ctx):
    def rows(book_id: int, payload: dict):
        return ctx.data.read("{name}", book_id=book_id, limit=payload.get("limit", 100),
            offset=payload.get("offset", 0), where=payload.get("where"), order_by=payload.get("order_by"))
    def count(book_id: int, payload: dict):
        return ctx.data.count("{name}", book_id=book_id, where=payload.get("where"))
    def insert(book_id: int, payload: dict):
        return ctx.data.insert("{name}", payload, book_id=book_id)
    registrar.post("/rows", rows)
    registrar.post("/count", count)
    registrar.post("/insert", insert)
'''.encode()
    raw, manifest, _, _ = build(plugin_id=pid, tables=True,
        files={'backend/main.py': code, 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['plugin_data:rw', 'book:read']))
    assert client.post('/api/system/plugins/install', files={'file': ('paged.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth, json={'user_ids': [plain_user.id]}).status_code == 200
    link = UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT')
    db.add(link); db.commit()

    for label in ('alpha', 'beta', 'gamma'):
        r = client.post(f'/api/plugins/{pid}/insert', params={'book_id': book.id}, json={'label': label}, headers=plain_auth)
        assert r.status_code == 200, r.text

    path = f'/api/plugins/{pid}'
    # 等值过滤 + 计数。
    r = client.post(path + '/rows', params={'book_id': book.id}, json={'where': {'label': 'beta'}}, headers=plain_auth)
    assert r.status_code == 200, r.text
    assert [row['label'] for row in r.json()] == ['beta']
    r = client.post(path + '/count', params={'book_id': book.id}, json={'where': {'label': 'beta'}}, headers=plain_auth)
    assert r.json() == 1
    r = client.post(path + '/count', params={'book_id': book.id}, json={'where': None}, headers=plain_auth)
    assert r.json() == 3
    # 载体形参必填：空 body 是调用方的错，给 422 而不是让插件在半路炸成 500。
    r = client.post(path + '/count', params={'book_id': book.id}, json={}, headers=plain_auth)
    assert r.status_code == 422
    # 比较操作符与 in。
    r = client.post(path + '/rows', params={'book_id': book.id}, json={'where': {'label': {'in': ['alpha', 'gamma']}}}, headers=plain_auth)
    assert sorted(row['label'] for row in r.json()) == ['alpha', 'gamma']
    # 分页与排序。
    r = client.post(path + '/rows', params={'book_id': book.id}, json={'limit': 2, 'order_by': 'id desc'}, headers=plain_auth)
    assert [row['label'] for row in r.json()] == ['gamma', 'beta']
    r = client.post(path + '/rows', params={'book_id': book.id}, json={'limit': 2, 'offset': 2}, headers=plain_auth)
    assert [row['label'] for row in r.json()] == ['gamma']
    # 未知字段、非法操作符与非法值都被拒绝（没有拼 SQL 的入口）。
    for where in ({'nope': 1}, {'label': {'raw': '1=1'}}, {'label': {'in': 'not-a-list'}}):
        r = client.post(path + '/rows', params={'book_id': book.id}, json={'where': where}, headers=plain_auth)
        assert r.status_code == 400, (where, r.text)
    # 过滤条件改不掉账套范围。
    r = client.post(path + '/rows', params={'book_id': book.id}, json={'where': {'book_id': book.id + 1}}, headers=plain_auth)
    assert r.json() == []


def test_插件表反射只做一次_重复读不再反射(plugin_env, client, auth, plain_auth, plain_user, book, db, monkeypatch):
    """同一张表**只反射一次**，之后复用。

    **为什么这件事值得一条用例**：反射（``autoload``）是每次读表都要重付一遍
    的固定开销 —— 实测（生产同款机器）每张表约 21 ms，复用反射结果后的一次
    查询只要约 3.5 ms。插件默认在**宿主进程内**跑，这些反射同步地发生在
    worker 的事件循环里：一次请求读几十次表就是几秒，卡住的是**整个宿主**。

    所以判据是**次数**：一个请求里读三次、再调三次接口，同一张表**只反射一次**。
    """
    from app.plugins import public_api

    build, restart = plugin_env
    pid = 'com.test.reflect'
    name = table_prefix(pid) + 'items'
    code = f'''def register(registrar, ctx):
    def rows(book_id: int):
        # 同一张表读三次：反射不该跟着读三次
        out = []
        for _ in range(3):
            out.append(ctx.data.read("{name}", book_id=book_id))
        return {{"reads": len(out), "rows": sum(len(item) for item in out)}}
    registrar.get("/rows", rows)
'''.encode()
    raw, manifest, _, _ = build(plugin_id=pid, tables=True,
        files={'backend/main.py': code, 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['plugin_data:rw', 'book:read']))
    assert client.post('/api/system/plugins/install', files={'file': ('reflect.cwp', raw)}, headers=auth).status_code == 200
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth, json={'user_ids': [plain_user.id]}).status_code == 200
    link = UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT')
    db.add(link); db.commit()

    reflected = []
    real_table = public_api.Table

    def counting_table(table_name, *args, **kwargs):
        reflected.append(table_name)
        return real_table(table_name, *args, **kwargs)

    monkeypatch.setattr(public_api, "Table", counting_table)
    # 缓存是进程级的：先清掉，免得上一条用例已经把这张表反射过了。
    public_api.clear_plugin_table_cache()

    path = f'/api/plugins/{pid}/rows'
    for _ in range(3):
        response = client.get(path, params={'book_id': book.id}, headers=plain_auth)
        assert response.status_code == 200, response.text
        assert response.json() == {'reads': 3, 'rows': 0}

    assert reflected.count(name) == 1, reflected

    # **失效**：迁移改了表结构之后要能看到新结构。
    public_api.clear_plugin_table_cache(pid)
    reflected.clear()
    response = client.get(path, params={'book_id': book.id}, headers=plain_auth)
    assert response.status_code == 200, response.text
    assert reflected.count(name) == 1, reflected


# ---------------------------------------------------------------------------
# 「解析调用者」按请求一份，而不是按能力调用一次
# ---------------------------------------------------------------------------

def _install_counting_plugin(plugin_env, client, auth, pid, name):
    """装一个只提供「读表」这一个能力的插件，返回 (安装路径, 重启函数)。"""
    build, restart = plugin_env
    code = f'''def register(registrar, ctx):
    def rows(book_id: int):
        out = []
        for _ in range(3):
            out.append(ctx.data.read("{name}", book_id=book_id))
        return {{"reads": len(out)}}
    registrar.get("/rows", rows)
'''.encode()
    raw, _manifest, _keys, _private = build(plugin_id=pid, tables=True,
        files={'backend/main.py': code, 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=['plugin_data:rw', 'book:read']))
    assert client.post('/api/system/plugins/install',
                       files={'file': ('principal.cwp', raw)}, headers=auth).status_code == 200
    restart()
    return f'/api/plugins/{pid}/rows', restart


def test_一个请求里解析调用者只做一次(plugin_env, client, auth, plain_auth, plain_user, book, db, monkeypatch):
    """一次请求里调三次能力，**解析调用者只做一次**。

    **为什么这件事值得一条用例**：解析调用者 = 7 条 SQL（sys_user +
    plugin_registry + plugin_user_access + plugin_cohort +
    plugin_worker_runtime×3）。实测（生产同款机器）一次共享卡工作台请求
    解析了 **92 次** ⇒ 644 条纯授权样板，占那条请求全部 SQL 的 **57%**。

    宿主网关**在每个请求入口已经校验过一遍**（``loader.py`` 的 ``guard()``）——
    请求内部再逐次解析是**重复**，不是「更严」。所以判据是**次数**：
    一个请求里读三次表 ⇒ 只解析一次。

    **计数挂在 ``public_api`` 上**：网关那道闸用的是 ``loader`` 里的名字，
    不在计数里 —— 数的正是「请求内部又解析了几次」。
    """
    from app.plugins import public_api

    pid = 'com.test.principal'
    name = table_prefix(pid) + 'items'
    path, _restart = _install_counting_plugin(plugin_env, client, auth, pid, name)
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [plain_user.id]}).status_code == 200
    link = UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT')
    db.add(link); db.commit()

    resolved = []
    real_require = public_api.require_plugin_access

    def counting_require(*args, **kwargs):
        resolved.append(args[1] if len(args) > 1 else kwargs.get('plugin_id'))
        return real_require(*args, **kwargs)

    monkeypatch.setattr(public_api, "require_plugin_access", counting_require)

    response = client.get(path, params={'book_id': book.id}, headers=plain_auth)
    assert response.status_code == 200, response.text
    assert response.json() == {'reads': 3}
    # 三次能力调用 ⇒ **只解析一次**。
    assert len(resolved) == 1, resolved


def test_撤权之后下一个请求立刻被拒(plugin_env, client, auth, plain_auth, plain_user, book, db):
    """**按请求缓存**不能把撤权拖到「下一个请求」之后才生效。

    缓存是**每请求一份**：上一个请求解析出来的调用者，不会被下一个请求
    拿来用。所以撤销授权之后，**紧接着的那个请求**必须被拒。

    判据是**紧邻**（不是「过一会儿」）—— 因为要防的正是
    「请求里缓存住了，下一次请求还认它」。
    """
    from app.models.plugin import PluginUserAccess

    pid = 'com.test.revoke'
    name = table_prefix(pid) + 'items'
    path, _restart = _install_counting_plugin(plugin_env, client, auth, pid, name)
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [plain_user.id]}).status_code == 200
    link = UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT')
    db.add(link); db.commit()

    # 授权在：请求通。
    response = client.get(path, params={'book_id': book.id}, headers=plain_auth)
    assert response.status_code == 200, response.text

    # 撤权。
    grant = db.get(PluginUserAccess, (pid, plain_user.id))
    assert grant is not None
    grant.revoked_at = datetime.now(timezone.utc)
    db.commit()

    # **紧接着**的请求：必须被拒。
    response = client.get(path, params={'book_id': book.id}, headers=plain_auth)
    assert response.status_code == 404, response.text


def test_换一个用户不会拿到上一个人的调用者(plugin_env, client, auth, plain_auth, admin_user, plain_user, book, db):
    """**缓存不能跨用户串** —— 上一位用户的调用者绝不能被下一位拿到。

    管理员（已授权）请求过一次，紧接着一个**没授权**的用户来请求：
    后者必须被拒。这条钉的是「缓存键里带 user_id」这件事 ——
    只拿 request_id 当键的话，钩子路径（不设 request_id）会退化成跨请求共享。
    """
    pid = 'com.test.nocross'
    name = table_prefix(pid) + 'items'
    path, _restart = _install_counting_plugin(plugin_env, client, auth, pid, name)
    # 只给管理员授权：plain_user 没有授权。
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [admin_user.id]}).status_code == 200

    # 管理员先请求一次（走一遍「解析调用者并缓存」）。
    response = client.get(path, params={'book_id': book.id}, headers=auth)
    assert response.status_code == 200, response.text

    # 紧接着没授权的用户来 —— 必须被拒。
    response = client.get(path, params={'book_id': book.id}, headers=plain_auth)
    assert response.status_code == 404, response.text


def test_跨账套读只见有授权的账套(plugin_env, client, auth, plain_auth, plain_user, admin_user, book, db):
    """跨账套读的范围**由宿主定死**：当前用户被授权的账套，一个不多一个不少。

    这条钉的是跨账套读的**权限边界**，不是它的性能收益：

    · 插件只能「要全部」，不能「要哪几本」—— 集合是 ``UserBook`` 子查询，
      插件既不能指定也不能改写；
    · **管理员也没有隐含的全账套权限** —— 管理员只看得见他被授权的那几本；
    · 默认仍然 fail-closed：不传 ``across_books`` 却不点名 ``book_id``
      依旧 400，老插件漏传参数不会静默放宽成「全账套可读」；
    · 跨账套不能同时点名账套（那两者的语义互相矛盾）；
    · 全局表没有账套范围，跨账套读它没有意义 —— 直接 400；
    · 写仍然必须点名账套。
    """
    from app.models.book import Book

    build, restart = plugin_env
    pid = 'com.test.across'
    name = table_prefix(pid) + 'items'
    gname = table_prefix(pid) + 'settings'
    code = f'''def register(registrar, ctx):
    def rows(payload: dict):
        return ctx.data.read("{name}", book_id=payload.get("book_id"),
            across_books=payload.get("across_books", False), where=payload.get("where"))
    def count(payload: dict):
        return ctx.data.count("{name}", book_id=payload.get("book_id"),
            across_books=payload.get("across_books", False))
    def insert(book_id: int, payload: dict):
        return ctx.data.insert("{name}", payload, book_id=book_id)
    def global_rows(payload: dict):
        return ctx.data.read("{gname}", across_books=payload.get("across_books", False))
    registrar.post("/rows", rows)
    registrar.post("/count", count)
    registrar.post("/insert", insert)
    registrar.post("/global", global_rows)
'''.encode()
    settings_migration = f'''from alembic import op
import sqlalchemy as sa
revision = "0002_settings"
down_revision = "0001_items"

def upgrade():
    op.create_table("{gname}", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value", sa.String(200), nullable=False))

def downgrade():
    op.drop_table("{gname}")
'''.encode()
    raw, manifest, _, _ = build(plugin_id=pid, tables=True,
        files={'backend/main.py': code,
               'frontend/index.js': b'export default host => ({render: () => host.h("div")});',
               'migrations/versions/0002_settings.py': settings_migration},
        edit=lambda m: m.update(permissions=['plugin_data:rw', 'book:read'],
            tables=[{'name': name, 'scope': 'book'},
                    {'name': gname, 'scope': 'global', 'purpose': 'configuration'}]))
    assert client.post('/api/system/plugins/install', files={'file': ('across.cwp', raw)}, headers=auth).status_code == 200
    restart()
    # 一次把两个人都授上：这个接口是**整组替换**语义，连着发两次会把前一次
    # 授的人撤掉。
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth,
                      json={'user_ids': [plain_user.id, admin_user.id]}).status_code == 200

    # 三本账：A、B 授权给普通用户；C 只授权给管理员；B 不授权给管理员。
    other_b = Book(company_name='另一家公司', name='另一本账', start_year=2026, start_month=1)
    hidden = Book(company_name='第三家公司', name='第三本账', start_year=2026, start_month=1)
    db.add_all([other_b, hidden]); db.flush()
    db.add_all([
        UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT'),
        UserBook(user_id=plain_user.id, book_id=other_b.id, role='ACCOUNTANT'),
        UserBook(user_id=admin_user.id, book_id=hidden.id, role='OWNER'),
    ])
    db.commit()

    path = f'/api/plugins/{pid}'
    # 三本账各插一行：A、B 由普通用户插（他是这两本的会计），C 由管理员插。
    assert client.post(path + '/insert', params={'book_id': book.id},
                        json={'label': 'a'}, headers=plain_auth).status_code == 200
    assert client.post(path + '/insert', params={'book_id': other_b.id},
                        json={'label': 'b'}, headers=plain_auth).status_code == 200
    assert client.post(path + '/insert', params={'book_id': hidden.id},
                        json={'label': 'c'}, headers=auth).status_code == 200

    # 普通用户跨账套读：只有 A 与 B，**看不到 C**（他没被授权）。
    r = client.post(path + '/rows', json={'across_books': True}, headers=plain_auth)
    assert r.status_code == 200, r.text
    assert sorted(row['label'] for row in r.json()) == ['a', 'b']
    assert {row['book_id'] for row in r.json()} == {book.id, other_b.id}
    # count 与 read 的范围一致。
    r = client.post(path + '/count', json={'across_books': True}, headers=plain_auth)
    assert r.json() == 2

    # **管理员也没有隐含的全账套权限**：他只被授权了 A（夹具给的 OWNER）与 C，
    # B 不在其中，所以 B 的那一行他也读不到。
    r = client.post(path + '/rows', json={'across_books': True}, headers=auth)
    assert r.status_code == 200, r.text
    assert sorted(row['label'] for row in r.json()) == ['a', 'c']

    # 跨账套不能同时点名账套（两者语义互相矛盾）。
    r = client.post(path + '/rows', json={'across_books': True, 'book_id': book.id}, headers=plain_auth)
    assert r.status_code == 400, r.text
    # 默认仍然 fail-closed：不点名账套就 400，不会静默变成「全账套可读」。
    # （body 不能是空对象 —— 空 body 先被宿主按「载体必填」挡成 422。）
    r = client.post(path + '/rows', json={'across_books': False}, headers=plain_auth)
    assert r.status_code == 400, r.text
    r = client.post(path + '/count', json={'across_books': False}, headers=plain_auth)
    assert r.status_code == 400, r.text
    # 全局表没有账套范围，跨账套读它没有意义。
    r = client.post(path + '/global', json={'across_books': True}, headers=plain_auth)
    assert r.status_code == 400, r.text
    # 写仍然必须点名账套：不给 book_id 直接 422（缺参数），连不到写能力。
    r = client.post(path + '/insert', json={'label': 'x'}, headers=plain_auth)
    assert r.status_code == 422, r.text
    # 过滤条件只能在授权范围**之内**收窄，不能放宽到没授权的那本账。
    r = client.post(path + '/rows', json={'across_books': True, 'where': {'book_id': hidden.id}},
                    headers=plain_auth)
    assert r.status_code == 200, r.text
    assert r.json() == []
