"""凭证：编号 / 校验 / 记账两态 / 删除不留号 / 批量 / 查询。

用户明确的四条口径在这里落地：
    · 不审核凭证 —— 只有 UNPOSTED / POSTED 两态
    · 凭证号作废不留号 —— 删除后序号可以复用
    · 批量操作 —— 记账 / 反记账 / 删除都是一次多张
    · 查询要能按摘要、科目、金额、期间筛
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accounting.vouchers import (
    create_voucher,
    delete_voucher,
    next_voucher_no,
    post_vouchers,
    query_vouchers,
    unpost_vouchers,
    update_voucher,
)
from app.models import Entry, Period, Voucher


def sid(subjects, code):
    return subjects[code].id


def _entries(subjects, dr, cr):
    out = [{"subject_id": sid(subjects, c), "direction": "DEBIT", "amount": str(a), "summary": s}
           for c, a, s in dr]
    out += [{"subject_id": sid(subjects, c), "direction": "CREDIT", "amount": str(a), "summary": s}
            for c, a, s in cr]
    return out


# --------------------------------------------------------------------- 校验


def test_借贷不平衡被拒(db, book, admin_user, subjects):
    with pytest.raises(ValueError, match="借贷不平衡"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=_entries(subjects, [("100201", 100, "x")], [("600101", 99, "x")]),
                       commit=False)
    db.rollback()


def test_零金额凭证被拒(db, book, admin_user, subjects):
    with pytest.raises(ValueError, match="大于 0"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=_entries(subjects, [("100201", 0, "x")], [("600101", 0, "x")]),
                       commit=False)
    db.rollback()


def test_跨账套科目被拒(db, book, admin_user, subjects):
    """科目是账套私有的，拿别家账套的 subject_id 来记账必须报错。"""
    import uuid

    from app.accounting.periods import create_periods
    from app.models import Book, Subject
    from app.seed.chart_of_accounts import install_chart

    other = Book(company_name=f"别家{uuid.uuid4().hex[:6]}", name="别家账套",
                 year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    install_chart(db, other.id)
    db.commit()

    other_bank = db.scalar(
        select(Subject.id).where(Subject.book_id == other.id, Subject.code == "100201")
    )
    with pytest.raises(ValueError, match="科目不存在或已停用"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=[
                           {"subject_id": other_bank, "direction": "DEBIT",
                            "amount": "10", "summary": "x"},
                           {"subject_id": sid(subjects, "600101"), "direction": "CREDIT",
                            "amount": "10", "summary": "x"},
                       ], commit=False)
    db.rollback()


def test_日期落在账套外报错(db, book, admin_user, subjects):
    with pytest.raises(ValueError, match="不在本账套的会计期间范围内"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2030, 1, 5),
                       entries=_entries(subjects, [("100201", 10, "x")], [("600101", 10, "x")]),
                       commit=False)
    db.rollback()


# --------------------------------------------------------------------- 编号


def test_凭证号期间内顺序编号(db, book, periods, admin_user, subjects, make_voucher):
    a = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    b = make_voucher("2026-01-06", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert (a.voucher_no, b.voucher_no) == ("记0001", "记0002")


def test_不同期间各自从0001开始(db, book, periods, admin_user, subjects, make_voucher):
    a = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    b = make_voucher("2026-02-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert a.voucher_no == "记0001" and b.voucher_no == "记0001"


def test_作废不留号_删除后序号复用(db, book, periods, admin_user, subjects, make_voucher):
    """用户明确要求：删掉 记0001 之后，新凭证就应该是 记0001。"""
    a = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    b = make_voucher("2026-01-06", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert b.voucher_no == "记0002"

    delete_voucher(db, a, admin_user)
    c = make_voucher("2026-01-07", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])

    # 下一个号取「当前最大值 + 1」，所以删中间那张不会让后续号前移，
    # 但删掉最后一张后号一定会被复用。
    assert c.voucher_no == "记0003"
    delete_voucher(db, b, admin_user)
    delete_voucher(db, c, admin_user)
    d = make_voucher("2026-01-08", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert d.voucher_no == "记0001"


def test_凭证号预览(db, book, periods, admin_user, subjects, make_voucher):
    make_voucher("2026-03-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    make_voucher("2026-03-06", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert next_voucher_no(db, periods[3]) == "记0003"


# --------------------------------------------------------------------- 两态


def test_凭证只有未记账和已记账两态(db, book, make_voucher, admin_user):
    """不审核 —— 模型里根本没有 REVIEW/APPROVED 这一档。"""
    from app.models import Voucher as V
    cols = {c.name for c in V.__table__.columns}
    assert "reviewed" not in cols and "audit_status" not in cols
    assert {"status"} <= cols

    v = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert v.status == "UNPOSTED"
    post_vouchers(db, [v.id], admin_user)
    db.refresh(v)
    assert v.status == "POSTED"
    assert v.posted_by is not None and v.posted_at is not None


def test_批量记账和反记账(db, book, make_voucher, admin_user):
    vs = [make_voucher(f"2026-01-0{i+5}", dr=[("100201", 10, "x")], cr=[("600101", 10, "x")])
          for i in range(3)]

    assert post_vouchers(db, [v.id for v in vs], admin_user) == 3
    assert all(db.get(Voucher, v.id).status == "POSTED" for v in vs)

    # 重复记账返回 0，不报错也不重复计数
    assert post_vouchers(db, [v.id for v in vs], admin_user) == 0

    assert unpost_vouchers(db, [v.id for v in vs], admin_user) == 3
    assert all(db.get(Voucher, v.id).status == "UNPOSTED" for v in vs)
    assert unpost_vouchers(db, [v.id for v in vs], admin_user) == 0


def test_改凭证后自动回到未记账(db, book, subjects, make_voucher, admin_user):
    """已记账的凭证被改动，账簿口径必须立刻跟着变，所以要退回未记账。"""
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    update_voucher(db, v, admin_user, {
        "entries": _entries(subjects, [("100201", 250, "改")], [("600101", 250, "改")])})
    db.refresh(v)
    assert v.status == "UNPOSTED"
    assert v.posted_at is None


def test_改凭证换月后分录期间同步(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-01-31", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    update_voucher(db, v, admin_user, {"voucher_date": date(2026, 3, 15)})
    db.refresh(v)
    assert v.period_id == periods[3].id
    assert all(e.period_ord == 3 for e in db.scalars(
        select(Entry).where(Entry.voucher_id == v.id)).all())


def test_删除凭证连同分录(db, book, make_voucher, admin_user):
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    vid = v.id
    delete_voucher(db, v, admin_user)
    assert db.get(Voucher, vid) is None
    assert db.scalars(select(Entry).where(Entry.voucher_id == vid)).all() == []


def test_已记账凭证可直接删除但要留快照(db, book, make_voucher, admin_user):
    """没有审核环节，所以不拦着删已记账凭证。

    但凭证号会复用，删掉之后凭证号就可能被别的凭证占用 ——
    唯一能还原「原来那张是什么」的东西就是审计快照。
    """
    from app.models import AuditLog

    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")],
                     posted=True, summary="原来的内容")
    vid, vno = v.id, v.voucher_no
    delete_voucher(db, v, admin_user)
    assert db.get(Voucher, vid) is None

    # 必须按 book_id 过滤：各测试共用同一张审计表，只按 action 查会拿到别的用例的记录
    row = db.scalar(select(AuditLog).where(
        AuditLog.book_id == book.id, AuditLog.action == "DELETE", AuditLog.entity == "voucher"))
    assert row is not None
    before = row.before
    assert before["voucher_no"] == vno
    assert before["remark"] == "原来的内容"
    assert len(before["entries"]) == 2


# --------------------------------------------------------------------- 查询


def test_按摘要和备注检索(db, book, make_voucher):
    make_voucher("2026-01-05", dr=[("100201", 1, "租金")], cr=[("600101", 1, "租金")])
    make_voucher("2026-01-06", dr=[("100201", 1, "办公")], cr=[("6602", 1, "办公")])

    rows, total = query_vouchers(db, book.id, keyword="租金")
    assert total == 1
    rows, total = query_vouchers(db, book.id, keyword="办公")
    assert total == 1


def test_按科目筛选(db, book, subjects, make_voucher):
    make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    make_voucher("2026-01-06", dr=[("1122", 1, "y")], cr=[("600101", 1, "y")])

    rows, total = query_vouchers(db, book.id, subject_id=sid(subjects, "1122"))
    assert total == 1 and rows[0].voucher_date == date(2026, 1, 6)


def test_按期间区间筛选(db, book, periods, make_voucher):
    make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    make_voucher("2026-02-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    make_voucher("2026-03-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])

    rows, total = query_vouchers(db, book.id, from_period=periods[2], to_period=periods[3])
    assert total == 2
    rows, total = query_vouchers(db, book.id, from_period=periods[1])
    assert total == 3


def test_按状态和来源筛选(db, book, make_voucher):
    make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")], posted=True)
    make_voucher("2026-01-06", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    make_voucher("2026-01-07", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")],
                 source="CARRY_FORWARD")

    assert query_vouchers(db, book.id, status="POSTED")[1] == 1
    assert query_vouchers(db, book.id, status="UNPOSTED")[1] == 2
    assert query_vouchers(db, book.id, source="CARRY_FORWARD")[1] == 1


def test_分页(db, book, make_voucher):
    for i in range(7):
        make_voucher(f"2026-01-{i+3:02d}", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    rows, total = query_vouchers(db, book.id, page=1, page_size=3)
    assert total == 7 and len(rows) == 3
    rows, total = query_vouchers(db, book.id, page=3, page_size=3)
    assert len(rows) == 1


def test_默认按日期倒序(db, book, make_voucher):
    a = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    b = make_voucher("2026-01-20", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    rows, _ = query_vouchers(db, book.id)
    assert [r.id for r in rows] == [b.id, a.id]
