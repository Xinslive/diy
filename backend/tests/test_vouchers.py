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
                 start_year=2026, start_month=1)
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


def test_已记账凭证锁住不能改(db, book, subjects, make_voucher, admin_user):
    """已记账的分录已经被总账和报表引用，改它必须先反记账。

    允许直接改的话，凭证上写的是一个数、报表上是另一个数，
    而且没有任何痕迹能说清差在哪一步。
    """
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    with pytest.raises(ValueError, match="已记账"):
        update_voucher(db, v, admin_user, {
            "entries": _entries(subjects, [("100201", 250, "改")], [("600101", 250, "改")])})
    db.rollback()
    db.refresh(v)
    assert v.status == "POSTED"

    # 反记账之后就能改了
    unpost_vouchers(db, [v.id], admin_user)
    db.refresh(v)
    assert v.status == "UNPOSTED"
    update_voucher(db, v, admin_user, {
        "entries": _entries(subjects, [("100201", 250, "改")], [("600101", 250, "改")])})
    db.refresh(v)
    assert v.status == "UNPOSTED"


def test_改凭证换月后分录期间同步(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-01-31", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
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


def test_已记账凭证不能删_先反记账才行(db, book, make_voucher, admin_user):
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")],
                     posted=True, summary="原来的内容")
    vid = v.id
    with pytest.raises(ValueError, match="已记账"):
        delete_voucher(db, v, admin_user)
    db.rollback()

    unpost_vouchers(db, [vid], admin_user)
    delete_voucher(db, db.get(Voucher, vid), admin_user)
    assert db.get(Voucher, vid) is None


def test_删除凭证要留内容快照(db, book, make_voucher, admin_user):
    """凭证号会复用，删掉之后唯一能还原「原来那张是什么」的东西就是审计快照。"""
    from app.models import AuditLog

    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")],
                     summary="原来的内容")
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


# --------------------------------------------------------------------- 记账门槛
# 业务规矩：只有上一期结账了，本期才允许记账。
# 上一期没封账，本期凭证再去改上期已经变动的数字，账就再也对不回来了。


def test_第一个期间可以记账_没有上一期可依(db, book, subjects, make_voucher, admin_user):
    """账套的第一个期间没有上一期，不该被这条规矩挡住。"""
    v = make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])

    post_vouchers(db, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"


def test_上一期没结账不能记账(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])

    with pytest.raises(ValueError) as e:
        post_vouchers(db, [v.id], admin_user)
    msg = str(e.value)
    assert "2026-02" in msg and "2026-01" in msg, msg
    assert db.get(Voucher, v.id).status == "UNPOSTED", "被拦下来时不该留下已记账"


def test_上一期结账后本期就能记账(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    periods[1].locked = True
    db.commit()

    post_vouchers(db, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"


def test_反结账上一期后本期又记不了账(db, book, periods, subjects, make_voucher, admin_user):
    """上一期一旦反结账，本期已记的凭证不受影响，但新的记账又被挡住。"""
    v = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    periods[1].locked = True
    db.commit()
    post_vouchers(db, [v.id], admin_user)

    periods[1].locked = False
    db.commit()
    v2 = make_voucher("2026-02-11", dr=[("100201", 50, "x")], cr=[("600101", 50, "x")])
    with pytest.raises(ValueError):
        post_vouchers(db, [v2.id], admin_user)
    assert db.get(Voucher, v2.id).status == "UNPOSTED"


def test_批量记账时报出全部未结账的上一期(db, book, periods, subjects, make_voucher, admin_user):
    """一次批多个期间时，错误信息要把每一处该先结谁的账都说清楚。"""
    a = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    periods[1].locked = True
    b = make_voucher("2026-03-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    db.commit()

    with pytest.raises(ValueError) as e:
        post_vouchers(db, [a.id, b.id], admin_user)
    msg = str(e.value)
    assert "2026-03" in msg and "2026-02" in msg, msg
    assert db.get(Voucher, b.id).status == "UNPOSTED"


def test_跨年看的是上年十二月(db, book, admin_user, subjects, make_voucher):
    """2027-01 的上一期是 2026-12，靠 ord 跨年度连续，不能只看同年。"""
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    db.commit()
    # 年度结转要求全年封账，先把 2026 整年结掉，再把 12 月退回来当「未结账」
    dec26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 12))
    carry_forward_profit(db, book.id, dec26, admin_user)
    close_period(db, dec26, admin_user)
    year_end_carry(db, book, admin_user)
    dec26.locked = False
    db.commit()

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2027, Period.month == 1))
    v = make_voucher("2027-01-15", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    db.commit()

    with pytest.raises(ValueError) as e:
        post_vouchers(db, [v.id], admin_user)
    assert "2026-12" in str(e.value), str(e.value)

    dec26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 12))
    dec26.locked = True
    db.commit()
    post_vouchers(db, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"
    assert jan27.locked is False
