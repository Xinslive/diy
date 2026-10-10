"""整理凭证：断号体检 / 重编号 / 按日期排序重排。

这个模块的价值在于「两阶段」：先 plan 预览差异，再 apply 落库。
财务改号直接影响已经打印出去的纸质凭证，必须能先看清楚再点确认。
"""
import pytest
from sqlalchemy import select

from app.accounting.organize import (
    apply_organize,
    check_gaps,
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
    assert split_no("记0001") == ("记", 1)
    assert split_no("记-0012") == ("记-", 12)
    assert split_no("银0007") == ("银", 7)
    assert split_no("手工") == ("手工", 0)      # 解析不了就原样保留


# --------------------------------------------------------------------- 体检


def test_连续编号没有断号(db, book, periods, subjects, admin_user):
    for i in range(1, 4):
        _mk(db, book, admin_user, subjects, f"2026-01-0{i}")
    r = check_gaps(db, periods[1])
    assert r["total"] == 3
    assert r["gaps"] == []
    assert r["clean"] is True


def test_删除中间一张会断号(db, book, periods, subjects, admin_user):
    """「作废不留号」的代价：号会留洞，所以需要整理功能来补。"""
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)

    r = check_gaps(db, periods[1])
    assert r["gaps"] == ["记0002"]
    assert r["clean"] is False


def test_体检不改数据(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)
    check_gaps(db, periods[1])
    assert _nos(db, book) == before


# --------------------------------------------------------------------- 计划


def test_GAPS只体检不改号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="GAPS")
    plan = plans[0]
    assert plan.gaps == ["记0002"]
    assert all(not c.changed for c in plan.changes)
    assert _nos(db, book) == before, "计划阶段绝对不能碰数据"


def test_RENUM补断号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    changes = {c.old_no: c.new_no for c in plans[0].changes if c.changed}
    assert changes == {"记0003": "记0002"}      # 记0001 原地不动，不算改动


def test_重排会交换号_两阶段必须生效(db, book, periods, subjects, admin_user):
    """记0001 与 记0002 互换编号，直接改会撞唯一约束。"""
    a = _mk(db, book, admin_user, subjects, "2026-01-20", no="记0001")
    b = _mk(db, book, admin_user, subjects, "2026-01-10", no="记0002")

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    changes = {c.voucher_id: (c.old_no, c.new_no) for c in plans[0].changes}
    assert changes[a.id] == ("记0001", "记0002")
    assert changes[b.id] == ("记0002", "记0001")

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")

    db.expire_all()
    assert db.get(Voucher, a.id).voucher_no == "记0002"
    assert db.get(Voucher, b.id).voucher_no == "记0001"


def test_倒序按日期排(db, book, periods, subjects, admin_user):
    a = _mk(db, book, admin_user, subjects, "2026-01-05")
    b = _mk(db, book, admin_user, subjects, "2026-01-25")
    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE_DESC")
    changes = {c.voucher_id: c.new_no for c in plans[0].changes}
    assert changes[b.id] == "记0001"
    assert changes[a.id] == "记0002"


def test_保留原来的凭证字(db, book, periods, subjects, admin_user):
    """多数凭证用「银」字，重排后不能全变成「记」。"""
    _mk(db, book, admin_user, subjects, "2026-01-05", no="银0001")
    _mk(db, book, admin_user, subjects, "2026-01-06", no="银0002")

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    assert [n for _i, n in _nos(db, book)] == ["银0001", "银0002"]


# --------------------------------------------------------------------- 护栏


def test_结转凭证不参与重编号(db, book, periods, subjects, admin_user):
    """结转凭证固定排在最后，重排不该动它。"""
    _mk(db, book, admin_user, subjects, "2026-01-05")
    cf = _mk(db, book, admin_user, subjects, "2026-01-31", no="记0002")
    cf.source = "CARRY_FORWARD"
    db.commit()

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id,
                          strategy="RENUM", include_system=False)
    targets = {c.voucher_id for c in plans[0].changes if c.changed}
    assert cf.id not in targets

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM", include_system=False)
    db.expire_all()
    assert db.get(Voucher, cf.id).voucher_no == "记0002"


def test_已结账期间不能整理(db, book, periods, subjects, admin_user):
    from app.accounting.periods import carry_forward_profit, close_period
    from app.accounting.vouchers import post_vouchers

    v = _mk(db, book, admin_user, subjects, "2026-01-05", no="记0009")
    post_vouchers(db, [v.id], admin_user)      # 结转损益只看已记账口径
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    db.refresh(periods[1])

    with pytest.raises(ValueError, match="已结账"):
        apply_organize(db, book_id=book.id, user=admin_user,
                       period_id=periods[1].id, strategy="RENUM")
    db.rollback()


def test_整账套整理按期间分组(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0005")
    _mk(db, book, admin_user, subjects, "2026-02-05", no="记0005")

    r = apply_organize(db, book_id=book.id, user=admin_user, strategy="RENUM")
    assert len(r["periods"]) == 12               # 12 个期间各一段
    assert all(p["period_id"] is not None for p in r["periods"])

    db.expire_all()
    nos = sorted(n for _i, n in _nos(db, book))
    assert nos == ["记0001", "记0001"]            # 各自期间内都压成 0001


def test_未知整理方式报错(db, book, admin_user):
    with pytest.raises(ValueError, match="未知的整理方式"):
        plan_organize(db, book_id=book.id, strategy="MAGIC")
    db.rollback()


def test_GAPS不能被当成执行(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-05")
    with pytest.raises(ValueError, match="只是体检"):
        apply_organize(db, book_id=book.id, user=admin_user,
                       period_id=periods[1].id, strategy="GAPS")
    db.rollback()


def test_整理后编号连续无断号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 5)]
    delete_voucher(db, vs[1], admin_user)
    delete_voucher(db, vs[3], admin_user)

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM")

    r = check_gaps(db, periods[1])
    assert r["clean"] is True
    assert sorted(n for _i, n in _nos(db, book)) == ["记0001", "记0002"]
