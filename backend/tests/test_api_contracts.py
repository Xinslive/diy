"""API 层与数据契约：长度上限、跨账套参数、角色归一化、撞号的人话。

这一组对应审查报告「API 层与数据契约」域的 P1-3 / P2-1 / P2-2 / P2-5 / P2-6 / P3-1。

**为什么这些要在接口层挡。** 数据库列宽是**已知且已声明**的
（``subject.code varchar(32)``、``voucher.attachments smallint`` …），
但 Pydantic 不拦、PostgreSQL 拦 —— 拦下的方式是 ``DataError``，
全局处理器把它翻成 **500 + 一个请求号**，用户看到的是「数据库内部错误」，
而且每次触发都在服务端留下一条完整异常栈日志。
所以同一套列宽必须**先在 schema 里写一遍**，超限直接 422。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.models import Book, Entry, Period, Subject, SysUser, UserBook, Voucher


@pytest.fixture
def other_book(db, admin_user):
    """另一个账套 —— 跨账套那一侧。"""
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    b = Book(company_name="别家公司", name=f"别的账套{uuid.uuid4().hex[:6]}",
             start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def book_2025(db, admin_user):
    """一本 2025 年起的账 —— 与服务器日历年不同，用来验「本年」的口径。"""
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    b = Book(company_name="旧账套公司", name=f"2025账套{uuid.uuid4().hex[:6]}",
             start_year=2025, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


def _subjects(db, book_id) -> dict:
    return {s.code: s for s in db.query(Subject).filter(Subject.book_id == book_id).all()}


def _entries(subjects, dr, cr):
    out = [{"subject_id": subjects[c].id, "direction": "DEBIT", "amount": str(a), "summary": s}
           for c, a, s in dr]
    out += [{"subject_id": subjects[c].id, "direction": "CREDIT", "amount": str(a), "summary": s}
            for c, a, s in cr]
    return out


# --------------------------------------------------------------------- P2-1 跨账套期间


def test_凭证列表拒绝别的账套的起始期间(client, auth, db, book, other_book):
    """跨账套的 period_id 不能放行（审查 P2-1）。

    ``ord`` 是**账套内相对序号**，两个账套的同一个 ord 含义完全不同 ——
    放行的后果是「结果是错的而不是空的」：外账套的 ord=1 会让本账套返回
    全部凭证，ord=2400 会让本账套返回空集。
    """
    p = db.query(Period).filter(Period.book_id == other_book.id).order_by(Period.ord).first()
    assert p is not None

    r = client.get(f"/api/books/{book.id}/vouchers?from_period_id={p.id}", headers=auth)
    assert r.status_code == 404, (
        f"跨账套的期间被放行了：HTTP {r.status_code} {r.text} —— "
        f"外账套的 ord 被当成本账套的 ord 用了")


def test_凭证列表拒绝别的账套的结束期间(client, auth, db, book, other_book):
    p = db.query(Period).filter(Period.book_id == other_book.id).order_by(Period.ord).first()
    r = client.get(f"/api/books/{book.id}/vouchers?to_period_id={p.id}", headers=auth)
    assert r.status_code == 404, f"跨账套的结束期间被放行了：HTTP {r.status_code} {r.text}"


# --------------------------------------------------------------------- P1-3 长度上限


def test_超长科目编码返回422而不是500(client, auth, book):
    """``subject.code varchar(32)`` —— 超长值以前一路走到 INSERT 才被截断，
    PostgreSQL 抛 DataError，全局处理器翻成 **500**。"""
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1" * 40, "name": "超长编码", "category": "ASSET", "direction": "DEBIT",
    })
    assert r.status_code == 422, f"超长科目编码返回了 {r.status_code}：{r.text}"
    assert "32" in r.text, r.text


def test_超长科目名称返回422(client, auth, book):
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1905", "name": "名" * 200, "category": "ASSET", "direction": "DEBIT",
    })
    assert r.status_code == 422, f"超长科目名称返回了 {r.status_code}：{r.text}"


def test_超长账套编码规则返回422(client, auth):
    """``book.subject_code_rule varchar(32)``。"""
    r = client.post("/api/books", headers=auth, json={
        "company_name": "测试公司", "name": "规则超长的账套",
        "start_year": 2026, "start_month": 1,
        "subject_code_rule": "-".join(["1234"] * 12),
    })
    assert r.status_code == 422, f"超长编码规则返回了 {r.status_code}：{r.text}"


def test_附单据数超出smallint返回422(client, auth, book, subjects):
    """``voucher.attachments smallint``（最大 32767）。"""
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-15", "attachments": 70000,
        "entries": _entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]),
    })
    assert r.status_code == 422, f"附单据数 70000 返回了 {r.status_code}：{r.text}"


def test_超长凭证号返回422(client, auth, book, subjects):
    """``voucher.voucher_no varchar(32)``。"""
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-15", "voucher_no": "记" + "1" * 40,
        "entries": _entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]),
    })
    assert r.status_code == 422, f"超长凭证号返回了 {r.status_code}：{r.text}"


# --------------------------------------------------------------------- P2-2 批量入参


def test_批量ids超限返回422(client, auth, book):
    """65535 个 id 会把绑定参数撑爆 → psycopg 抛 OperationalError → 500。"""
    r = client.post(f"/api/books/{book.id}/vouchers/batch-delete", headers=auth,
                    json={"ids": list(range(1, 70001))})
    assert r.status_code == 422, f"超长批量入参返回了 {r.status_code}：{r.text}"


def test_单张凭证分录数超限返回422(client, auth, book, subjects):
    """「1 份请求体 → N 条数据库行」的放大器要关上。"""
    entries = _entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]) * 501
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-15", "entries": entries,
    })
    assert r.status_code == 422, f"501 条分录返回了 {r.status_code}：{r.text}"


def test_写期初的行数超限返回422(client, auth, book, subjects):
    rows = [{"subject_id": subjects["1001"].id, "amount": "1.00"}] * 2001
    r = client.post(f"/api/books/{book.id}/periods", headers=auth, json=rows)
    assert r.status_code == 422, f"2001 行期初返回了 {r.status_code}：{r.text}"


# --------------------------------------------------------------------- P2-6 登录入参


def test_超长登录用户名返回422(client):
    r = client.post("/api/auth/login", json={"username": "u" * 300, "password": "x"})
    assert r.status_code == 422, f"超长用户名返回了 {r.status_code}：{r.text}"


def test_超长登录口令返回422(client):
    r = client.post("/api/auth/login", json={"username": "someone", "password": "p" * 2000})
    assert r.status_code == 422, f"超长口令返回了 {r.status_code}：{r.text}"


# --------------------------------------------------------------------- P3-1 角色归一化


@pytest.mark.parametrize("role", ["owner", "Owner", " OWNER ", "OWNER", "Owner "])
def test_角色归一化之后判定一致(role):
    """``normalize_role`` 的白名单语义：大小写与首尾空白都吃掉（审查 P3-1）。

    **为什么只能在这儿钉。** 库上有 ``ck_sys_user_book_role`` CHECK 约束
    （``role IN ('OWNER','ACCOUNTANT','VIEWER')``），小写的角色**根本存不进去**，
    接口层也只认那三个大写字面量 —— 所以这条是「配置事故」级的防御，
    运行时不可复现（审查报告自己也这么记）。它要保证的是：
    **万一**库里出现了 ``'owner'``，判定口径仍然与 ``'OWNER'`` 一致。
    """
    from app.core.deps import normalize_role

    assert normalize_role(role) == "OWNER", f"{role!r} 归一化之后不是 OWNER"


def test_账套所有者的判断走归一化(client, auth, db, book, monkeypatch):
    """``api/books.py`` 的 OWNER 判断不许再裸比较（审查 P3-1）。

    裸比较 ``link.role != "OWNER"`` 与 ``normalize_role(link.role) != "OWNER"``
    在能存进去的角色值上完全一样 —— 差别只在「万一存进了一个带空白或小写的
    角色」时才显形，而那正是这道防线的意义。所以这里用探针钉住
    **判断确实走了归一化**这件事本身。
    """
    import app.api.books as api_books

    seen: list = []
    original = api_books.normalize_role

    def spy(role):
        seen.append(role)
        return original(role)

    monkeypatch.setattr(api_books, "normalize_role", spy)
    r = client.put(f"/api/books/{book.id}", headers=auth, json={"name": "改名一次"})
    assert r.status_code == 200, r.text
    assert seen, "update_book 的 OWNER 判断没有走 normalize_role"
    # 而且必须是**拿归一化后的值**去比的，不是拿原值比完再归一化
    assert original("OWNER") == "OWNER"


# --------------------------------------------------------------------- P2-3 撞号的人话


def test_改凭证日期跨期撞号返回422且是人话(client, auth, db, book, subjects,
                                          make_voucher):
    """改到另一个已经有同号凭证的期间 → 422 + 人话，**不漏数据库英文原文**。

    以前这里没有任何占用检查：改期 → flush → 撞 ``uq_voucher_period_no`` →
    裸的 UniqueViolation，全局处理器把数据库原文回给用户。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-02-05", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    assert v1.voucher_no == v2.voucher_no == "记0001", (v1.voucher_no, v2.voucher_no)

    r = client.put(f"/api/books/{book.id}/vouchers/{v1.id}", headers=auth, json={
        "voucher_date": "2026-02-10",
        "entries": _entries(subjects, [("1001", 10, "x")], [("5001", 10, "x")]),
    })
    assert r.status_code == 422, f"撞号返回了 {r.status_code}：{r.text}"
    detail = r.json()["detail"]
    assert "记0001" in detail, detail
    assert "2026-02" in detail, detail
    # 数据库的英文原文与库结构名不许漏给客户端
    assert "uq_voucher_period_no" not in r.text, r.text
    assert "duplicate key" not in r.text.lower(), r.text
    # 撞号之后凭证必须还在原期间
    db.expire_all()
    assert db.get(Voucher, v1.id).period_id == v1.period_id, "撞号之后凭证被挪走了"


# --------------------------------------------------------------------- P2-5 会计年度


def _book_2025_voucher(db, book, subjects, amount="100"):
    from app.accounting.vouchers import create_voucher

    user = db.query(SysUser).join(UserBook, UserBook.user_id == SysUser.id).filter(
        UserBook.book_id == book.id).first()
    v = create_voucher(
        db, book_id=book.id, user=user,
        voucher_date=date(2025, 3, 10),
        entries=[{"subject_id": subjects["1001"].id, "direction": "DEBIT",
                  "amount": amount, "summary": "2025 年的业务"},
                 {"subject_id": subjects["5001"].id, "direction": "CREDIT",
                  "amount": amount, "summary": "2025 年的业务"}],
    )
    db.commit()
    return v


def test_本年发生额按账套的会计年度判定(client, auth, db, book_2025):
    """「本年」= **账套当前在记的会计年度**，不是服务器日历年（审查 P2-5）。

    以前写的是 ``EXTRACT(YEAR FROM CURRENT_DATE)``：服务器当前日期加上连接
    会话时区（本项目钉死成 UTC）。给一本 2025 年起的账记 2025 年的账时，
    它按 2026 判 —— 判的是错的那个年。
    """
    if date.today().year == 2025:
        pytest.skip("服务器当前就是 2025 年，两种口径分不出来")
    subjects = _subjects(db, book_2025.id)
    _book_2025_voucher(db, book_2025, subjects)

    r = client.patch(f"/api/books/{book_2025.id}/subjects/{subjects['1001'].id}",
                     headers=auth, json={"code": "1001X"})
    assert r.status_code == 400, (
        f"2025 年的发生额没被认出来：HTTP {r.status_code} {r.text} —— "
        f"「本年」被算成了服务器日历年 {date.today().year}")


def test_科目列表的本年发生额按账套的会计年度判定(client, auth, db, book_2025):
    """同一口径，列表页那一处也要跟着走。"""
    if date.today().year == 2025:
        pytest.skip("服务器当前就是 2025 年，两种口径分不出来")
    subjects = _subjects(db, book_2025.id)
    _book_2025_voucher(db, book_2025, subjects)

    tree = client.get(f"/api/books/{book_2025.id}/subjects", headers=auth).json()

    def walk(nodes):
        for n in nodes:
            if n["code"] == "1001":
                return n
            got = walk(n.get("children") or [])
            if got:
                return got
        return None

    node = walk(tree)
    assert node is not None, "科目树里没有 1001"
    assert node["has_current_year_activity"] is True, (
        "2025 年的发生额没被算成「本年发生额」—— 判据用的是服务器日历年")
