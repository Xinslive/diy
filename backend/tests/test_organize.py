"""整理凭证：断号体检 / 重编号 / 按日期排序重排。

这个模块的价值在于「两阶段」：先 plan 预览差异，再 apply 落库。
财务改号直接影响已经打印出去的纸质凭证，必须能先看清楚再点确认。
"""
import pytest
from sqlalchemy import select

from app.accounting.organize import (
    apply_organize,
    check_period,
    plan_organize,
    split_no,
)
from app.accounting.vouchers import delete_voucher
from app.models import Voucher


def _mk(db, book, admin_user, subjects, date_, no=None, amount=10):
    from datetime import date as _d

    from app.accounting.vouchers import create_voucher
    d = _d.fromisoformat(date_) if isinstance(date_, str) else date_
    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=d,
        voucher_no=no,
        entries=[
            {"subject_id": subjects["100201"].id, "direction": "DEBIT",
             "amount": str(amount), "summary": "x"},
            {"subject_id": subjects["600101"].id, "direction": "CREDIT",
             "amount": str(amount), "summary": "x"},
        ],
    )
    return v


def _nos(db, book):
    return [(v.id, v.voucher_no) for v in db.scalars(
        select(Voucher).where(Voucher.book_id == book.id).order_by(Voucher.voucher_no)
    ).all()]


# --------------------------------------------------------------------- 解析


def test_拆凭证号():
    assert split_no("记001") == ("记", 1)
    assert split_no("记-0012") == ("记-", 12)
    assert split_no("银007") == ("银", 7)
    assert split_no("手工") == ("手工", 0)      # 解析不了就原样保留


# --------------------------------------------------------------------- 体检


def test_连续编号没有断号(db, book, periods, subjects, admin_user):
    for i in range(1, 4):
        _mk(db, book, admin_user, subjects, f"2026-01-0{i}")
    r = check_period(db, periods[1])
    assert r["total"] == 3
    assert r["gaps"] == []
    assert r["clean"] is True


def test_删除中间一张会断号(db, book, periods, subjects, admin_user):
    """「作废不留号」的代价：号会留洞，所以需要整理功能来补。"""
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)

    r = check_period(db, periods[1])
    assert r["gaps"] == ["记002"]
    assert r["clean"] is False


def test_体检不改数据(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)
    check_period(db, periods[1])
    assert _nos(db, book) == before


# --------------------------------------------------------------------- 计划


def test_体检只是查断号_不排号也不改号(db, book, periods, subjects, admin_user):
    """断号体检不再是整理方式了，它是列表页顶上的那句提醒，纯只读。"""
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)

    assert check_period(db, periods[1])["gaps"] == ["记002"]
    assert _nos(db, book) == before


def test_计划阶段绝对不碰数据(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    plan = plans[0]
    assert plan.gaps == ["记002"]
    assert any(c.changed for c in plan.changes)
    assert _nos(db, book) == before, "计划阶段绝对不能碰数据"


def test_RENUM补断号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    changes = {c.old_no: c.new_no for c in plans[0].changes if c.changed}
    assert changes == {"记003": "记002"}      # 记001 原地不动，不算改动


def test_重排会交换号_两阶段必须生效(db, book, periods, subjects, admin_user):
    """记001 与 记002 互换编号，直接改会撞唯一约束。"""
    a = _mk(db, book, admin_user, subjects, "2026-01-20", no="记001")
    b = _mk(db, book, admin_user, subjects, "2026-01-10", no="记002")

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    changes = {c.voucher_id: (c.old_no, c.new_no) for c in plans[0].changes}
    assert changes[a.id] == ("记001", "记002")
    assert changes[b.id] == ("记002", "记001")

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")

    db.expire_all()
    assert db.get(Voucher, a.id).voucher_no == "记002"
    assert db.get(Voucher, b.id).voucher_no == "记001"


def test_只有升序没有倒序(db, book, periods, subjects, admin_user):
    """倒序编号撤了：按日期倒着编号，纸质凭证翻出来永远是从后往前，不符合记账习惯。"""
    with pytest.raises(ValueError, match="未知的整理方式"):
        plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE_DESC")
    db.rollback()


def test_结转损益排在最后(db, book, periods, subjects, admin_user):
    a = _mk(db, book, admin_user, subjects, "2026-01-05", no="记001")
    cf1 = _mk(db, book, admin_user, subjects, "2026-01-31", no="记002")
    cf2 = _mk(db, book, admin_user, subjects, "2026-01-31", no="记003")
    for v in (cf1, cf2):
        v.source = "CARRY_FORWARD"
    db.commit()

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    new_no = {c.voucher_id: c.new_no for c in plans[0].changes}
    assert new_no[a.id] == "记001"
    assert {new_no[cf1.id], new_no[cf2.id]} == {"记002", "记003"}

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    db.expire_all()
    assert db.get(Voucher, cf1.id).voucher_no in ("记002", "记003")
    assert db.get(Voucher, cf2.id).voucher_no in ("记002", "记003")
    assert db.get(Voucher, a.id).voucher_no == "记001"


def test_保留原来的凭证字(db, book, periods, subjects, admin_user):
    """多数凭证用「银」字，重排后不能全变成「记」。"""
    _mk(db, book, admin_user, subjects, "2026-01-05", no="银001")
    _mk(db, book, admin_user, subjects, "2026-01-06", no="银002")

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    assert [n for _i, n in _nos(db, book)] == ["银001", "银002"]


# --------------------------------------------------------------------- 时序 / 闸门


def test_时序不对查得出来(db, book, periods, subjects, admin_user):
    """号大的那张日期反而更早 = 这个月是回头补记的，号段已经乱了。"""
    _mk(db, book, admin_user, subjects, "2026-01-25", no="记001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记002")

    rep = check_period(db, periods[1])
    assert len(rep["time_out_of_order"]) == 1
    assert rep["time_out_of_order"][0]["prev_no"] == "记001"
    assert rep["time_out_of_order"][0]["voucher_no"] == "记002"
    assert rep["clean"] is False
    assert any("时序不对" in b for b in rep["blocking"])


def test_同日多张不算时序不对(db, book, periods, subjects, admin_user):
    """同一天的两张凭证谁先谁后不算错 —— 日期相同，比不出先后。"""
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记002")
    assert check_period(db, periods[1])["clean"] is True


def test_时序不对时不能用当前顺序编号(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-25", no="记001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记002")

    with pytest.raises(ValueError, match="时序"):
        plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    db.rollback()
    # 换个方式就好：先按日期排好时序，正是这条规则存在的意义
    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    assert [c.new_no for c in plans[0].changes] == ["记001", "记002"]


def test_已记账的凭证不改号(db, book, periods, subjects, admin_user):
    """已记账的占着号原样不动，其余凭证只在空出来的号里重新发。"""
    from app.accounting.vouchers import post_vouchers

    a = _mk(db, book, admin_user, subjects, "2026-01-05")
    b = _mk(db, book, admin_user, subjects, "2026-01-06")
    c = _mk(db, book, admin_user, subjects, "2026-01-07")
    post_vouchers(db, book.id, [b.id], admin_user)
    db.expire_all()

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    new_no = {c_.voucher_id: c_.new_no for c_ in plans[0].changes}
    assert new_no[b.id] == b.voucher_no, "已记账那张号不能动"
    assert new_no[a.id] == "记001"
    assert new_no[c.id] == "记003"

    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)
    db.expire_all()
    assert db.get(Voucher, b.id).status == "POSTED"
    assert db.get(Voucher, b.id).voucher_no == b.voucher_no


def test_断号的月份不许记账(db, book, periods, subjects, admin_user):
    from app.accounting.vouchers import post_vouchers

    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)          # 留个洞

    with pytest.raises(ValueError, match="还没整理干净"):
        post_vouchers(db, book.id, [vs[0].id, vs[2].id], admin_user)
    db.rollback()

    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)
    post_vouchers(db, book.id, [v.id for v in (vs[0], vs[2])], admin_user)
    assert db.get(Voucher, vs[0].id).status == "POSTED"


def test_时序不对的月份不许结账(db, book, periods, subjects, admin_user):
    from app.accounting.periods import close_period

    a = _mk(db, book, admin_user, subjects, "2026-01-25", no="记001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记002")
    with pytest.raises(ValueError, match="时序不对"):
        close_period(db, periods[1], admin_user)
    db.rollback()


# --------------------------------------------------------------------- 护栏


def test_结转凭证垫底_录入单在前(db, book, periods, subjects, admin_user):
    """结转损益也参与重编号，但排序键里它排在最后 —— 所以永远拿到末尾的号。

    早先的做法是「结转凭证不参与重编号」，于是手工凭证压紧之后会踩到结转占着的号：
    三张手工凭证压成 001~003，而结转那张还占着 002，直接撞唯一约束。
    """
    m1 = _mk(db, book, admin_user, subjects, "2026-01-05", no="记001")
    m2 = _mk(db, book, admin_user, subjects, "2026-01-06", no="记003")
    cf = _mk(db, book, admin_user, subjects, "2026-01-31", no="记002")
    cf.source = "CARRY_FORWARD"
    db.commit()

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    db.expire_all()
    assert db.get(Voucher, m1.id).voucher_no == "记001"
    assert db.get(Voucher, m2.id).voucher_no == "记002"
    assert db.get(Voucher, cf.id).voucher_no == "记003"


def test_按当前顺序重排时结转也在最后(db, book, periods, subjects, admin_user):
    """RENUM 保持存储顺序，但结转键在最高位，所以照样垫底。"""
    cf = _mk(db, book, admin_user, subjects, "2026-01-10", no="记001")
    cf.source = "CARRY_FORWARD"
    m = _mk(db, book, admin_user, subjects, "2026-01-20", no="记002")
    db.commit()

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM")
    db.expire_all()
    assert db.get(Voucher, m.id).voucher_no == "记001"
    assert db.get(Voucher, cf.id).voucher_no == "记002"


def test_已结账期间不能整理(db, book, periods, subjects, admin_user):
    from app.accounting.periods import carry_forward_profit, close_period
    from app.accounting.vouchers import post_vouchers

    v = _mk(db, book, admin_user, subjects, "2026-01-05", no="记009")
    # 号段没整理干净就不许记账 —— 记账之前先把号排好，这是新的闸门
    with pytest.raises(ValueError, match="还没整理干净"):
        post_vouchers(db, book.id, [v.id], admin_user)
    db.rollback()
    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)

    post_vouchers(db, book.id, [v.id], admin_user)      # 结转损益只看已记账口径
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    db.refresh(periods[1])

    with pytest.raises(ValueError, match="已结账"):
        apply_organize(db, book_id=book.id, user=admin_user,
                       period_id=periods[1].id, strategy="RENUM")
    db.rollback()


def test_整账套整理按期间分组(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记005")
    _mk(db, book, admin_user, subjects, "2026-02-05", no="记005")

    r = apply_organize(db, book_id=book.id, user=admin_user, strategy="RENUM")
    assert len(r["periods"]) == 12               # 12 个期间各一段
    assert all(p["period_id"] is not None for p in r["periods"])

    db.expire_all()
    nos = sorted(n for _i, n in _nos(db, book))
    assert nos == ["记001", "记001"]            # 各自期间内都压成 0001


def test_未知整理方式报错(db, book, admin_user):
    with pytest.raises(ValueError, match="未知的整理方式"):
        plan_organize(db, book_id=book.id, strategy="MAGIC")
    db.rollback()


def test_断号检查不能被当成整理方式(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-05")
    with pytest.raises(ValueError, match="未知的整理方式"):
        apply_organize(db, book_id=book.id, user=admin_user,
                       period_id=periods[1].id, strategy="GAPS")
    db.rollback()


def test_整理后编号连续无断号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 5)]
    delete_voucher(db, vs[1], admin_user)
    delete_voucher(db, vs[3], admin_user)

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM")

    r = check_period(db, periods[1])
    assert r["clean"] is True
    assert sorted(n for _i, n in _nos(db, book)) == ["记001", "记002"]
