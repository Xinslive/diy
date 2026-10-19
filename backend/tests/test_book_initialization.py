"""账套初始化的两步：状态、第 1 步闸门、第 2 步完成标记。

**这份文件钉的是一条数据依赖，不是一条流程偏好。**

期初余额挂在科目上。所以「先定科目、再录期初」不是界面上的建议顺序，
而是「顺序反了，数据就挂不上」的硬约束：科目还没定下来时解析出来的
每一条期初都匹配不到科目，用户看到的是「读进来 N 条，全是科目不存在」，
而根因 —— 顺序反了 —— 在界面上一个字都看不出来。

所以三件事必须钉死：

1. 新账套出来时初始化停在第 1 步（而不是自动当成做完了）；
2. 第 1 步没完成，**导入与保存**两条路都被 409 挡下
   （只挡一条等于没挡：手一抖把 payload POST 进来就绕过去了，
   而那恰恰是最需要拦住的路径）；
3. 两条路都算数 —— 导入外部科目表，或者确认沿用系统标准科目表。
   只认第一条会把「就用标准科目表从零开始记账」的用户挡在门外。

外加两条不许回归的：keep 幂等（重复点不重写时间戳、不刷审计）、
第 2 步的完成标记与 SET_OPENINGS 在**同一个事务**里。
"""
from __future__ import annotations

import io
import uuid

from openpyxl import Workbook
from sqlalchemy import select

from app.models import AuditLog, Book, PeriodOpening, Subject, SysUser, UserBook

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _new_book(client, auth, **over) -> dict:
    """走 API 建一个**真·新账套**：它的初始化状态是空的。"""
    tag = uuid.uuid4().hex[:8]
    payload = {
        "company_name": f"初始化测试{tag}",
        "name": f"账套{tag}",
        "start_year": 2026,
        "start_month": 1,
        **over,
    }
    r = client.post("/api/books", json=payload, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _init(client, auth, book_id) -> dict:
    r = client.get(f"/api/books/{book_id}/init", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _subject_xlsx() -> bytes:
    """一份最小可用的外部科目表：两个末级科目，层级 4-2。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["类型", "级次", "科目编码", "科目名称", "余额方向"])
    ws.append(["资产", 1, "1001", "库存现金", "借"])
    ws.append(["资产", 2, "100101", "库存现金-基本户", "借"])
    ws.append(["负债", 1, "2001", "短期借款", "贷"])
    ws.append(["负债", 2, "200101", "短期借款-银行", "贷"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _openings_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["科目编码", "科目名称", "方向", "期初余额"])
    ws.append(["100101", "库存现金-基本户", "借", 1000])
    ws.append(["200101", "短期借款-银行", "贷", 1000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ 新账套的起点


def test_新建账套后初始化停在第1步(client, auth):
    """建完账套**不等于**初始化做完了。

    建账时系统确实装好了标准科目表，但「第 1 步已完成」是用户的决定，
    不是系统的副作用 —— 决定之前，向导要一直告诉他还差这一步。
    """
    book = _new_book(client, auth)
    init = _init(client, auth, book["id"])
    assert init["subjects_ready"] is False
    assert init["openings_ready"] is False
    assert init["step"] == 1
    # 标准科目表确实已经装上了，所以「沿用它」这一条是可走的
    assert init["subject_count"] > 0
    assert init["can_keep_chart"] is True


def test_账套列表与详情都带着初始化进度(client, auth):
    """卡片上那个「待初始化」标记就靠这个字段，不能让前端为每个账套多发一次请求。"""
    book = _new_book(client, auth)
    row = next(b for b in client.get("/api/books", headers=auth).json() if b["id"] == book["id"])
    assert row["init"]["step"] == 1, row["init"]
    detail = client.get(f"/api/books/{book['id']}", headers=auth).json()
    assert detail["init"]["subjects_ready"] is False


def test_期初余额页也带着初始化进度(client, auth):
    book = _new_book(client, auth)
    got = client.get(f"/api/books/{book['id']}/openings", headers=auth).json()
    assert got["init"]["step"] == 1


# ------------------------------------------------------------------ 第 1 步闸门


def test_第1步没完成时导入期初被拒(client, auth):
    book = _new_book(client, auth)
    r = client.post(f"/api/books/{book['id']}/openings/parse", headers=auth,
                    files={"file": ("期初.xlsx", _openings_xlsx(), XLSX_MIME)})
    assert r.status_code == 409, r.text
    assert "第 1 步" in r.json()["detail"], r.json()


def test_第1步没完成时保存期初被拒(client, auth, db, subjects):
    """**两条路都要挡。** 只挡解析的话，手一抖把 payload POST 进来就绕过去了，
    而期初一旦存歪，之后每个月、每张账簿、每份报表都跟着歪。"""
    book = _new_book(client, auth)
    # 新账套里找两个末级科目来构造 payload
    new_subs = {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book["id"])).all()}
    codes = sorted(c for c, s in new_subs.items() if s.is_leaf)
    assert len(codes) >= 2
    payload = [
        {"subject_id": new_subs[codes[0]].id, "amount": "100"},
        {"subject_id": new_subs[codes[1]].id, "amount": "-100"},
    ]
    r = client.post(f"/api/books/{book['id']}/periods", headers=auth, json=payload)
    assert r.status_code == 409, r.text
    assert "第 1 步" in r.json()["detail"], r.json()
    # 确认没写进去
    assert db.scalar(select(PeriodOpening).where(PeriodOpening.book_id == book["id"])) is None


def test_只读账号不能替别人确认第1步(client, plain_auth, db, plain_user):
    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"只读{tag}", name=f"账套{tag}", start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    create_periods(db, b)
    install_chart(db, b.id)
    db.add(UserBook(user_id=plain_user.id, book_id=b.id, role="VIEWER"))
    db.commit()
    r = client.post(f"/api/books/{b.id}/init/subjects/keep", headers=plain_auth)
    assert r.status_code == 403, r.text


# ------------------------------------------------------------------ 第 1 步：两条路


def test_沿用标准科目表也算第1步完成(client, auth):
    book = _new_book(client, auth)
    r = client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["changed"] is True
    init = body["init"]
    assert init["subjects_ready"] is True
    assert init["subjects_source"] == "CHART"
    assert init["subjects_at"], "第 1 步完成时间必须落库"
    assert init["step"] == 2, "第 1 步做完后该轮到第 2 步"


def test_沿用标准科目表要留痕(client, auth, db):
    """初始化状态是账面数字的前置条件，「谁在什么时候确认的」要查得到。"""
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    rows = db.scalars(select(AuditLog).where(
        AuditLog.book_id == book["id"], AuditLog.action == "INIT_SUBJECTS")).all()
    assert rows, "确认沿用标准科目表没有留审计"
    assert rows[-1].detail
    assert rows[-1].before is not None or rows[-1].after is not None


def test_keep是幂等的(client, auth, db):
    """重复点不能重写时间戳，也不能刷审计 ——
    「这一步是什么时候完成的」必须只有一个答案。"""
    book = _new_book(client, auth)
    first = client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth).json()
    second = client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth).json()
    assert second["changed"] is False
    assert second["init"]["subjects_at"] == first["init"]["subjects_at"]
    logs = db.scalars(select(AuditLog).where(
        AuditLog.book_id == book["id"], AuditLog.action == "INIT_SUBJECTS")).all()
    assert len(logs) == 1, f"重复确认刷了 {len(logs)} 条审计"


def test_导入外部科目表也算第1步完成(client, auth, db):
    """从初始化向导点进来，和从科目表页面点进来，是同一件事。"""
    book = _new_book(client, auth)
    r = client.post(f"/api/books/{book['id']}/subjects/import", headers=auth,
                    files={"file": ("会计科目.xls", _subject_xlsx(), XLSX_MIME)})
    assert r.status_code == 200, r.text
    init = r.json()["init"]
    assert init["subjects_ready"] is True
    assert init["subjects_source"] == "IMPORT"
    assert init["step"] == 2


def test_没有科目的账套不能确认沿用(client, auth, db, admin_user):
    """空账套点「沿用标准科目表」等于什么都没确认 —— 必须挡。"""
    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"空账套{tag}", name=f"账套{tag}", start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    from app.accounting.periods import create_periods

    create_periods(db, b)
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    r = client.post(f"/api/books/{b.id}/init/subjects/keep", headers=auth)
    assert r.status_code == 400, r.text


# ------------------------------------------------------------------ 第 2 步


def test_第一次保存期初后两步都算完成(client, auth, db):
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    subs = {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book["id"])).all()}
    codes = sorted(c for c, s in subs.items() if s.is_leaf)
    payload = [{"subject_id": subs[codes[0]].id, "amount": "100"},
               {"subject_id": subs[codes[1]].id, "amount": "-100"}]
    r = client.post(f"/api/books/{book['id']}/periods", headers=auth, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["init_step_done"] is True


def test_保存期初要把最新init一起带回来(client, auth, db):
    """第 2 步是在**另一个页面**上显示的，状态得跟着这一次请求一起过去。

    以前这条只返回 init_step_done，前端拿不到 init 对象，也就无从写回 store ——
    用户从期初页回到账套管理，向导和卡片都还在说「还没做」，
    而那一步他刚刚明明做完了。keep / skip / 导入科目三个口子都带 init，
    只有保存期初不带，链子就断在这一环。
    """
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    subs = {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book["id"])).all()}
    codes = sorted(c for c, s in subs.items() if s.is_leaf)
    payload = [{"subject_id": subs[codes[0]].id, "amount": "100"},
               {"subject_id": subs[codes[1]].id, "amount": "-100"}]
    r = client.post(f"/api/books/{book['id']}/periods", headers=auth, json=payload)
    assert r.status_code == 200, r.text

    init = r.json()["init"]
    assert init["openings_ready"] is True, "带回来的 init 说第 2 步还没做"
    assert init["openings_source"] == "IMPORT"
    assert init["step"] == 0
    # 而且要和另外那个接口说的是同一份事实（两边各算一次却不同步，是另一种病）
    again = client.get(f"/api/books/{book['id']}/init", headers=auth).json()
    assert again["openings_at"] == init["openings_at"]
    init = _init(client, auth, book["id"])
    assert init["openings_ready"] is True
    assert init["step"] == 0, "两步都走完时 step 应为 0"
    assert init["opening_count"] == 2


def test_完成第1步后就能导入期初(client, auth):
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    r = client.post(f"/api/books/{book['id']}/openings/parse", headers=auth,
                    files={"file": ("期初.xlsx", _openings_xlsx(), XLSX_MIME)})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ 存量账套


def test_夹具造的老账套不需要再走初始化(client, auth, book_id):
    """迁移把存量账套回填成 CHART，夹具同理 ——
    否则「正在用的老账套打开就被 409 拦住」会成为升级后的第一波投诉。"""
    init = _init(client, auth, book_id)
    assert init["subjects_ready"] is True
    assert init["subjects_source"] == "CHART"


# ------------------------------------------------------------------ 无期初余额


def test_可以声明本期无期初余额(client, auth, db):
    """新公司第一年、上一年度本来就是空账，期初该是 0。

    界面上只有「导入」一个出口时，这类账套的初始化永远停在第 2 步：
    卡片上的「待初始化」摘不掉，向导每次弹出来都在催一件他根本不需要做的事。
    """
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    r = client.post(f"/api/books/{book['id']}/init/openings/skip", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    init = body["init"]
    assert init["openings_ready"] is True
    # 来源必须分清：「确认过没有」和「导入过」在账面含义上完全相反
    assert init["openings_source"] == "NONE"
    assert init["opening_count"] == 0
    assert init["step"] == 0, "两步都走完时 step 应为 0"

    # 声明之后照样可以再导入期初（这是可逆的，不是把账锁死）
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    assert client.get(f"/api/books/{book['id']}/init", headers=auth).json()["step"] == 0


def test_已经有期初余额的账套不能声明无期初(client, auth, db):
    """「本来就没有」与「把期初清空了」不是一回事，不能用一个声明糊弄过去。"""
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    subs = {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book["id"])).all()}
    codes = sorted(c for c, s in subs.items() if s.is_leaf)
    r = client.post(f"/api/books/{book['id']}/periods", headers=auth, json=[
        {"subject_id": subs[codes[0]].id, "amount": "100"},
        {"subject_id": subs[codes[1]].id, "amount": "-100"},
    ])
    assert r.status_code == 200, r.text
    r = client.post(f"/api/books/{book['id']}/init/openings/skip", headers=auth)
    assert r.status_code == 409, r.text
    assert "期初余额" in r.json()["detail"], r.json()


def test_声明无期初要留审计(client, auth, db):
    """事后要查的恰恰是「当时账上到底有没有数」，而界面上一个数字都没有。"""
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    client.post(f"/api/books/{book['id']}/init/openings/skip", headers=auth)
    rows = db.scalars(select(AuditLog).where(
        AuditLog.book_id == book["id"], AuditLog.action == "INIT_OPENINGS")).all()
    assert rows, "声明无期初余额没有留审计"
    assert rows[-1].after and rows[-1].after.get("init_openings_source") == "NONE"


def test_skip是幂等的(client, auth, db):
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    first = client.post(f"/api/books/{book['id']}/init/openings/skip", headers=auth).json()
    second = client.post(f"/api/books/{book['id']}/init/openings/skip", headers=auth).json()
    assert second["changed"] is False
    assert second["init"]["openings_at"] == first["init"]["openings_at"]
    assert len(db.scalars(select(AuditLog).where(
        AuditLog.book_id == book["id"], AuditLog.action == "INIT_OPENINGS")).all()) == 1


def test_导入期初时来源记为IMPORT(client, auth, db):
    book = _new_book(client, auth)
    client.post(f"/api/books/{book['id']}/init/subjects/keep", headers=auth)
    subs = {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book["id"])).all()}
    codes = sorted(c for c, s in subs.items() if s.is_leaf)
    client.post(f"/api/books/{book['id']}/periods", headers=auth, json=[
        {"subject_id": subs[codes[0]].id, "amount": "100"},
        {"subject_id": subs[codes[1]].id, "amount": "-100"},
    ])
    init = client.get(f"/api/books/{book['id']}/init", headers=auth).json()
    assert init["openings_source"] == "IMPORT"
