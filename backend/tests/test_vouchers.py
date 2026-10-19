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
from tests.accounting_helpers import close_any


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
                       entries=_entries(subjects, [("1001", 100, "x")], [("5001", 99, "x")]),
                       commit=False)
    db.rollback()


def test_零金额凭证被拒(db, book, admin_user, subjects):
    """一条金额 0 的分录不是「记了 0 块」，是没填。负数（红字冲销）另算，见下。"""
    with pytest.raises(ValueError, match="不能是 0"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=_entries(subjects, [("1001", 0, "x")], [("5001", 0, "x")]),
                       commit=False)
    db.rollback()


def test_挂上级科目被拒(db, book, admin_user, subjects):
    """分录只能记在末级科目上：前端面板不放，服务端也得自己挡一道。

    前端能绕的路太多（接口直调、导入、老凭证本来就挂着），
    只在面板里不给选，等于没拦。这里的用例把绕过去之后的样子钉死。
    """
    parent = subjects["1101"]
    assert parent.is_leaf is False
    with pytest.raises(ValueError, match="不是末级科目"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=[
                           {"subject_id": parent.id, "direction": "DEBIT",
                            "amount": "10", "summary": "x"},
                           {"subject_id": sid(subjects, "5001"), "direction": "CREDIT",
                            "amount": "10", "summary": "x"},
                       ], commit=False)
    db.rollback()


def test_改凭证也挡住上级科目(db, book, admin_user, subjects, make_voucher):
    """改一张凭证时也不行 —— 新建拦住了，改却放过去，等于留了个后门。"""
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    with pytest.raises(ValueError, match="不是末级科目"):
        update_voucher(db, voucher=v, user=admin_user, payload={"entries": [
            {"subject_id": subjects["1101"].id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": sid(subjects, "5001"), "direction": "CREDIT",
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
        select(Subject.id).where(Subject.book_id == other.id, Subject.code == "1001")
    )
    with pytest.raises(ValueError, match="科目不存在或已停用"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2026, 1, 5),
                       entries=[
                           {"subject_id": other_bank, "direction": "DEBIT",
                            "amount": "10", "summary": "x"},
                           {"subject_id": sid(subjects, "5001"), "direction": "CREDIT",
                            "amount": "10", "summary": "x"},
                       ], commit=False)
    db.rollback()


def test_日期落在账套外报错(db, book, admin_user, subjects):
    with pytest.raises(ValueError, match="不在本账套的会计期间范围内"):
        create_voucher(db, book_id=book.id, user=admin_user,
                       voucher_date=date(2030, 1, 5),
                       entries=_entries(subjects, [("1001", 10, "x")], [("5001", 10, "x")]),
                       commit=False)
    db.rollback()


# --------------------------------------------------------------------- 编号


def test_凭证号期间内顺序编号(db, book, periods, admin_user, subjects, make_voucher):
    a = make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    b = make_voucher("2026-01-06", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    assert (a.voucher_no, b.voucher_no) == ("记0001", "记0002")


def test_不同期间各自从0001开始(db, book, periods, admin_user, subjects, make_voucher):
    a = make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    b = make_voucher("2026-02-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    assert a.voucher_no == "记0001" and b.voucher_no == "记0001"


def test_作废不留号_删除后序号复用(db, book, periods, admin_user, subjects, make_voucher):
    """用户明确要求：删掉 记0001 之后，新凭证就应该是 记0001。"""
    a = make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    b = make_voucher("2026-01-06", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    assert b.voucher_no == "记0002"

    delete_voucher(db, a, admin_user)
    c = make_voucher("2026-01-07", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])

    # 下一个号取「当前最大值 + 1」，所以删中间那张不会让后续号前移，
    # 但删掉最后一张后号一定会被复用。
    assert c.voucher_no == "记0003"
    delete_voucher(db, b, admin_user)
    delete_voucher(db, c, admin_user)
    d = make_voucher("2026-01-08", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    assert d.voucher_no == "记0001"


def test_凭证号预览(db, book, periods, admin_user, subjects, make_voucher):
    make_voucher("2026-03-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    make_voucher("2026-03-06", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    assert next_voucher_no(db, periods[3]) == "记0003"


# --------------------------------------------------------------------- 两态


def test_凭证只有未记账和已记账两态(db, book, make_voucher, admin_user):
    """不审核 —— 模型里根本没有 REVIEW/APPROVED 这一档。"""
    from app.models import Voucher as V
    cols = {c.name for c in V.__table__.columns}
    assert "reviewed" not in cols and "audit_status" not in cols
    assert {"status"} <= cols

    v = make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    assert v.status == "UNPOSTED"
    post_vouchers(db, book.id, [v.id], admin_user)
    db.refresh(v)
    assert v.status == "POSTED"
    assert v.posted_by is not None and v.posted_at is not None


def test_批量记账和反记账(db, book, make_voucher, admin_user):
    vs = [make_voucher(f"2026-01-0{i+5}", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
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
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")], posted=True)
    with pytest.raises(ValueError, match="已记账"):
        update_voucher(db, v, admin_user, {
            "entries": _entries(subjects, [("1001", 250, "改")], [("5001", 250, "改")])})
    db.rollback()
    db.refresh(v)
    assert v.status == "POSTED"

    # 反记账之后就能改了
    unpost_vouchers(db, book.id, [v.id], admin_user)
    db.refresh(v)
    assert v.status == "UNPOSTED"
    update_voucher(db, v, admin_user, {
        "entries": _entries(subjects, [("1001", 250, "改")], [("5001", 250, "改")])})
    db.refresh(v)
    assert v.status == "UNPOSTED"


def test_改凭证换月后分录期间同步(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-01-31", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    update_voucher(db, v, admin_user, {"voucher_date": date(2026, 3, 15)})
    db.refresh(v)
    assert v.period_id == periods[3].id
    assert all(e.period_ord == 3 for e in db.scalars(
        select(Entry).where(Entry.voucher_id == v.id)).all())


def test_删除凭证连同分录(db, book, make_voucher, admin_user):
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    vid = v.id
    delete_voucher(db, v, admin_user)
    assert db.get(Voucher, vid) is None
    assert db.scalars(select(Entry).where(Entry.voucher_id == vid)).all() == []


def test_已记账凭证不能删_先反记账才行(db, book, make_voucher, admin_user):
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
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

    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
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
    make_voucher("2026-01-05", dr=[("1001", 1, "租金")], cr=[("5001", 1, "租金")])
    make_voucher("2026-01-06", dr=[("1001", 1, "办公")], cr=[("560206", 1, "办公")])

    rows, total = query_vouchers(db, book.id, keyword="租金")
    assert total == 1
    rows, total = query_vouchers(db, book.id, keyword="办公")
    assert total == 1


def test_按科目筛选(db, book, subjects, make_voucher):
    make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    make_voucher("2026-01-06", dr=[("1122", 1, "y")], cr=[("5001", 1, "y")])

    rows, total = query_vouchers(db, book.id, subject_id=sid(subjects, "1122"))
    assert total == 1 and rows[0].voucher_date == date(2026, 1, 6)


def test_按期间区间筛选(db, book, periods, make_voucher):
    make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    make_voucher("2026-02-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    make_voucher("2026-03-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])

    rows, total = query_vouchers(db, book.id, from_period=periods[2], to_period=periods[3])
    assert total == 2
    rows, total = query_vouchers(db, book.id, from_period=periods[1])
    assert total == 3


def test_按状态和来源筛选(db, book, make_voucher):
    make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")], posted=True)
    make_voucher("2026-01-06", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    make_voucher("2026-01-07", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")],
                 source="CARRY_FORWARD")

    assert query_vouchers(db, book.id, status="POSTED")[1] == 1
    assert query_vouchers(db, book.id, status="UNPOSTED")[1] == 2
    assert query_vouchers(db, book.id, source="CARRY_FORWARD")[1] == 1


def test_按金额区间筛选(db, book, make_voucher):
    # 「金额」筛的是**某一条分录**的金额，不是整张凭证的合计。
    # 单借单贷时两条分录同额，看起来像合计，其实是一回事。
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])   # 每行 100
    make_voucher("2026-01-06", dr=[("1001", 500, "y")], cr=[("5001", 500, "y")])   # 每行 500
    make_voucher("2026-01-07", dr=[("1001", 800, "z")], cr=[("5001", 800, "z")])   # 每行 800

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
        dr=[("1001", 300, "a"), ("1001", 200, "b")],
        cr=[("5001", 400, "a"), ("222102", 100, "b")],
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
    v = make_voucher("2026-01-09", dr=[("1001", 800, "a")], cr=[("5001", 100, "a"), ("222102", 700, "b")])
    rows, hit = query_vouchers(db, book.id, min_amount=Decimal("790"))
    assert hit == 1 and rows[0].id == v.id


def test_分页(db, book, make_voucher):
    for i in range(7):
        make_voucher(f"2026-01-{i+3:02d}", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    rows, total = query_vouchers(db, book.id, page=1, page_size=3)
    assert total == 7 and len(rows) == 3
    rows, total = query_vouchers(db, book.id, page=3, page_size=3)
    assert len(rows) == 1


def test_默认按日期倒序(db, book, make_voucher):
    a = make_voucher("2026-01-05", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    b = make_voucher("2026-01-20", dr=[("1001", 1, "x")], cr=[("5001", 1, "x")])
    rows, _ = query_vouchers(db, book.id)
    assert [r.id for r in rows] == [b.id, a.id]


# --------------------------------------------------------------------- 记账门槛
# 业务规矩：只有上一期结账了，本期才允许记账。
# 上一期没封账，本期凭证再去改上期已经变动的数字，账就再也对不回来了。


def test_第一个期间可以记账_没有上一期可依(db, book, subjects, make_voucher, admin_user):
    """账套的第一个期间没有上一期，不该被这条规矩挡住。"""
    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    post_vouchers(db, book.id, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"


def test_上一期没结账不能记账(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-02-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    with pytest.raises(ValueError) as e:
        post_vouchers(db, book.id, [v.id], admin_user)
    msg = str(e.value)
    assert "2026-02" in msg and "2026-01" in msg, msg
    assert db.get(Voucher, v.id).status == "UNPOSTED", "被拦下来时不该留下已记账"


def test_上一期结账后本期就能记账(db, book, periods, subjects, make_voucher, admin_user):
    v = make_voucher("2026-02-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    periods[1].locked = True
    db.commit()

    post_vouchers(db, book.id, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"


def test_反结账上一期后本期又记不了账(db, book, periods, subjects, make_voucher, admin_user):
    """上一期一旦反结账，本期已记的凭证不受影响，但新的记账又被挡住。"""
    v = make_voucher("2026-02-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    periods[1].locked = True
    db.commit()
    post_vouchers(db, book.id, [v.id], admin_user)

    periods[1].locked = False
    db.commit()
    v2 = make_voucher("2026-02-11", dr=[("1001", 50, "x")], cr=[("5001", 50, "x")])
    with pytest.raises(ValueError):
        post_vouchers(db, book.id, [v2.id], admin_user)
    assert db.get(Voucher, v2.id).status == "UNPOSTED"


def test_批量记账时报出全部未结账的上一期(db, book, periods, subjects, make_voucher, admin_user):
    """一次批多个期间时，错误信息要把每一处该先结谁的账都说清楚。"""
    a = make_voucher("2026-02-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    periods[1].locked = True
    b = make_voucher("2026-03-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()

    with pytest.raises(ValueError) as e:
        post_vouchers(db, book.id, [a.id, b.id], admin_user)
    msg = str(e.value)
    assert "2026-03" in msg and "2026-02" in msg, msg
    assert db.get(Voucher, b.id).status == "UNPOSTED"


def test_跨年看的是上年十二月(db, book, admin_user, subjects, make_voucher):
    """2027-01 的上一期是 2026-12，靠 ord 跨年度连续，不能只看同年。"""
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry

    make_voucher("2026-01-10", dr=[("1001", 1000, "x")], cr=[("5001", 1000, "x")],
                 posted=True)
    db.commit()
    # 新规矩：上月没结账就不许给本月结转。所以先把 1 月结转结掉，
    # 再一路封到 11 月，12 月才轮到它结转。
    jan26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 1))
    nov26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 11))
    carry_forward_profit(db, book.id, jan26, admin_user)
    close_any(db, book, jan26, admin_user)
    close_any(db, book, nov26, admin_user)      # 级联封掉 2~11 月

    # 年度结转要求全年封账，先把 2026 整年结掉，再把 12 月退回来当「未结账」
    # 1 月的损益已经在 1 月结转走了，12 月账上没余额，不必再结转
    dec26 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2026, Period.month == 12))
    close_any(db, book, dec26, admin_user)
    year_end_carry(db, book, admin_user)
    dec26.locked = False
    db.commit()

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2027, Period.month == 1))
    v = make_voucher("2027-01-15", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
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
    v1 = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    v2 = make_voucher("2026-01-10", dr=[("1001", 200, "x")], cr=[("5001", 200, "x")])
    v3 = make_voucher("2026-01-20", dr=[("1001", 300, "x")], cr=[("5001", 300, "x")])

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
    a = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    b = make_voucher("2026-01-05", dr=[("1001", 200, "x")], cr=[("5001", 200, "x")])

    r = client.get(f"/api/books/{book.id}/vouchers/{b.id}/neighbour", headers=auth).json()
    assert (r["prev_id"], r["next_id"]) == (a.id, None)
    r2 = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour", headers=auth).json()
    assert (r2["prev_id"], r2["next_id"]) == (None, b.id)


def test_前后张不跨会计期间(client, auth, book, make_voucher):
    """1 月最后一张的后一张不该跳到 2 月 —— 跳过去日期会突然变掉。"""
    jan = make_voucher("2026-01-28", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    feb = make_voucher("2026-02-03", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    assert client.get(f"/api/books/{book.id}/vouchers/{jan.id}/neighbour", headers=auth).json()["next_id"] is None
    assert client.get(f"/api/books/{book.id}/vouchers/{feb.id}/neighbour", headers=auth).json()["prev_id"] is None


def test_带筛选条件时前后张翻的是结果集(client, auth, book, make_voucher):
    """用户举的例子：筛出「凭证号含 16」的（每月一张），进 8 月那张，
    前一张是 7 月那张，后一张从 7 月点回来还是 8 月。
    顺序是**时间正序**—— 跟列表那个倒序不是一个东西，混了会跳去 9 月。"""
    def mk(day, no):
        v = make_voucher(day, dr=[("1001", 100, no)], cr=[("5001", 100, no)])
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
    a = make_voucher("2026-05-31", dr=[("1001", 100, "16 a")], cr=[("5001", 100, "a")])
    b = make_voucher("2026-06-30", dr=[("1001", 100, "16 b")], cr=[("5001", 100, "b")])
    c = make_voucher("2026-07-31", dr=[("1001", 100, "16 c")], cr=[("5001", 100, "c")])

    first = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour?keyword=16", headers=auth).json()
    last = client.get(f"/api/books/{book.id}/vouchers/{c.id}/neighbour?keyword=16", headers=auth).json()
    assert (first["prev_id"], first["next_id"], first["index"], first["total"]) == (None, b.id, 0, 3)
    assert (last["prev_id"], last["next_id"], last["index"]) == (b.id, None, 2)


def test_这张不在结果集里就两头都没有(client, auth, book, make_voucher):
    """筛选条件刚改过、当前这张被筛掉了时，不该瞎指一张邻票。"""
    keep = make_voucher("2026-05-31", dr=[("1001", 100, "16")], cr=[("5001", 100, "x")])
    gone = make_voucher("2026-06-30", dr=[("1001", 100, "别的")], cr=[("5001", 100, "x")])
    r = client.get(f"/api/books/{book.id}/vouchers/{gone.id}/neighbour?keyword=16", headers=auth).json()
    assert r["prev_id"] is None and r["next_id"] is None and r["index"] == -1
    assert keep.id is not None


def test_金额筛选也参与前后张(client, auth, book, make_voucher):
    """分录金额区间同样带着翻 —— 跟列表筛出来的结果必须是同一个集合。"""
    small = make_voucher("2026-05-31", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    big = make_voucher("2026-06-30", dr=[("1001", 900000, "x")], cr=[("5001", 900000, "x")])

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
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    make_voucher("2026-01-28", dr=[("1001", 100, "y")], cr=[("5001", 100, "y")])
    p1 = book.periods[0]

    r = client.get(f"/api/books/{book.id}/vouchers/next", params={"period_id": p1.id}, headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["voucher_date"] == "2026-01-28"        # 不是今天，也不是 1 号
    assert d["period_id"] == p1.id

    # 号码跟真落库发出来的是同一个（预填要是跟实际对不上，就等于白填一次）
    made = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-28",
        "entries": _entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]),
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
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_no": v.voucher_no, "voucher_date": "2026-01-06",
        "entries": _entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]),
    })
    assert r.status_code == 400, r.text
    assert v.voucher_no in r.json()["detail"]      # 把撞上的号说清楚
    assert "唯一约束" not in r.json()["detail"]     # 而不是甩一个约束名出来


def test_别的账套的凭证查不到前后张(client, auth, db, book, make_voucher):
    from app.models import Book
    other = db.scalar(select(Book).where(Book.id != book.id).limit(1))
    if other is None:
        return
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    code = client.get(f"/api/books/{other.id}/vouchers/{v.id}/neighbour", headers=auth).status_code
    assert code in (403, 404), f"不该返回 {code}"


# ===================================================================== 插入凭证


def _stack(make_voucher, n=10, month=3):
    """连着造 n 张 2026-0X 的凭证，号自然是 记0001..记0010。"""
    out = []
    for i in range(n):
        v = make_voucher(f"2026-{month:02d}-{(i % 28) + 1:02d}",
                         dr=[("1001", 100, "收入")], cr=[("5001", 100, "收入")])
        out.append(v)
    return out


def test_插入凭证拿到参照的号_后面的整体顺位(db, book, periods, admin_user, subjects, make_voucher):
    """记0001..记0010，打开记0006插入 → 新凭证拿记0006，原记0006..记0010 变记0007..记0011。"""
    stack = _stack(make_voucher)
    anchor = stack[5]                      # 记0006

    new = create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 6),
        entries=_entries(subjects, [("1001", 50, "插入")], [("5001", 50, "插入")]),
        insert_after_id=anchor.id,
    )

    assert new.voucher_no == "记0006"
    db.refresh(anchor)
    assert anchor.voucher_no == "记0007"
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == [
        "记0001", "记0002", "记0003", "记0004", "记0005",
        "记0007", "记0008", "记0009", "记0010", "记0011",
    ]
    # 内容一条没动：顺位只改号，不碰分录
    assert db.get(Voucher, stack[0].id).entries[0].amount == Decimal("100.00")


def test_插入凭证顺延已经记账的号(db, book, admin_user, subjects, make_voucher):
    """已记账的凭证也要让位 —— 它只是换号，账里的数一个没动。"""
    first = make_voucher("2026-03-01", dr=[("1001", 100, "收入")],
                         cr=[("5001", 100, "收入")])
    anchor = make_voucher("2026-03-02", dr=[("1001", 100, "收入")],
                          cr=[("5001", 100, "收入")], posted=True)
    last = make_voucher("2026-03-03", dr=[("1001", 100, "收入")],
                        cr=[("5001", 100, "收入")])
    stack = [first, anchor, last]
    create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 2),
        entries=_entries(subjects, [("1001", 50, "插入")], [("5001", 50, "插入")]),
        insert_after_id=stack[1].id,
    )
    assert db.get(Voucher, stack[1].id).voucher_no == "记0003"
    assert db.get(Voucher, stack[2].id).voucher_no == "记0004"


def test_插入位是最后一张就只是接在末尾(db, book, admin_user, subjects, make_voucher):
    stack = _stack(make_voucher, n=3)
    new = create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 3),
        entries=_entries(subjects, [("1001", 50, "尾")], [("5001", 50, "尾")]),
        insert_after_id=stack[2].id,
    )
    assert new.voucher_no == "记0003"
    assert db.get(Voucher, stack[2].id).voucher_no == "记0004"


def test_插入凭证不建就不顺位(db, book, admin_user, subjects, make_voucher):
    """一个格子都没填就退出 = 什么都没发生：别人的号原封不动。

    顺位跟建凭证在同一个事务里，没有凭证就没有顺位 —— 这条是「插入」这件事
    唯一安全的前提，不能靠前端记得别提前改号。
    """
    stack = _stack(make_voucher, n=4)
    before = [db.get(Voucher, v.id).voucher_no for v in stack]
    slot = insert_slot(db, stack[1])
    assert slot["no"] == "记0002"           # 只算不改
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == before


def test_插入位不能跨会计期间(db, book, admin_user, subjects, make_voucher):
    jan = make_voucher("2026-01-05", dr=[("1001", 10, "a")], cr=[("5001", 10, "a")])
    mar = make_voucher("2026-03-05", dr=[("1001", 10, "a")], cr=[("5001", 10, "a")])
    with pytest.raises(ValueError, match="同一个会计期间"):
        create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=date(2026, 3, 6),
            entries=_entries(subjects, [("1001", 10, "b")], [("5001", 10, "b")]),
            insert_after_id=jan.id,
        )
    assert db.get(Voucher, mar.id).voucher_no == "记0001"


def test_手改了号就照人的号来_顺位照做(db, book, admin_user, subjects, make_voucher):
    """页面预填的是插入位，但人有权改 —— 顺位照做，占的是人填的号。"""
    stack = _stack(make_voucher, n=3)
    new = create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=date(2026, 3, 2),
        entries=_entries(subjects, [("1001", 50, "改号")], [("5001", 50, "改号")]),
        voucher_no="记0099", insert_after_id=stack[1].id,
    )
    assert new.voucher_no == "记0099"
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == ["记0001", "记0003", "记0004"]


def test_插入位撞上别人已占的号要报人话(db, book, admin_user, subjects, make_voucher):
    """顺位只顶开 anchor 之后的号；手填一个还在别人手上的号，照样要拦下来。"""
    stack = _stack(make_voucher, n=3)
    with pytest.raises(ValueError, match="记0001 在 2026-03 已经有一张了"):
        create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=date(2026, 3, 3),
            entries=_entries(subjects, [("1001", 50, "插")], [("5001", 50, "插")]),
            voucher_no="记0001", insert_after_id=stack[1].id,
        )
    # 撞了就整笔回滚：顺位也不能留下半个
    assert [db.get(Voucher, v.id).voucher_no for v in stack] == ["记0001", "记0002", "记0003"]


def test_插入预览只算不改(client, auth, book_id, admin_user, subjects, make_voucher):
    stack = _stack(make_voucher, n=6)
    r = client.get(f"/api/books/{book_id}/vouchers/insert-preview",
                   params={"after_id": stack[3].id}, headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["anchor_no"] == "记0004"
    assert data["voucher_no"] == "记0004"
    assert data["voucher_date"] == "2026-03-04"
    assert [m["from"] for m in data["moved"]] == ["记0004", "记0005", "记0006"]
    assert [m["to"] for m in data["moved"]] == ["记0005", "记0006", "记0007"]
    assert db_get_no(client, auth, book_id, stack[3].id) == "记0004"   # 没被改


def db_get_no(client, auth, book_id, voucher_id):
    r = client.get(f"/api/books/{book_id}/vouchers/{voucher_id}", headers=auth)
    return r.json()["voucher_no"]


def test_插入预览的参照不存在就404(client, auth, book_id):
    assert client.get(f"/api/books/{book_id}/vouchers/insert-preview",
                      params={"after_id": 999999}, headers=auth).status_code == 404


def test_走接口插入凭证号也是顺位的(client, auth, book_id, admin_user, subjects, make_voucher):
    stack = _stack(make_voucher, n=3)
    sid_d = subjects["1001"].id
    sid_c = subjects["5001"].id
    r = client.post(f"/api/books/{book_id}/vouchers", headers=auth, json={
        "voucher_date": "2026-03-02",
        "insert_after": stack[1].id,
        "entries": [
            {"subject_id": sid_d, "direction": "DEBIT", "amount": "10", "summary": "插"},
            {"subject_id": sid_c, "direction": "CREDIT", "amount": "10", "summary": "插"},
        ],
    })
    assert r.status_code == 201, r.text
    assert r.json()["voucher_no"] == "记0002"
    assert db_get_no(client, auth, book_id, stack[1].id) == "记0003"


# ===================================================================== 红字冲销


def test_红字凭证借贷两边都是负的也存得下(db, book, admin_user, subjects, make_voucher):
    """冲销老账就是这个形状：-300 / -300。它是平的，就该存。"""
    v = make_voucher("2026-03-01", dr=[("1001", 300, "收")], cr=[("5001", 300, "收")])
    red = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 3, 2),
        entries=_entries(subjects, [("1001", -300, "冲")], [("5001", -300, "冲")]),
    )
    assert [e.amount for e in red.entries] == [Decimal("-300.00"), Decimal("-300.00")]
    assert v.id != red.id


def test_金额为0的分录仍然被拒(db, book, admin_user, subjects):
    with pytest.raises(ValueError, match="不能是 0"):
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 3, 1),
            entries=_entries(subjects, [("1001", 0, "x")], [("5001", 0, "x")]),
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


def test_合计口径_正负相抵的凭证按平处理(db, book, admin_user, subjects):
    """借100 / 借-100 / 贷50 / 贷-50：合计 150 / 150，两栏一样大 —— 就是平的，该存。

    红字把负数搬去了对面，带符号那一对是 0/0，可人看着两栏相等就是平的
    （2026-10-12 用户明确要求这么判）。早先把这类凭证挡在门外，前端摆的红字
    跟「差额 0.00」自相矛盾，用户一看就知道是程序判错了。
    """
    rows = _entries(subjects,
                    [("1001", 100, "x"), ("1001", -100, "x")],
                    [("5001", 50, "x"), ("5001", -50, "x")])
    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 3, 1),
        entries=rows,
    )
    # 存下来了，四条分录原样保留（红字的负号没被吃掉）
    assert [str(e.amount) for e in v.entries] == \
        ["100.00", "-100.00", "50.00", "-50.00"]


# ===================================================================== 金额量级
# 审计 P1-21：``amount: 1e30`` 曾把整张凭证打成 HTTP 500。
#
# ``decimal.InvalidOperation`` 的祖先链是 InvalidOperation → DecimalException →
# **ArithmeticError** → Exception —— 它**不是 ValueError 的子类**。而这条链上
# 全是 ``except ValueError``，于是用户填错一个数，服务端回他一个「服务器错误」，
# 而且整张凭证的校验在半路中断。


@pytest.mark.parametrize("bad", [1e30, "1e30"])
def test_金额量级越界被拒_不是500(client, auth, book, subjects, bad):
    """Decimal 异常必须落在 400 上 —— 500 的含义是「服务端坏了」，
    而这里服务端好得很，是用户填了一个不可能的数。

    ``1e30`` 原来得到的是 500：``money()`` 抛 InvalidOperation，
    而路由只写 ``except ValueError``，InvalidOperation 不是它的子类。
    """
    entries = _entries(subjects, [("1001", bad, "x")], [("5001", 100, "x")])
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth,
                    json={"voucher_date": "2026-01-15", "entries": entries})
    assert r.status_code == 400, f"金额 {bad!r} 得到了 {r.status_code}：{r.text}"
    assert "金额" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_NaN与无穷大被拒_不是500(client, auth, book, subjects, bad):
    """这两种由 Pydantic 在请求体校验那一步就挡掉（422）。

    特别值得钉的一条：``Decimal('NaN').quantize(...)`` **不抛异常**，
    它返回的还是 NaN，而 NaN 的任何比较都为 False —— 也就是说
    ``dr != cr`` 永远不成立，一张「NaN 凭证」本来能一路存进库里。
    """
    entries = _entries(subjects, [("1001", bad, "x")], [("5001", 100, "x")])
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth,
                    json={"voucher_date": "2026-01-15", "entries": entries})
    assert r.status_code in (400, 422), f"金额 {bad!r} 得到了 {r.status_code}：{r.text}"


def test_金额量级越界时凭证没有落库(client, db, auth, book, subjects):
    """拒绝要干净：不能留下一张「半张凭证」。"""
    before = db.scalar(select(func.count()).select_from(Voucher))
    entries = _entries(subjects, [("1001", 1e30, "x")], [("5001", 1e30, "x")])
    client.post(f"/api/books/{book.id}/vouchers", headers=auth,
                json={"voucher_date": "2026-01-15", "entries": entries})
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Voucher)) == before


def test_正常的大额仍然能存(client, db, auth, book, subjects):
    """量级上限不能误伤：Numeric(20,2) 的上界之内照常落库。"""
    big = "9999999999999.99"
    entries = _entries(subjects, [("1001", big, "x")], [("5001", big, "x")])
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth,
                    json={"voucher_date": "2026-01-15", "entries": entries})
    assert r.status_code == 201, f"千万级金额被拒了：{r.status_code} {r.text}"
    assert r.json()["total_debit"] == big


# ===================================================================== 历史非末级分录
# 审计 P2-21。
#
# 报告原话是「is_leaf 服务端不读、是派生的」，**与实际不符**：服务端既算也强制
# （``_validate_entries`` 拒非末级），而 ``update_voucher`` 无条件调用它。
#
# 真实症状：一张历史上合法、后来其科目被加了子科目的老凭证（含非末级分录），
# 现在**连只改附单据数都存不下去** —— 400。前端松开硬拦没有用，症状在后端。


def _legacy_voucher_with_non_leaf(db, book, subjects, admin_user):
    """造一张「末级校验之前就存在」的老凭证：分录挂在非末级的 1101 上。

    直接写库，而不是走 ``create_voucher`` —— 走服务层现在会被拒（这是对的）。
    我们要的是「历史数据长这样」，不是「现在还能这么建」。
    """
    from app.accounting.util import find_period
    from app.models import Entry, Voucher

    p = find_period(db, book.id, date(2026, 1, 15))
    v = Voucher(book_id=book.id, period_id=p.id, voucher_no="记0001",
                voucher_date=date(2026, 1, 15), status="UNPOSTED", source="MANUAL",
                remark="老凭证", attachments=1, created_by=admin_user.username)
    db.add(v)
    db.flush()
    db.add(Entry(voucher_id=v.id, line_no=1, subject_id=subjects["1101"].id,
                 direction="DEBIT", amount=Decimal("100.00"), summary="历史"))
    db.add(Entry(voucher_id=v.id, line_no=2, subject_id=subjects["5001"].id,
                 direction="CREDIT", amount=Decimal("100.00"), summary="历史"))
    db.commit()
    db.expire_all()
    return db.get(Voucher, v.id)


def _put(client, auth, book, v, entries, **extra):
    body = {"voucher_date": "2026-01-15", "entries": entries}
    body.update(extra)
    return client.put(f"/api/books/{book.id}/vouchers/{v.id}", headers=auth, json=body)


def test_老凭证含非末级分录_只改附单据数能存下去(client, auth, db, book, subjects,
                                               admin_user):
    """症状本身：前端只改了「附单据数」，后端却因为另一条**没动的**分录拒了整张。"""
    v = _legacy_voucher_with_non_leaf(db, book, subjects, admin_user)
    entries = [
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "历史"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "100.00", "summary": "历史"},
    ]
    r = _put(client, auth, book, v, entries, attachments=7)
    assert r.status_code == 200, (
        f"只改附单据数却被拒 —— 这条分录根本没动：{r.status_code} {r.text}")
    assert r.json()["attachments"] == 7
    assert len(r.json()["entries"]) == 2, "分录不该被这条豁免顺手改掉"


def test_豁免走过的凭证在审计里留痕(client, auth, db, book, subjects, admin_user):
    """放行必须是**可追溯**的：事后有人问「这张怎么还能改」，答案在日志里。"""
    from app.models import AuditLog

    v = _legacy_voucher_with_non_leaf(db, book, subjects, admin_user)
    entries = [
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "历史"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "100.00", "summary": "历史"},
    ]
    _put(client, auth, book, v, entries, attachments=7)

    db.expire_all()
    # 按 id 倒序取最新一条：`.all()[-1]` 没有 ORDER BY 时拿到的是物理顺序里的
    # 最后一行，SELECT 不保证那是最新写入的那条（同 test_login_lockout 的坑）。
    row = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_id == str(v.id), AuditLog.action == "UPDATE")
        .order_by(AuditLog.id.desc())
    ).first()
    assert row is not None, "这次豁免修改没有留下审计"
    assert "非末级" in row.detail, f"豁免没有留痕：{row.detail}"


def test_新增一条非末级分录仍被拒(client, auth, db, book, subjects, admin_user):
    """口子只对「原样保留」开。**新增**挂在非末级科目上的分录照样 400 ——
    这是 fail-closed 的底线：历史不被锁死，新错也不许进。"""
    v = _legacy_voucher_with_non_leaf(db, book, subjects, admin_user)
    entries = [
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "历史"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "100.00", "summary": "历史"},
        # 老凭证里并没有这一条：它新指向了非末级的 1101
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "50.00", "summary": "新加的"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "50.00", "summary": "新加的"},
    ]
    r = _put(client, auth, book, v, entries, attachments=7)
    assert r.status_code == 400, f"新增非末级分录竟然存下去了：{r.status_code} {r.text}"
    assert "末级" in r.json()["detail"]


def test_改动历史非末级分录的金额仍被拒(client, auth, db, book, subjects, admin_user):
    """「原样保留」不是「随便改」：把老分录的金额从 100 改成 200 必须拒。

    走这条路的金额会顺着账簿流下去，而明细账根本不出这条分录 ——
    账面与账簿从这一刻起对不上，且没有任何提示。
    """
    v = _legacy_voucher_with_non_leaf(db, book, subjects, admin_user)
    entries = [
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "200.00", "summary": "历史"},       # 原来是 100
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "200.00", "summary": "历史"},
    ]
    r = _put(client, auth, book, v, entries)
    assert r.status_code == 400, f"改金额竟然存下去了：{r.status_code} {r.text}"


def test_复制一条历史非末级分录塞进去仍被拒(client, auth, db, book, subjects, admin_user):
    """计数不是布尔开关：删掉一条、再加两条一模一样的，第二条扣不掉。"""
    v = _legacy_voucher_with_non_leaf(db, book, subjects, admin_user)
    entries = [
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "历史"},
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "历史"},      # 复制的一条
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "200.00", "summary": "历史"},
    ]
    r = _put(client, auth, book, v, entries)
    assert r.status_code == 400, f"复制非末级分录竟然存下去了：{r.status_code} {r.text}"


def test_正常凭证仍然拒非末级(client, auth, db, book, subjects):
    """回归：口子没有顺带把闸门本身关掉。"""
    entries = [
        {"subject_id": subjects["1101"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "x"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "100.00", "summary": "x"},
    ]
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth,
                    json={"voucher_date": "2026-01-15", "entries": entries})
    assert r.status_code == 400, f"新凭证竟然能挂非末级科目：{r.status_code} {r.text}"


# ===================================================================== 列表页的往返次数
# 审计 P3-12：``query_vouchers`` 原来对每行 ``db.refresh``，一个 200 行的页
# 就是 200 次往返；而 ``refresh`` 刷的还是凭证自己那一行，分录仍在序列化时逐条
# 懒加载 —— 等于 200 次白跑 + 200 次懒加载，外加每张凭证查一次科目表。


def test_凭证列表一页只跑常数条SQL(client, auth, book, make_voucher, subjects):
    """计数的是**真正发出去的语句**。结果对而慢，用户一样会骂。"""
    import sqlalchemy

    from app.core.db import engine as app_engine

    for i in range(12):
        make_voucher(f"2026-01-{i + 1:02d}",
                     dr=[("1001", 100 + i, "x")], cr=[("5001", 100 + i, "x")])

    stmts: list[str] = []
    handler = lambda conn, cursor, statement, params, ctx, many: stmts.append(statement)
    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        r = client.get(f"/api/books/{book.id}/vouchers?page_size=200", headers=auth)
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)

    assert r.status_code == 200, r.text
    assert r.json()["total"] == 12
    # 科目一次 + 计数一次 + 本页凭证一次 + 分录一次 = 4。
    # 留一点余量是为了不把实现细节钉死（比如将来加一条无害的查询）。
    assert len(stmts) <= 8, (
        f"12 行的页发了 {len(stmts)} 条 SQL，逐行往返没去掉：{stmts}")


# ===================================================================== 撞号的数据库兜底
# 审计 P1-3。
#
# 上面那条「凭证号撞了报人话」走的是**应用层的占用检查**。但它和
# ``next_voucher_no`` 的「取全表求 max」都没有锁：两个人同时开一张，
# 两个事务会**双双查到「没被占」**。DB 侧有 ``uq_voucher_period_no`` 兜底，
# 所以不产生脏数据 —— 但撞出来的是一条裸的 ``IntegrityError``，
# 而 ``importer.commit`` 只 ``except ValueError``，于是批量导入变成 HTTP 500。
#
# 下面两条把「占用检查恰好没看见」这个窗口稳定复现出来：
# 把那一句检查短路掉，让数据库去拒。


def _blind_the_taken_check(db, monkeypatch):
    """让 ``create_voucher`` 里那句占用检查查不到东西（模拟并发窗口）。

    认的是语句本身（按 ``voucher_no`` **等值**过滤的那句），不是「第几次调用」
    ——调用序会随代码改动漂移，按次数打补丁的测试迟早测错东西。

    判据收紧到 ``"voucher_no = "``（带等号和空格）是有讲究的：发号那条语句里
    同样出现 ``voucher_no``，但它是 ``substring(voucher_no, ...)`` 这种「抠序号」
    的聚合，不是占用检查。认得太宽会把发号一起短路掉 —— 于是本该撞号的用例
    拿到一个更小的号、顺利建出第二张凭证，验不到它本来要验的 savepoint 恢复，
    反而变成一条永远绿着的假用例。
    """
    real_scalar = db.scalar

    def fake_scalar(stmt, *a, **kw):
        sql = str(stmt)
        if "voucher_no = " in sql:
            return None
        return real_scalar(stmt, *a, **kw)

    monkeypatch.setattr(db, "scalar", fake_scalar)


def test_并发撞号由数据库兜底_仍然报人话(db, book, admin_user, subjects,
                                       make_voucher, monkeypatch):
    """唯一约束兜底生效时，撞号必须落成 ValueError（人话），不是裸 IntegrityError。"""
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    _blind_the_taken_check(db, monkeypatch)

    with pytest.raises(ValueError) as exc:
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 6),
            entries=_entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]),
            voucher_no=v.voucher_no,
        )
    detail = str(exc.value)
    assert v.voucher_no in detail, f"没说清撞的是哪个号：{detail}"
    assert "唯一约束" not in detail, f"把数据库原话甩给了用户：{detail}"
    db.rollback()


def test_撞号之后同一个事务还能继续写(db, book, admin_user, subjects,
                                      make_voucher, monkeypatch):
    """撞号走 savepoint：回滚的只是这一条 insert，不是调用方整个事务。

    这正是批量导入依赖的性质 —— 前面几张已经写进去的凭证不该被
    「后面一张撞了号」一起废掉（``importer.commit`` 最后统一回滚）。
    """
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    _blind_the_taken_check(db, monkeypatch)

    with pytest.raises(ValueError):
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 6),
            entries=_entries(subjects, [("1001", 1, "x")], [("5001", 1, "x")]),
            voucher_no=v.voucher_no,
        )
    # 同一个会话、同一个事务，接着写第二张：必须还能成功
    ok = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 7),
        entries=_entries(subjects, [("1001", 2, "y")], [("5001", 2, "y")]),
    )
    assert ok.id
    db.commit()


# --------------------------------------------------------------------- 列表排序
#
# 四档：凭证号顺序 / 凭证号倒序 / 日期顺序 / 日期倒序，默认凭证号倒序
# （用户 2026-10-13 定的）。两条最容易写错的地方，各有钉子：
#   1. 凭证号**只在期间内唯一**，跨月必须先按期间排，否则记0001 与 记0012
#      被拿去比大小；
#   2. 号段要按**整数**比。直接排字符串时 "记0100" < "记0099"（字典序），
#      与 next_voucher_no 当年踩过的 max() 是同一个坑。


def _nos_in_order(rows):
    return [(v.voucher_date.isoformat(), v.voucher_no) for v in rows]


def test_排序_默认是凭证号倒序(db, book, periods, admin_user, subjects):
    for i in range(1, 4):
        create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, i),
                       entries=_entries(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    db.commit()

    rows, _total = query_vouchers(db, book.id)      # 不传 sort = 默认档
    assert [v.voucher_no for v in rows] == ["记0003", "记0002", "记0001"]


def test_排序_凭证号跨期间先按期间再按号段(db, book, periods, admin_user, subjects):
    """2 月一张记0001、1 月三张记0001..003。凭证号倒序该是「2月那张」在最前，
    因为它的期间更晚 —— 而不是被 1 月那张同号的记0001 顶掉。"""
    for i in range(1, 4):
        create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, i),
                       entries=_entries(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 2, 1),
                   entries=_entries(subjects, [("1001", 100, "y")], [("5001", 100, "y")]))
    db.commit()

    rows, total = query_vouchers(db, book.id, sort="no_desc")
    assert total == 4
    assert rows[0].voucher_date == date(2026, 2, 1)
    assert [(v.voucher_date.month, v.voucher_no) for v in rows] == [
        (2, "记0001"), (1, "记0003"), (1, "记0002"), (1, "记0001"),
    ]

    rows_asc, _ = query_vouchers(db, book.id, sort="no_asc")
    assert [(v.voucher_date.month, v.voucher_no) for v in rows_asc] == [
        (1, "记0001"), (1, "记0002"), (1, "记0003"), (2, "记0001"),
    ]


def test_排序_凭证号按整数比不是按字典序(db, book, periods, admin_user, subjects):
    """记099 与 记100：字典序下 "记100" < "记099"，整数序才是 099 < 100。"""
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 1),
                   voucher_no="记100",
                   entries=_entries(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 1),
                   voucher_no="记099",
                   entries=_entries(subjects, [("1001", 100, "y")], [("5001", 100, "y")]))
    db.commit()

    rows, _ = query_vouchers(db, book.id, sort="no_asc")
    assert [v.voucher_no for v in rows] == ["记099", "记100"]


def test_排序_日期两档与凭证号无关(db, book, periods, admin_user, subjects):
    """同一天两张、号段与日期相反：日期两档必须都按日期给，不受号段影响。"""
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 10),
                   voucher_no="记0001",
                   entries=_entries(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
                   voucher_no="记0002",
                   entries=_entries(subjects, [("1001", 100, "y")], [("5001", 100, "y")]))
    db.commit()

    asc, _ = query_vouchers(db, book.id, sort="date_asc")
    assert [v.voucher_no for v in asc] == ["记0002", "记0001"]   # 01-05 在前

    desc, _ = query_vouchers(db, book.id, sort="date_desc")
    assert [v.voucher_no for v in desc] == ["记0001", "记0002"]  # 01-10 在前


def test_排序_未知档位退回默认不报错(db, book, periods, admin_user, subjects):
    """排序是显示偏好，不是数据错误：认不出的档位退回默认，而不是 400。"""
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 1),
                   entries=_entries(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 2),
                   entries=_entries(subjects, [("1001", 100, "y")], [("5001", 100, "y")]))
    db.commit()

    rows, _ = query_vouchers(db, book.id, sort="随便写的")
    assert [v.voucher_no for v in rows] == ["记0002", "记0001"]


def test_排序_筛选与分页在四个档位下总数一致(db, book, periods, admin_user, subjects):
    """换排序只改顺序，不该改命中数 —— 排序若漏了 WHERE，总数就会变。"""
    for i in range(1, 7):
        create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, i),
                       entries=_entries(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    db.commit()

    counts = {s: query_vouchers(db, book.id, sort=s, page_size=200)[1]
              for s in ("no_asc", "no_desc", "date_asc", "date_desc")}
    assert set(counts.values()) == {6}
