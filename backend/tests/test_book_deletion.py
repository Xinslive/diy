"""账套删除机制（两条隐藏路径）。

路径一：管理员收回**最后一份授权** → 账套一个授权用户都没有 → 删掉。
路径二：账套只剩**一个**授权用户时，他把它改名成「删除」→ 删掉。

两条路径都**不在界面上留入口**（没有按钮、没有文案），所以这里守的是
**行为**而不是外观：接口认这两条路，别的地方不许长出第三条。

删除要过的关卡（外键 + 触发器）写在 ``app/accounting/book_deletion.py`` 里。
本文件最要紧的一条是
``test_有已记账凭证和已结账期间的账套也能删干净`` ——
「结账锁」「已记账」「借贷平衡」这三个触发器拦的就是这种账，
而它恰恰是**最该被删掉**的那本（错账、重记的账、测试残留）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.models import (AuditLog, Book, Entry, InvoiceConfig, InvoiceRule, Period,
                        PeriodOpening, ReportResult, Subject, SysUser, UserBook, Voucher)


def _count(db, model, **where) -> int:
    """按条件数行。**不用 db.get** —— 会话里留着删之前的对象，读到的是旧的。"""
    stmt = select(func.count()).select_from(model)
    for col, val in where.items():
        stmt = stmt.where(getattr(model, col) == val)
    return int(db.scalar(stmt) or 0)


def _grant(db, user_id: int, book_id: int, role: str = "OWNER") -> None:
    db.add(UserBook(user_id=user_id, book_id=book_id, role=role))
    db.commit()


def _second_user(db, username: str = "第二个会计") -> SysUser:
    u = SysUser(username=username, password_hash="x", real_name=username)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ====================================================================== 路径二


def test_唯一授权用户改名成删除_账套被删(client, auth, db, book):
    """路径二的主线：账套只有他一个人有权限，他改名成「删除」，账套就没了。"""
    before = _count(db, Book)
    r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert _count(db, Book) == before - 1
    assert _count(db, Book, id=book.id) == 0
    # 授权行随账套一起走（sys_user_book.book_id 是 CASCADE）
    assert _count(db, UserBook, book_id=book.id) == 0


def test_唯一授权人是会计_照样能删(client, auth, db, book):
    """唯一授权人不是 OWNER 也照样能删。

    账套常常是管理员建了、只留给了某个会计（OWNER 离职、账转交）。
    那时这个人是这本账**唯一能管的人** —— 按 OWNER 拦住他，
    账套就成了没人能删的孤儿。
    """
    link = db.scalar(select(UserBook).where(UserBook.book_id == book.id))
    link.role = "ACCOUNTANT"
    db.commit()
    r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert _count(db, Book, id=book.id) == 0


def test_删掉之后再访问这个账套是403(client, auth, db, book):
    """账套没了，它的一切入口都要跟着关上。

    顺序上先撞的是**授权**（``get_my_book`` 先查「你有没有这个账套的权限」），
    所以是 403 而不是 404 —— 账套已经不存在了，但这里先回的是「你没权限」。
    """
    client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    r = client.get(f"/api/books/{book.id}", headers=auth)
    assert r.status_code == 403, r.text
    r = client.get(f"/api/books/{book.id}/periods", headers=auth)
    assert r.status_code == 403, r.text


def test_改名成删除之后这个人就没有账套了(client, auth, db, book):
    """删完之后原持有人变成零账套 —— 界面上他回到「没有账套」的状态。"""
    client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    r = client.get("/api/books", headers=auth)
    assert r.status_code == 200
    assert [b["id"] for b in r.json()] == []


def test_账套还有别的授权用户_改名被拒_账套还在(client, auth, db, book):
    """两个人有权时，谁也不能靠改名把账删掉 —— 得先收回另一个人的权限。"""
    other = _second_user(db)
    _grant(db, other.id, book.id, role="ACCOUNTANT")
    r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    assert r.status_code == 409, r.text
    assert _count(db, Book, id=book.id) == 1
    # 名字也没被改掉
    assert db.scalar(select(Book.name).where(Book.id == book.id)) != "删除"


def test_只读账号改名成删除_被拒(client, auth, db, book):
    """只读账号连改名都做不到 —— 它没有「唯一授权人」这个身份。"""
    link = db.scalar(select(UserBook).where(UserBook.book_id == book.id))
    link.role = "VIEWER"
    db.commit()
    r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    assert r.status_code == 403, r.text
    assert _count(db, Book, id=book.id) == 1


def test_建账时不能把账套名就叫删除(client, auth, db):
    """「删除」是保留名：它得是**信号**，不能同时是一本真账的名字。"""
    r = client.post("/api/books", json={
        "company_name": "保留名公司", "name": "删除",
        "start_year": 2026, "start_month": 1,
    }, headers=auth)
    assert r.status_code == 400, r.text
    assert _count(db, Book, name="删除") == 0


def test_普通改名照旧能用(client, auth, db, book):
    """保留名不能误伤正常改名。"""
    r = client.put(f"/api/books/{book.id}", json={"name": "新的账套名"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json().get("deleted") is None
    assert db.scalar(select(Book.name).where(Book.id == book.id)) == "新的账套名"


# ====================================================================== 路径一


def test_管理员收回最后一份授权_账套被删(client, auth, db, book):
    """路径一：管理员在「用户与授权」里把唯一那份授权收回来，账套跟着没。"""
    user = _second_user(db, "被收回的人")
    db.delete(db.scalar(select(UserBook).where(UserBook.book_id == book.id)))
    _grant(db, user.id, book.id)
    before = _count(db, Book)
    r = client.put(f"/api/system/users/{user.id}/books", json=[], headers=auth)
    assert r.status_code == 200, r.text
    assert _count(db, Book) == before - 1
    assert _count(db, Book, id=book.id) == 0


def test_收回一份但还有人有权_账套留着(client, auth, db, book):
    """还有别人有权 → 账套还有人用，不能删。"""
    other = _second_user(db, "还有权限的人")
    _grant(db, other.id, book.id, role="VIEWER")
    r = client.put(f"/api/system/users/{other.id}/books", json=[], headers=auth)
    assert r.status_code == 200, r.text
    assert _count(db, Book, id=book.id) == 1


def test_删除用户_他名下唯一授权的账套跟着删(client, auth, db, book):
    """删账号等于收回他全部授权 —— 账套因此没人能进，随之删掉。"""
    user = _second_user(db, "被删除的账号")
    db.delete(db.scalar(select(UserBook).where(UserBook.book_id == book.id)))
    _grant(db, user.id, book.id)
    before = _count(db, Book)
    r = client.delete(f"/api/system/users/{user.id}", headers=auth)
    assert r.status_code == 200, r.text
    assert _count(db, Book) == before - 1
    assert _count(db, Book, id=book.id) == 0


def test_收回授权不影响别人还有权的账套(client, auth, db, book):
    """只删真正没人了的那些 —— 有人的账套一根汗毛都不能动。"""
    keeper = _second_user(db, "留着的人")
    doomed = _second_user(db, "要走的人")
    other_book = _make_book(db, "别人还在用的账")
    # 管理员先把 `book` 的授权交给 doomed —— 否则管理员自己就有权，账套删不掉
    db.delete(db.scalar(select(UserBook).where(UserBook.book_id == book.id)))
    _grant(db, keeper.id, other_book.id, role="VIEWER")
    _grant(db, doomed.id, book.id)
    r = client.put(f"/api/system/users/{doomed.id}/books", json=[], headers=auth)
    assert r.status_code == 200, r.text
    assert _count(db, Book, id=book.id) == 0
    assert _count(db, Book, id=other_book.id) == 1


def _make_book(db, name: str) -> Book:
    """造一本只有管理员有权的账。"""
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    b = Book(company_name="测试公司", name=name, start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    db.commit()
    db.refresh(b)
    return b


# ====================================================================== 插件表


#: 插件样式的表名 —— 前缀照抄真插件的约定（``plugin_<hash>_<表名>``）。
_PLUGIN_TABLE = "plugin_test1a2b3c_no_cascade"


def _make_plugin_table(db, *, with_child: bool = False) -> None:
    """造一张**和真插件一模一样**的表：外键指向 book.id，**不带** ON DELETE。

    这就是 2026-10-31 生产事故的复刻：插件建表时写
    ``sa.ForeignKey("book.id")`` 而忘了写 ``ondelete="CASCADE"``，
    库里落成 PG 默认的 NO ACTION —— 于是 ``DELETE FROM book`` 被它挡住。

    ``with_child=True`` 时再建一张子表，用 NO ACTION 引用母表，
    用来验「先删子表、再删母表」的顺序。
    """
    db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {_PLUGIN_TABLE} (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES book(id),
            note TEXT NOT NULL DEFAULT ''
        )
    """))
    if with_child:
        db.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {_PLUGIN_TABLE}_child (
                id SERIAL PRIMARY KEY,
                book_id INTEGER NOT NULL REFERENCES book(id),
                parent_id INTEGER NOT NULL REFERENCES {_PLUGIN_TABLE}(id)
            )
        """))
    db.commit()


def _drop_plugin_table(db, *, with_child: bool = False) -> None:
    if with_child:
        db.execute(text(f"DROP TABLE IF EXISTS {_PLUGIN_TABLE}_child"))
    db.execute(text(f"DROP TABLE IF EXISTS {_PLUGIN_TABLE}"))
    db.commit()


def _plugin_rows(db, table: str) -> list[tuple]:
    return list(db.execute(text(f"SELECT id, book_id FROM {table} ORDER BY id")).all())


def test_插件表没声明级联_账套照样删得掉(client, auth, db, book):
    """**回归**：插件表的外键没写 ON DELETE CASCADE 时，账套也必须删得掉。

    2026-10-31 生产事故：三个插件建表时都写成 ``sa.ForeignKey("book.id")``，
    库里落成 NO ACTION，``DELETE FROM book`` 被插件表挡住，报 PostgreSQL 23503 ——
    而宿主对 23503 只回一句脱敏的「引用了不存在的关联数据」，
    **八本有插件数据的账全都删不掉**（操作者完全看不出是哪张表挡着）。

    这里复刻那个结构：一张插件样式的表，外键指向 book.id，**不带** ON DELETE。
    """
    _make_plugin_table(db)
    try:
        db.execute(text(
            f"INSERT INTO {_PLUGIN_TABLE} (book_id, note) VALUES (:b, 'x'), (:b, 'y')"),
            {"b": book.id})
        db.commit()
        assert len(_plugin_rows(db, _PLUGIN_TABLE)) == 2

        r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True
        assert _count(db, Book, id=book.id) == 0
        # 插件的账套数据跟着账套一起走 —— 一行都不许剩
        assert _plugin_rows(db, _PLUGIN_TABLE) == []
    finally:
        _drop_plugin_table(db)


def test_删账套只带走本账套的插件行(client, auth, db, book):
    """别账套的插件行**不许**被顺手删掉。"""
    _make_plugin_table(db)
    try:
        other = _make_book(db, "别的账")
        db.execute(text(
            f"INSERT INTO {_PLUGIN_TABLE} (book_id, note) VALUES (:b, 'mine'), (:o, 'theirs')"),
            {"b": book.id, "o": other.id})
        db.commit()

        r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
        assert r.status_code == 200, r.text
        rows = _plugin_rows(db, _PLUGIN_TABLE)
        assert len(rows) == 1
        assert rows[0][1] == other.id, "别的账套的插件行被误删了"
    finally:
        _drop_plugin_table(db)


def test_插件表之间也有外键_按依赖顺序删(client, auth, db, book):
    """插件表**之间**用 NO ACTION 互相引用时，先删子表、再删母表。

    母表还有行被子表引用着就先删母表 → 23503。顺序错了照样删不掉。
    """
    _make_plugin_table(db, with_child=True)
    try:
        db.execute(text(
            f"INSERT INTO {_PLUGIN_TABLE} (book_id, note) VALUES (:b, 'x')"),
            {"b": book.id})
        db.commit()
        parent_id = _plugin_rows(db, _PLUGIN_TABLE)[0][0]
        db.execute(text(
            f"INSERT INTO {_PLUGIN_TABLE}_child (book_id, parent_id) VALUES (:b, :p)"),
            {"b": book.id, "p": parent_id})
        db.commit()

        r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
        assert r.status_code == 200, r.text
        assert _count(db, Book, id=book.id) == 0
        assert _plugin_rows(db, _PLUGIN_TABLE) == []
        assert _plugin_rows(db, _PLUGIN_TABLE + "_child") == []
    finally:
        _drop_plugin_table(db, with_child=True)


def test_删掉的插件行数进审计快照(client, auth, db, book):
    """审计快照要能回答「删这本账时顺手带走了哪张插件表的多少行」。"""
    _make_plugin_table(db)
    try:
        db.execute(text(
            f"INSERT INTO {_PLUGIN_TABLE} (book_id, note) VALUES (:b, 'x'), (:b, 'y'), (:b, 'z')"),
            {"b": book.id})
        db.commit()

        r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
        assert r.status_code == 200, r.text

        rows = db.scalars(select(AuditLog).where(
            AuditLog.action == "DELETE_BOOK", AuditLog.entity_id == str(book.id))).all()
        assert len(rows) == 1
        assert rows[0].before["children"][_PLUGIN_TABLE] == 3
    finally:
        _drop_plugin_table(db)


# ====================================================================== 删得干净


def test_有已记账凭证和已结账期间的账套也能删干净(client, auth, db, book, make_voucher):
    """**最要紧的一条**：一本有账的账（已记账 + 已结账 + 期初 + 发票配置）也能删。

    这三种数据各自对应一个会**直接抛错**的护栏：
      · 结账锁 → fn_voucher_delete_guard / fn_entry_lock_guard
      · 已记账 → fn_posted_voucher_guard
      · 期初 / 发票科目 → 指向 subject 的 RESTRICT 外键
    少做任何一步，删除就失败（500），账套就永远删不掉。
    """
    from sqlalchemy import update

    # 一张已记账的凭证，且它所在的期间已经结账 —— 两道护栏同时压着
    make_voucher(date(2026, 3, 15),
                 dr=[("1001", Decimal("100"), "收现")],
                 cr=[("2202", Decimal("100"), "欠款")],
                 posted=True)
    db.execute(update(Period)
               .where(Period.book_id == book.id, Period.month == 3)
               .values(locked=True))
    db.commit()

    # 期初余额（指向 subject 的 RESTRICT）
    p = db.scalar(select(Period).where(Period.book_id == book.id, Period.month == 1))
    s = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "1001"))
    db.add(PeriodOpening(book_id=book.id, period_id=p.id, subject_id=s.id,
                          amount=Decimal("500")))
    # 发票配置 + 发票规则（同样指向 subject 的 RESTRICT）
    db.add(InvoiceConfig(book_id=book.id, output_tax_subject_id=s.id,
                          input_credit_subject_id=s.id, output_debit_subject_id=s.id))
    db.add(InvoiceRule(book_id=book.id, match_type="SELLER", invoice_type="BOTH",
                        keyword="测试供应商", subject_id=s.id))
    db.commit()

    assert _count(db, Voucher, book_id=book.id) == 1
    assert _count(db, PeriodOpening, book_id=book.id) == 1

    r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    # 一行都不许剩：没有孤儿行，没有「账套没了但科目还在」的残骸
    assert _count(db, Book, id=book.id) == 0
    assert _count(db, Subject, book_id=book.id) == 0
    assert _count(db, Period, book_id=book.id) == 0
    assert _count(db, Voucher, book_id=book.id) == 0
    assert _count(db, Entry, book_id=book.id) == 0
    assert _count(db, PeriodOpening, book_id=book.id) == 0
    assert _count(db, InvoiceConfig, book_id=book.id) == 0
    assert _count(db, InvoiceRule, book_id=book.id) == 0
    assert _count(db, UserBook, book_id=book.id) == 0


def test_删除留痕_审计日志里有这一条(client, auth, db, book):
    """账套没了，但它被删掉这件事要查得到 —— 审计日志是 SET NULL，日志留下。"""
    client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=auth)
    rows = db.scalars(select(AuditLog).where(
        AuditLog.action == "DELETE_BOOK", AuditLog.entity_id == str(book.id))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.username == db.scalar(select(SysUser.username).where(SysUser.id == row.user_id))
    assert "删除账套" in row.detail
    # 快照里带着删之前的规模 —— 事后才知道这本账里曾经有什么
    assert row.before["name"] == book.name
    assert row.before["vouchers"] == 0
    assert row.before["subjects"] > 0
    # 账套没了，这条日志的 book_id 被置空（SET NULL），但行还在
    assert row.book_id is None


def test_路径一也留痕(client, auth, db, book):
    """管理员收回授权这条路同样要留痕。"""
    user = _second_user(db, "被收回的人2")
    db.delete(db.scalar(select(UserBook).where(UserBook.book_id == book.id)))
    _grant(db, user.id, book.id)
    client.put(f"/api/system/users/{user.id}/books", json=[], headers=auth)
    rows = db.scalars(select(AuditLog).where(
        AuditLog.action == "DELETE_BOOK", AuditLog.entity_id == str(book.id))).all()
    assert len(rows) == 1
    assert "删除账套" in rows[0].detail


def test_非授权用户不能碰账套(client, plain_auth, db, book):
    """外人连改名这条路都走不到。"""
    r = client.put(f"/api/books/{book.id}", json={"name": "删除"}, headers=plain_auth)
    assert r.status_code == 403, r.text
    assert _count(db, Book, id=book.id) == 1
