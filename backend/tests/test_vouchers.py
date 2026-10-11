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
    insert_slot,
    side_totals,
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
    """一条金额 0 的分录不是「记了 0 块」，是没填。负数（红字冲销）另算，见下。"""
    with pytest.raises(ValueError, match="不能是 0"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=_entries(subjects, [("100201", 0, "x")], [("600101", 0, "x")]),
                       commit=False)
    db.rollback()


def test_挂上级科目被拒(db, book, admin_user, subjects):
    """分录只能记在末级科目上：前端面板不放，服务端也得自己挡一道。

    前端能绕的路太多（接口直调、导入、老凭证本来就挂着），
    只在面板里不给选，等于没拦。这里的用例把绕过去之后的样子钉死。
    """
    bank = subjects["1002"]
    assert bank.is_leaf is False
    with pytest.raises(ValueError, match="不是末级科目"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=[
                           {"subject_id": bank.id, "direction": "DEBIT",
                            "amount": "10", "summary": "x"},
                           {"subject_id": sid(subjects, "600101"), "direction": "CREDIT",
                            "amount": "10", "summary": "x"},
                       ], commit=False)
    db.rollback()


def test_改凭证也挡住上级科目(db, book, admin_user, subjects, make_voucher):
    """改一张凭证时也不行 —— 新建拦住了，改却放过去，等于留了个后门。"""
    v = make_voucher(date(2026, 1, 5),
                     dr=[("100201", 10, "x")], cr=[("600101", 10, "x")])
    with pytest.raises(ValueError, match="不是末级科目"):
        update_voucher(db, voucher=v, user=admin_user, payload={"entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": sid(subjects, "600101"), "direction": "CREDIT",
             "amount": "10", "summary": "x"},
        ]})
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
    assert (a.voucher_no, b.voucher_no) == ("记001", "记002")


def test_不同期间各自从0001开始(db, book, periods, admin_user, subjects, make_voucher):
    a = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    b = make_voucher("2026-02-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert a.voucher_no == "记001" and b.voucher_no == "记001"


def test_作废不留号_删除后序号复用(db, book, periods, admin_user, subjects, make_voucher):
    """用户明确要求：删掉 记001 之后，新凭证就应该是 记001。"""
    a = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    b = make_voucher("2026-01-06", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert b.voucher_no == "记002"

    delete_voucher(db, a, admin_user)
    c = make_voucher("2026-01-07", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])

    # 下一个号取「当前最大值 + 1」，所以删中间那张不会让后续号前移，
    # 但删掉最后一张后号一定会被复用。
    assert c.voucher_no == "记003"
    delete_voucher(db, b, admin_user)
    delete_voucher(db, c, admin_user)
    d = make_voucher("2026-01-08", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert d.voucher_no == "记001"


def test_凭证号预览(db, book, periods, admin_user, subjects, make_voucher):
    make_voucher("2026-03-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    make_voucher("2026-03-06", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert next_voucher_no(db, periods[3]) == "记003"


# --------------------------------------------------------------------- 两态


def test_凭证只有未记账和已记账两态(db, book, make_voucher, admin_user):
    """不审核 —— 模型里根本没有 REVIEW/APPROVED 这一档。"""
    from app.models import Voucher as V
    cols = {c.name for c in V.__table__.columns}
    assert "reviewed" not in cols and "audit_status" not in cols
    assert {"status"} <= cols

    v = make_voucher("2026-01-05", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert v.status == "UNPOSTED"
    post_vouchers(db, book.id, [v.id], admin_user)
    db.refresh(v)
    assert v.status == "POSTED"
    assert v.posted_by is not None and v.posted_at is not None


def test_批量记账和反记账(db, book, make_voucher, admin_user):
    vs = [make_voucher(f"2026-01-0{i+5}", dr=[("100201", 10, "x")], cr=[("600101", 10, "x")])
          for i in range(3)]

    assert post_vouchers(db, book.id, [v.id for v in vs], admin_user) == 3
    assert all(db.get(Voucher, v.id).status == "POSTED" for v in vs)

    # 重复记账返回 0，不报错也不重复计数
    assert post_vouchers(db, book.id, [v.id for v in vs], admin_user) == 0

    assert unpost_vouchers(db, book.id, [v.id for v in vs], admin_user) == 3
    assert all(db.get(Voucher, v.id).status == "UNPOSTED" for v in vs)
    assert unpost_vouchers(db, book.id, [v.id for v in vs], admin_user) == 0


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
    unpost_vouchers(db, book.id, [v.id], admin_user)
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

    unpost_vouchers(db, book.id, [vid], admin_user)
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


def test_按金额区间筛选(db, book, make_voucher):
    # 「金额」筛的是**某一条分录**的金额，不是整张凭证的合计。
    # 单借单贷时两条分录同额，看起来像合计，其实是一回事。
    make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])   # 每行 100
    make_voucher("2026-01-06", dr=[("100201", 500, "y")], cr=[("600101", 500, "y")])   # 每行 500
    make_voucher("2026-01-07", dr=[("100201", 800, "z")], cr=[("600101", 800, "z")])   # 每行 800

    assert query_vouchers(db, book.id, min_amount=Decimal("500"))[1] == 2
    assert query_vouchers(db, book.id, max_amount=Decimal("499"))[1] == 1
    assert query_vouchers(db, book.id,
                          min_amount=Decimal("400"), max_amount=Decimal("700"))[1] == 1
    assert query_vouchers(db, book.id, min_amount=Decimal("900"))[1] == 0


def test_金额区间筛的是单行而不是合计(db, book, make_voucher):
    # 借 300 + 200，贷 400 + 100：没有一行达到 700，但合计够 700。
    # 按「单行」语义这张不该被 min_amount=700 捞出来。
    v = make_voucher(
        "2026-01-08",
        dr=[("100201", 300, "a"), ("100201", 200, "b")],
        cr=[("600101", 400, "a"), ("2221", 100, "b")],
    )
    # 但 400 那行确实落在 300~500 区间里，凭证要被捞出来
    rows, hit = query_vouchers(db, book.id,
                               min_amount=Decimal("300"), max_amount=Decimal("500"))
    assert hit == 1 and rows[0].id == v.id
    # 一行都够不着 700 → 不该命中（合计语义会误命中）
    assert query_vouchers(db, book.id, min_amount=Decimal("700"))[1] == 0
    # 350~380 这一段没有行落在里面（实际各行是 300/200/400/100）→ 不该命中
    assert query_vouchers(db, book.id,
                          min_amount=Decimal("350"), max_amount=Decimal("380"))[1] == 0
    # 单边上限 190：只有 100 那行够低 → 命中
    assert query_vouchers(db, book.id, max_amount=Decimal("190"))[1] == 1


def test_金额区间借贷两边都参与匹配(db, book, make_voucher):
    # 只借方有 800、贷方只有 100 的凭证，按单行语义 800 那行照样命中
    v = make_voucher("2026-01-09", dr=[("100201", 800, "a")], cr=[("600101", 100, "a"), ("2221", 700, "b")])
    rows, hit = query_vouchers(db, book.id, min_amount=Decimal("790"))
    assert hit == 1 and rows[0].id == v.id


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

    post_vouchers(db, book.id, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"


def test_上一期没结账不能记账(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])

    with pytest.raises(ValueError) as e:
        post_vouchers(db, book.id, [v.id], admin_user)
    msg = str(e.value)
    assert "2026-02" in msg and "2026-01" in msg, msg
    assert db.get(Voucher, v.id).status == "UNPOSTED", "被拦下来时不该留下已记账"


def test_上一期结账后本期就能记账(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    periods[1].locked = True
    db.commit()

    post_vouchers(db, book.id, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"


def test_反结账上一期后本期又记不了账(db, book, periods, subjects, make_voucher, admin_user):
    """上一期一旦反结账，本期已记的凭证不受影响，但新的记账又被挡住。"""
    v = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    periods[1].locked = True
    db.commit()
    post_vouchers(db, book.id, [v.id], admin_user)

    periods[1].locked = False
    db.commit()
    v2 = make_voucher("2026-02-11", dr=[("100201", 50, "x")], cr=[("600101", 50, "x")])
    with pytest.raises(ValueError):
        post_vouchers(db, book.id, [v2.id], admin_user)
    assert db.get(Voucher, v2.id).status == "UNPOSTED"


def test_批量记账时报出全部未结账的上一期(db, book, periods, subjects, make_voucher, admin_user):
    """一次批多个期间时，错误信息要把每一处该先结谁的账都说清楚。"""
    a = make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    periods[1].locked = True
    b = make_voucher("2026-03-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    db.commit()

    with pytest.raises(ValueError) as e:
        post_vouchers(db, book.id, [a.id, b.id], admin_user)
    msg = str(e.value)
    assert "2026-03" in msg and "2026-02" in msg, msg
    assert db.get(Voucher, b.id).status == "UNPOSTED"


def test_跨年看的是上年十二月(db, book, admin_user, subjects, make_voucher):
    """2027-01 的上一期是 2026-12，靠 ord 跨年度连续，不能只看同年。"""
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    db.commit()
    # 新规矩：上月没结账就不许给本月结转。所以先把 1 月结转结掉，
    # 再一路封到 11 月，12 月才轮到它结转。
    jan26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 1))
    nov26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 11))
    carry_forward_profit(db, book.id, jan26, admin_user)
    close_period(db, jan26, admin_user)
    close_period(db, nov26, admin_user)      # 级联封掉 2~11 月

    # 年度结转要求全年封账，先把 2026 整年结掉，再把 12 月退回来当「未结账」
    # 1 月的损益已经在 1 月结转走了，12 月账上没余额，不必再结转
    dec26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 12))
    close_period(db, dec26, admin_user)
    year_end_carry(db, book, admin_user)
    dec26.locked = False
    db.commit()

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2027, Period.month == 1))
    v = make_voucher("2027-01-15", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    db.commit()

    with pytest.raises(ValueError) as e:
        post_vouchers(db, book.id, [v.id], admin_user)
    assert "2026-12" in str(e.value), str(e.value)

    dec26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 12))
    dec26.locked = True
    db.commit()
    post_vouchers(db, book.id, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"
    assert jan27.locked is False


# --------------------------------------------------------------------- 前一张/后一张


def test_前一张后一张按录入顺序翻(client, auth, book, make_voucher):
    """同期间内按 (日期, id) 升序翻 —— 连续录入时「后一张」是接下来要填的那张。"""
    v1 = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    v2 = make_voucher("2026-01-10", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])
    v3 = make_voucher("2026-01-20", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")])

    def nb(v):
        r = client.get(f"/api/books/{book.id}/vouchers/{v.id}/neighbour", headers=auth)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["scope"] == "period"      # 没带筛选条件 → 退回同期间
        return (d["prev_id"], d["next_id"])

    assert nb(v1) == (None, v2.id)
    assert nb(v2) == (v1.id, v3.id)
    assert nb(v3) == (v2.id, None)


def test_同一天的多张凭证按录入先后排(client, auth, book, make_voucher):
    """同一天的两张凭证必须能区分前后，否则「后一张」会来回跳。"""
    a = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    b = make_voucher("2026-01-05", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])

    r = client.get(f"/api/books/{book.id}/vouchers/{b.id}/neighbour", headers=auth).json()
    assert (r["prev_id"], r["next_id"]) == (a.id, None)
    r2 = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour", headers=auth).json()
    assert (r2["prev_id"], r2["next_id"]) == (None, b.id)


def test_前后张不跨会计期间(client, auth, book, make_voucher):
    """1 月最后一张的后一张不该跳到 2 月 —— 跳过去日期会突然变掉。"""
    jan = make_voucher("2026-01-28", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    feb = make_voucher("2026-02-03", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])

    assert client.get(f"/api/books/{book.id}/vouchers/{jan.id}/neighbour", headers=auth).json()["next_id"] is None
    assert client.get(f"/api/books/{book.id}/vouchers/{feb.id}/neighbour", headers=auth).json()["prev_id"] is None


def test_带筛选条件时前后张翻的是结果集(client, auth, book, make_voucher):
    """用户举的例子：筛出「凭证号含 16」的（每月一张），进 8 月那张，
    前一张是 7 月那张，后一张从 7 月点回来还是 8 月。
    顺序是**时间正序**—— 跟列表那个倒序不是一个东西，混了会跳去 9 月。"""
    def mk(day, no):
        v = make_voucher(day, dr=[("100201", 100, no)], cr=[("600101", 100, no)])
        return v

    jul = mk("2026-07-31", "16 七月")
    aug = mk("2026-08-31", "16 八月")
    sep = mk("2026-09-30", "16 九月")
    # 不带 16 的干扰项，筛的时候本来就该被排掉
    other = mk("2026-08-15", "别的")

    def nb(v):
        r = client.get(
            f"/api/books/{book.id}/vouchers/{v.id}/neighbour?keyword=16", headers=auth)
        assert r.status_code == 200, r.text
        return r.json()

    r = nb(aug)
    assert r["scope"] == "filtered"
    assert r["prev_id"] == jul.id      # 8 月的前一张 = 7 月
    assert r["next_id"] == sep.id
    r2 = nb(jul)
    assert r2["next_id"] == aug.id     # 7 月的后一张 = 8 月（能跳回去）
    assert other.id not in (r["prev_id"], r["next_id"], r2["next_id"])


def test_筛选结果里的头尾没有前后张(client, auth, book, make_voucher):
    a = make_voucher("2026-05-31", dr=[("100201", 100, "16 a")], cr=[("600101", 100, "a")])
    b = make_voucher("2026-06-30", dr=[("100201", 100, "16 b")], cr=[("600101", 100, "b")])
    c = make_voucher("2026-07-31", dr=[("100201", 100, "16 c")], cr=[("600101", 100, "c")])

    first = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour?keyword=16", headers=auth).json()
    last = client.get(f"/api/books/{book.id}/vouchers/{c.id}/neighbour?keyword=16", headers=auth).json()
    assert (first["prev_id"], first["next_id"], first["index"], first["total"]) == (None, b.id, 0, 3)
    assert (last["prev_id"], last["next_id"], last["index"]) == (b.id, None, 2)


def test_这张不在结果集里就两头都没有(client, auth, book, make_voucher):
    """筛选条件刚改过、当前这张被筛掉了时，不该瞎指一张邻票。"""
    keep = make_voucher("2026-05-31", dr=[("100201", 100, "16")], cr=[("600101", 100, "x")])
    gone = make_voucher("2026-06-30", dr=[("100201", 100, "别的")], cr=[("600101", 100, "x")])
    r = client.get(f"/api/books/{book.id}/vouchers/{gone.id}/neighbour?keyword=16", headers=auth).json()
    assert r["prev_id"] is None and r["next_id"] is None and r["index"] == -1
    assert keep.id is not None


def test_金额筛选也参与前后张(client, auth, book, make_voucher):
    """分录金额区间同样带着翻 —— 跟列表筛出来的结果必须是同一个集合。"""
    small = make_voucher("2026-05-31", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    big = make_voucher("2026-06-30", dr=[("100201", 900000, "x")], cr=[("600101", 900000, "x")])

    r = client.get(
        f"/api/books/{book.id}/vouchers/{big.id}/neighbour?min_amount=100000", headers=auth).json()
    assert r["scope"] == "filtered"
    assert r["total"] == 1 and r["prev_id"] is None and r["next_id"] is None
    # 小的那张被区间挡在外面，两头都没有
    r2 = client.get(
        f"/api/books/{book.id}/vouchers/{small.id}/neighbour?min_amount=100000", headers=auth).json()
    assert r2["index"] == -1


def test_新开一张的预填值(client, auth, db, book, subjects, make_voucher):
    """凭证号 = 下一个号，日期 = 这个期间最后一张的日期。"""
    make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    make_voucher("2026-01-28", dr=[("100201", 100, "y")], cr=[("600101", 100, "y")])
    p1 = book.periods[0]

    r = client.get(f"/api/books/{book.id}/vouchers/next", params={"period_id": p1.id}, headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["voucher_date"] == "2026-01-28"        # 不是今天，也不是 1 号
    assert d["period_id"] == p1.id

    # 号码跟真落库发出来的是同一个（预填要是跟实际对不上，就等于白填一次）
    made = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-28",
        "entries": _entries(subjects, [("100201", 1, "x")], [("600101", 1, "x")]),
    })
    assert made.status_code == 201, made.text
    assert made.json()["voucher_no"] == d["voucher_no"]


def test_拿别的账套的期间问不出号(client, auth, db, book):
    from app.models import Book, Period
    other_p = db.scalar(select(Period).where(Period.book_id != book.id).limit(1))
    if other_p is None:
        return
    r = client.get(f"/api/books/{book.id}/vouchers/next",
                   params={"period_id": other_p.id}, headers=auth)
    assert r.status_code == 404, r.text


def test_凭证号撞了报人话(client, auth, book, subjects, make_voucher):
    """页面上预填了号，两个人同时开一张就会撞。撞了要说清改哪个字段。"""
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_no": v.voucher_no, "voucher_date": "2026-01-06",
        "entries": _entries(subjects, [("100201", 1, "x")], [("600101", 1, "x")]),
    })
    assert r.status_code == 400, r.text
    assert v.voucher_no in r.json()["detail"]      # 把撞上的号说清楚
    assert "唯一约束" not in r.json()["detail"]     # 而不是甩一个约束名出来


def test_别的账套的凭证查不到前后张(client, auth, db, book, make_voucher):
    from app.models import Book
    other = db.scalar(select(Book).where(Book.id != book.id).limit(1))
    if other is None:
        return
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    code = client.get(f"/api/books/{other.id}/vouchers/{v.id}/neighbour", headers=auth).status_code
    assert code in (403, 404), f"不该返回 {code}"


# ===================================================================== 插入凭证


def _stack(make_voucher, n=10, month=3):
    """连着造 n 张 2026-0X 的凭证，号自然是 记001..记010。"""
    out = []
    for i in range(n):
        v = make_voucher(f"2026-{month:02d}-{(i % 28) + 1:02d}",
                         dr=[("100201", 100, "收入")], cr=[("600101", 100, "收入")])
        out.append(v)
    return out


def test_插入凭证拿到参照的号_后面的整体顺位(db, book, periods, admin_user, subjects, make_voucher):
    """记001..记010，打开记006插入 → 新凭证拿记006，原记006..记010 变记007..记011。"""
    stack = _stack(make_voucher)
    anchor = stack[5]                      # 记006

    new = create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 6),
        entries=_entries(subjects, [("100201", 50, "插入")], [("600101", 50, "插入")]),
        insert_after_id=anchor.id,
    )

    assert new.voucher_no == "记006"
    db.refresh(anchor)
    assert anchor.voucher_no == "记007"
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == [
        "记001", "记002", "记003", "记004", "记005",
        "记007", "记008", "记009", "记010", "记011",
    ]
    # 内容一条没动：顺位只改号，不碰分录
    assert db.get(Voucher, stack[0].id).entries[0].amount == Decimal("100.00")


def test_插入凭证顺延已经记账的号(db, book, admin_user, subjects, make_voucher):
    """已记账的凭证也要让位 —— 它只是换号，账里的数一个没动。"""
    first = make_voucher("2026-03-01", dr=[("100201", 100, "收入")],
                         cr=[("600101", 100, "收入")])
    anchor = make_voucher("2026-03-02", dr=[("100201", 100, "收入")],
                          cr=[("600101", 100, "收入")], posted=True)
    last = make_voucher("2026-03-03", dr=[("100201", 100, "收入")],
                        cr=[("600101", 100, "收入")])
    stack = [first, anchor, last]
    create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 2),
        entries=_entries(subjects, [("100201", 50, "插入")], [("600101", 50, "插入")]),
        insert_after_id=stack[1].id,
    )
    assert db.get(Voucher, stack[1].id).voucher_no == "记003"
    assert db.get(Voucher, stack[2].id).voucher_no == "记004"


def test_插入位是最后一张就只是接在末尾(db, book, admin_user, subjects, make_voucher):
    stack = _stack(make_voucher, n=3)
    new = create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 3),
        entries=_entries(subjects, [("100201", 50, "尾")], [("600101", 50, "尾")]),
        insert_after_id=stack[2].id,
    )
    assert new.voucher_no == "记003"
    assert db.get(Voucher, stack[2].id).voucher_no == "记004"


def test_插入凭证不建就不顺位(db, book, admin_user, subjects, make_voucher):
    """一个格子都没填就退出 = 什么都没发生：别人的号原封不动。

    顺位跟建凭证在同一个事务里，没有凭证就没有顺位 —— 这条是「插入」这件事
    唯一安全的前提，不能靠前端记得别提前改号。
    """
    stack = _stack(make_voucher, n=4)
    before = [db.get(Voucher, v.id).voucher_no for v in stack]
    slot = insert_slot(db, stack[1])
    assert slot["no"] == "记002"           # 只算不改
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == before


def test_插入位不能跨会计期间(db, book, admin_user, subjects, make_voucher):
    jan = make_voucher("2026-01-05", dr=[("100201", 10, "a")], cr=[("600101", 10, "a")])
    mar = make_voucher("2026-03-05", dr=[("100201", 10, "a")], cr=[("600101", 10, "a")])
    with pytest.raises(ValueError, match="同一个会计期间"):
        create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=date(2026, 3, 6),
            entries=_entries(subjects, [("100201", 10, "b")], [("600101", 10, "b")]),
            insert_after_id=jan.id,
        )
    assert db.get(Voucher, mar.id).voucher_no == "记001"


def test_手改了号就照人的号来_顺位照做(db, book, admin_user, subjects, make_voucher):
    """页面预填的是插入位，但人有权改 —— 顺位照做，占的是人填的号。"""
    stack = _stack(make_voucher, n=3)
    new = create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 2),
        entries=_entries(subjects, [("100201", 50, "改号")], [("600101", 50, "改号")]),
        voucher_no="记099", insert_after_id=stack[1].id,
    )
    assert new.voucher_no == "记099"
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == ["记001", "记003", "记004"]


def test_插入位撞上别人已占的号要报人话(db, book, admin_user, subjects, make_voucher):
    """顺位只顶开 anchor 之后的号；手填一个还在别人手上的号，照样要拦下来。"""
    stack = _stack(make_voucher, n=3)
    with pytest.raises(ValueError, match="记001 在 2026-03 已经有一张了"):
        create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=date(2026, 3, 3),
            entries=_entries(subjects, [("100201", 50, "插")], [("600101", 50, "插")]),
            voucher_no="记001", insert_after_id=stack[1].id,
        )
    # 撞了就整笔回滚：顺位也不能留下半个
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == ["记001", "记002", "记003"]


def test_插入预览只算不改(client, auth, book_id, admin_user, subjects, make_voucher):
    stack = _stack(make_voucher, n=6)
    r = client.get(f"/api/books/{book_id}/vouchers/insert-preview",
                   params={"after_id": stack[3].id}, headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["anchor_no"] == "记004"
    assert data["voucher_no"] == "记004"
    assert data["voucher_date"] == "2026-03-04"
    assert [m["from"] for m in data["moved"]] == ["记004", "记005", "记006"]
    assert [m["to"] for m in data["moved"]] == ["记005", "记006", "记007"]
    assert db_get_no(client, auth, book_id, stack[3].id) == "记004"   # 没被改


def db_get_no(client, auth, book_id, voucher_id):
    r = client.get(f"/api/books/{book_id}/vouchers/{voucher_id}", headers=auth)
    return r.json()["voucher_no"]


def test_插入预览的参照不存在就404(client, auth, book_id):
    assert client.get(f"/api/books/{book_id}/vouchers/insert-preview",
                      params={"after_id": 999999}, headers=auth).status_code == 404


def test_走接口插入凭证号也是顺位的(client, auth, book_id, admin_user, subjects, make_voucher):
    stack = _stack(make_voucher, n=3)
    sid_d = subjects["100201"].id
    sid_c = subjects["600101"].id
    r = client.post(f"/api/books/{book_id}/vouchers", headers=auth, json={
        "voucher_date": "2026-03-02",
        "insert_after": stack[1].id,
        "entries": [
            {"subject_id": sid_d, "direction": "DEBIT", "amount": "10", "summary": "插"},
            {"subject_id": sid_c, "direction": "CREDIT", "amount": "10", "summary": "插"},
        ],
    })
    assert r.status_code == 201, r.text
    assert r.json()["voucher_no"] == "记002"
    assert db_get_no(client, auth, book_id, stack[1].id) == "记003"


# ===================================================================== 红字冲销


def test_红字凭证借贷两边都是负的也存得下(db, book, admin_user, subjects, make_voucher):
    """冲销老账就是这个形状：-300 / -300。它是平的，就该存。"""
    v = make_voucher("2026-03-01", dr=[("100201", 300, "收")], cr=[("600101", 300, "收")])
    red = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 3, 2),
        entries=_entries(subjects, [("100201", -300, "冲")], [("600101", -300, "冲")]),
    )
    assert [e.amount for e in red.entries] == [Decimal("-300.00"), Decimal("-300.00")]
    assert v.id != red.id


def test_金额为0的分录仍然被拒(db, book, admin_user, subjects):
    with pytest.raises(ValueError, match="不能是 0"):
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 3, 1),
            entries=_entries(subjects, [("100201", 0, "x")], [("600101", 0, "x")]),
        )


def test_合计口径_贷方红字算到借方():
    """借 100 / 借 200 / 贷 −300 → 借方合计 600、贷方合计 0。

    红字的本意是「贷方那一笔其实是借方」，不是「贷方减了 300」。
    照带符号相加会印出「借 300 / 贷 −300、差额 600」，看着像凭空多出 600 块。
    """
    entries = [
        {"direction": "DEBIT", "amount": "100"},
        {"direction": "DEBIT", "amount": "200"},
        {"direction": "CREDIT", "amount": "-300"},
    ]
    assert [str(x) for x in side_totals(entries)] == ["600.00", "0"]


def test_合计口径_借方红字算到贷方():
    """借 −100 / 借 200 / 贷 100 → 借方 200、贷方 200。

    借方那个红字是冲收入用的，它顶的是贷方，不是把借方的 200 减成 100。
    """
    entries = [
        {"direction": "DEBIT", "amount": "-100"},
        {"direction": "DEBIT", "amount": "200"},
        {"direction": "CREDIT", "amount": "100"},
    ]
    assert [str(x) for x in side_totals(entries)] == ["200.00", "200.00"]


def test_合计口径_两张正负相抵的凭证不许存(db, book, admin_user, subjects):
    """借100 / 借-100 / 贷50 / 贷-50 带符号是平的，但什么也没记 —— 挡掉。

    合计看着倒是 150 / 150，平得漂亮，可账上一笔都没动：这种凭证存进去，
    报表上的数会莫名多出一截，还查不出是哪张凭证干的。
    """
    rows = _entries(subjects,
                    [("100201", 100, "x"), ("100201", -100, "x")],
                    [("600101", 50, "x"), ("600101", -50, "x")])
    with pytest.raises(ValueError, match="不能是 0"):
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 3, 1),
            entries=rows,
        )
