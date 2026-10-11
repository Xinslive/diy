"""明细账的账套隔离（审计 P2-04）。

``general`` 接口校验了期间归属，``detail`` 对科目却只写了一句
``db.get(Subject, subject_id)`` —— 不带 book_id 约束。同一个文件里
两种口径，放进去别的账套的科目 ID 也能过。

分录查询本身带 ``Entry.book_id == book_id``，所以**没有金额泄露**，
问题出在科目元数据（编码、名称）会跨账套参与计算，并可能返回一个
「看着正常、其实是空账」的页面 —— 用户会以为这个科目真的一分钱没有。

所以断言的是 404，不是「返回空页」。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Book, Period, Subject, UserBook


@pytest.fixture
def other_book(db, admin_user):
    """另一个账套 —— 攻击者手里的「目标」，与主账套完全无关。"""
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


def _first_subject(db, book_id):
    return db.scalars(select(Subject).where(
        Subject.book_id == book_id, Subject.is_leaf.is_(True))
        .order_by(Subject.code)).first()


def test_明细账拒绝别的账套的科目(client, auth, db, book, other_book):
    """验收项：跨账套科目 ID 一律 404。"""
    foreign = _first_subject(db, other_book.id)
    assert foreign.id != _first_subject(db, book.id).id

    r = client.get(f"/api/books/{book.id}/ledger/detail?subject_id={foreign.id}",
                   headers=auth)
    assert r.status_code == 404, (
        f"跨账套科目竟然返回了 {r.status_code}："
        f"别的账套 {foreign.code} 的元数据参与了本账套的账页计算")


def test_明细账拒绝不存在的科目(client, auth, db, book):
    r = client.get(f"/api/books/{book.id}/ledger/detail?subject_id=99999999", headers=auth)
    assert r.status_code == 404, f"不存在的科目返回了 {r.status_code}"


def test_明细账拒绝本账套的非明细科目也不报错(client, auth, db, book, subjects):
    """非明细科目本身是合法 ID，只是没有自己的分录 —— 该返回空页而不是 404。

    这条钉的是**别把闸门开过头**：把 is_leaf 也一起拦掉的话，
    用户点上级科目的小计行就会撞 404。
    """
    parent = next(s for s in subjects.values() if not s.is_leaf)
    r = client.get(f"/api/books/{book.id}/ledger/detail?subject_id={parent.id}",
                   headers=auth)
    assert r.status_code == 200, f"本账套的非明细科目应当能查（空页即可）：{r.text}"


def test_本账套科目的明细账照常能查(client, auth, db, book, subjects, make_voucher):
    """收紧不能误伤正常路径。"""
    make_voucher("2026-04-10", dr=[("100201", 300, "x")],
                 cr=[("600101", 300, "x")], posted=True)
    sid_ = subjects["100201"].id

    r = client.get(f"/api/books/{book.id}/ledger/detail?subject_id={sid_}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject"]["code"] == "100201"
    assert any(row.get("voucher_no") for row in body["rows"]), "分录没出现在明细账里"


def test_总账的起始期间归属校验仍在(client, auth, db, book, other_book):
    """对照组：``general`` 本来就有这道校验，确认它没被顺手删掉。"""
    foreign_period = db.scalars(select(Period).where(
        Period.book_id == other_book.id)).first()

    r = client.get(f"/api/books/{book.id}/ledger/general"
                   f"?from_period_id={foreign_period.id}", headers=auth)
    assert r.status_code == 404, f"总账的跨账套期间校验丢了：{r.status_code}"
