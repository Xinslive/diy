"""会计期间：结转损益 / 结账 / 批量反结账 / 年度结转。

这一组对应用户明确点名的四个诉求，是整个系统最容易出错也最不能出错的地方：
    1. 月度结转 = 自动生成结转损益凭证
    2. 不审核凭证，只有 未记账 / 已记账 两态
    3. 要有便捷的批量反结账（改老凭证时用）
    4. 年度结转生成新账套
所以这里每条用例都按「用户实际操作顺序」写，而不是按函数名排列。
"""
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accounting.periods import (
    batch_unclose,
    carry_forward_profit,
    close_period,
    preview_unclose,
    year_end_carry,
)
from app.accounting.vouchers import create_voucher, post_vouchers
from app.ledger.engine import LedgerEngine
from app.models import Book, Entry, Period, PeriodOpening, Subject, Voucher


def sid(subjects, code):
    return subjects[code].id


def bal(db, book, periods, subjects, code, month=1):
    """某科目在某期的期末余额（借正贷负）。"""
    eng = LedgerEngine(db, book.id)
    return eng.figures(sid(subjects, code), periods[month].ord).closing


# --------------------------------------------------------------------- 结转损益


def test_结转损益生成凭证并把损益科目清零(db, book, periods, subjects, admin_user, make_voucher):
    """收入类、费用类各生成一张凭证，结转后所有损益明细科目余额为 0。"""
    make_voucher("2026-01-10", dr=[("100201", 5000, "租金")], cr=[("600101", 5000, "租金")], posted=True)
    make_voucher("2026-01-15", dr=[("660201" if "660201" in subjects else "6602", 800, "水电")],
                 cr=[("100201", 800, "水电")], posted=True)

    r = carry_forward_profit(db, book.id, periods[1], admin_user)

    assert r["period"] == "2026-01"
    assert len(r["vouchers"]) == 2                 # 收入一张 + 费用一张
    assert r["income"] and r["income"][0][0] == "600101"
    assert r["expense"] and r["expense"][0][0] == "6602"

    # 结转凭证默认「未记账」，账簿默认口径看不见它 —— 记账后损益才归零
    assert bal(db, book, periods, subjects, "600101") == Decimal("-5000")
    post_vouchers(db, [
        v.id for v in db.scalars(
            select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
        ).all()
    ], admin_user)

    assert bal(db, book, periods, subjects, "600101") == Decimal("0")
    assert bal(db, book, periods, subjects, "6602") == Decimal("0")
    # 本年利润 = 收入 5000 - 费用 800 = 4200（贷方余额在借正贷负下是负数）
    assert bal(db, book, periods, subjects, "4103") == Decimal("-4200")


def test_结转凭证记在期末最后一天(db, book, periods, subjects, admin_user, make_voucher):
    """结转凭证的日期必须是当月最后一天，否则会漏进下个月的明细账。"""
    make_voucher("2026-02-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[2], admin_user)

    v = db.scalar(
        select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    )
    assert (v.voucher_date.year, v.voucher_date.month, v.voucher_date.day) == (2026, 2, 28)
    assert v.period_id == periods[2].id
    assert v.status == "UNPOSTED"          # 结转生成的凭证默认未记账，结账时会自动记账


def test_结转后结账成功(db, book, periods, subjects, admin_user, make_voucher):
    """完整闭环：录业务 -> 结转损益 -> 结账。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    r = close_period(db, periods[1], admin_user)

    assert periods[1].locked is True
    assert r["auto_posted"] == 1            # 结转凭证被自动记账
    db.refresh(periods[1])
    assert periods[1].locked is True
    assert periods[1].closed_by == admin_user.username


def test_损益未结平时拒绝结账(db, book, periods, subjects, admin_user, make_voucher):
    """没结转就结账必须拦下来，而且期间不能被锁住。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    with pytest.raises(ValueError, match="结转损益"):
        close_period(db, periods[1], admin_user)
    db.rollback()

    db.refresh(periods[1])
    assert periods[1].locked is False


def test_重复结转被拦住(db, book, periods, subjects, admin_user, make_voucher):
    """连点两次「结转损益」会生成两张凭证，本年利润直接翻倍 —— 必须挡住。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)

    with pytest.raises(ValueError, match="已执行过结转损益"):
        carry_forward_profit(db, book.id, periods[1], admin_user)
    db.rollback()

    assert db.scalar(
        select(func.count()).select_from(Voucher).where(
            Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    ) == 1


def test_删掉结转凭证后可以重新结转(db, book, periods, subjects, admin_user, make_voucher):
    """护栏不能把「改完凭证要重结转」这条路也堵死。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)

    from app.accounting.vouchers import delete_voucher
    cf = db.scalar(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD"))
    delete_voucher(db, cf, admin_user)

    r = carry_forward_profit(db, book.id, periods[1], admin_user)
    assert len(r["vouchers"]) == 1


def test_已结账期间不能结转(db, book, periods, subjects, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    db.refresh(periods[1])

    make_voucher  # noqa
    with pytest.raises(ValueError, match="已结账"):
        carry_forward_profit(db, book.id, periods[1], admin_user)
    db.rollback()


# --------------------------------------------------------------------- 结账 / 级联


def test_结账级联关闭之前所有期间(db, book, periods, admin_user):
    """不允许出现「3 月已结账、1 月还开着」—— 否则已结账期间的累计数会随上游改动漂移。"""
    r = close_period(db, periods[3], admin_user)

    assert r["period"] == "2026-03"
    assert r["auto_closed_periods"] == ["2026-01", "2026-02"]
    for m in (1, 2, 3):
        assert db.get(Period, periods[m].id).locked is True
    assert db.get(Period, periods[4].id).locked is False


def test_重复结账报错(db, book, periods, admin_user):
    close_period(db, periods[1], admin_user)
    db.refresh(periods[1])
    with pytest.raises(ValueError, match="已经结账"):
        close_period(db, periods[1], admin_user)
    db.rollback()


def test_结账期间禁止写入凭证(db, book, periods, subjects, admin_user, make_voucher):
    """写保护在数据库触发器里，不只是应用层判断。"""
    close_period(db, periods[1], admin_user)
    db.refresh(periods[1])
    with pytest.raises(Exception) as e:
        make_voucher("2026-01-20", dr=[("100201", 1, "x")], cr=[("600101", 1, "x")])
    assert "结账" in str(e.value)
    db.rollback()


# --------------------------------------------------------------------- 批量反结账


def _has_pl(db, book, periods, subjects, m):
    """该期有没有需要结转的损益余额（没业务数据时跳过结转）。"""
    leaves = db.scalars(
        select(Subject).where(
            Subject.book_id == book.id, Subject.carry_forward.is_(True), Subject.is_leaf.is_(True)
        )
    ).all()
    eng = LedgerEngine(db, book.id)
    for s in leaves:
        if s.code == "4103":
            continue
        if eng.figures(s.id, periods[m].ord).closing != 0:
            return True
    return False


def test_批量反结账一次解锁到最后一个已锁期间(db, book, periods, admin_user, make_voucher):
    """这是用户最看重的功能：从 1 月反结账，2、3 月跟着一起开。"""
    for m in (1, 2, 3):
        make_voucher(f"2026-{m:02d}-10",
                     dr=[("100201", 100 * m, "x")], cr=[("600101", 100 * m, "x")], posted=True)
        carry_forward_profit(db, book.id, periods[m], admin_user)
        close_period(db, periods[m], admin_user)
    for m in (1, 2, 3):
        db.refresh(periods[m])
        assert periods[m].locked is True

    r = batch_unclose(db, book.id, periods[1], admin_user)

    assert r["periods"] == ["2026-01", "2026-02", "2026-03"]
    assert r["unposted_vouchers"] == 6          # 3 张业务 + 3 张结转凭证
    for m in (1, 2, 3):
        assert db.get(Period, periods[m].id).locked is False
    assert db.scalar(
        select(Voucher).where(Voucher.book_id == book.id, Voucher.status == "POSTED")
    ) is None


def test_反结账预览(db, book, periods, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)

    p = preview_unclose(db, book.id, periods[1])
    assert p["from_period"] == "2026-01"
    assert p["periods"] == ["2026-01"]
    assert p["total_vouchers"] == 2
    assert p["will_unpost_vouchers"] == 2


def test_反结账后凭证回到未记账且可再记账(db, book, periods, admin_user, make_voucher):
    """账套解锁了凭证还标着已记账的话，默认口径下余额会凭空消失。"""
    v = make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)

    batch_unclose(db, book.id, periods[1], admin_user)
    db.refresh(v)
    assert v.status == "UNPOSTED"
    assert v.posted_at is None and v.posted_by is None

    post_vouchers(db, [v.id], admin_user)
    db.refresh(v)
    assert v.status == "POSTED"


def test_反结账可以只回退凭证不改状态(db, book, periods, admin_user, make_voucher):
    """revert_posting=False：期间开了但凭证保持已记账。"""
    v = make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    r = batch_unclose(db, book.id, periods[1], admin_user, revert_posting=False)

    db.refresh(v)
    assert v.status == "POSTED"
    assert r["unposted_vouchers"] == 0
    assert db.get(Period, periods[1].id).locked is False


def test_未结账时反结账直接报错(db, book, periods, admin_user):
    with pytest.raises(ValueError, match="无需反结账"):
        batch_unclose(db, book.id, periods[1], admin_user)
    db.rollback()


def test_反结账后修改旧凭证再重新结账(db, book, periods, subjects, admin_user, make_voucher):
    """用户的真实工作流：改以前月份的凭证 -> 重新结转 -> 重新结账，数字要跟着变。

    这里顺带钉住了「重新结转前要先删掉旧结转凭证」这条约定，
    否则旧凭证会把损益又冲一次。
    """
    from decimal import Decimal as D

    from app.accounting.vouchers import delete_voucher, update_voucher

    v = make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    before = bal(db, book, periods, subjects, "100201", 2)

    batch_unclose(db, book.id, periods[1], admin_user)

    # 1. 旧的结转凭证删掉（主键/凭证号会回收，符合「作废不留号」的约定）
    cf = db.scalar(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD"))
    delete_voucher(db, cf, admin_user)

    # 2. 改业务凭证：100 -> 250
    update_voucher(db, v, admin_user, {
        "entries": [
            {"subject_id": sid(subjects, "100201"), "direction": "DEBIT",
             "amount": D("250"), "summary": "改后"},
            {"subject_id": sid(subjects, "600101"), "direction": "CREDIT",
             "amount": D("250"), "summary": "改后"},
        ]
    })

    # 3. 重新记账（update 之后凭证回到未记账），再重新结转损益
    post_vouchers(db, [
        x.id for x in db.scalars(
            select(Voucher).where(Voucher.book_id == book.id, Voucher.period_id == periods[1].id)
        ).all()
    ], admin_user)
    carry_forward_profit(db, book.id, periods[1], admin_user)

    after = bal(db, book, periods, subjects, "100201", 2)
    assert after - before == D("150")

    # 4. 重新结账：新结转凭证被自动记账，本年利润跟着变成 250
    close_period(db, periods[1], admin_user)
    assert db.get(Period, periods[1].id).locked is True

    eng = LedgerEngine(db, book.id)
    profit = sid(subjects, "4103")
    assert eng.figures(profit, periods[1].ord).closing == D("-250")


# --------------------------------------------------------------------- 年度结转


def _close_year(db, book, periods, admin_user):
    """年度结转要求 12 期全部结账，逐月「有损益就结转 -> 结账」。"""
    for m in range(1, 13):
        if _has_pl(db, book, periods, None, m):
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_period(db, periods[m], admin_user)


def test_年度结转生成新账套并带入期初(db, book, periods, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    r = year_end_carry(db, book, admin_user)

    new_book = db.get(Book, r["new_book_id"])
    assert new_book.year == 2027
    assert r["new_year"] == 2027
    assert db.get(Book, book.id).status == "CLOSED"

    # 新账套第一期期初 = 旧账套期末
    first = db.scalar(
        select(Period).where(Period.book_id == new_book.id).order_by(Period.ord).limit(1)
    )
    bank_new = db.scalar(
        select(Subject).where(Subject.book_id == new_book.id, Subject.code == "100201")
    )
    opening = db.scalar(
        select(PeriodOpening).where(
            PeriodOpening.book_id == new_book.id,
            PeriodOpening.subject_id == bank_new.id,
        )
    )
    assert opening is not None and opening.amount == Decimal("1000")


def test_年度结转继承科目表与用户授权(db, book, periods, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    r = year_end_carry(db, book, admin_user)
    new_book_id = r["new_book_id"]

    old = db.scalars(select(Subject).where(Subject.book_id == book.id)).all()
    new = db.scalars(select(Subject).where(Subject.book_id == new_book_id)).all()
    assert len(old) == len(new)
    assert {s.code for s in old} == {s.code for s in new}

    from app.models import UserBook
    assert db.scalar(
        select(UserBook).where(UserBook.book_id == new_book_id, UserBook.user_id == admin_user.id)
    ) is not None


def test_年度结转把本年利润转到未分配利润(db, book, periods, admin_user, make_voucher):
    """利润表算出来的钱，最终要落到 4104 未分配利润上。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    r = year_end_carry(db, book, admin_user)
    new_book = db.get(Book, r["new_book_id"])

    retained = db.scalar(
        select(Subject).where(Subject.book_id == new_book.id, Subject.code == "4104")
    )
    first = db.scalar(
        select(Period).where(Period.book_id == new_book.id).order_by(Period.ord).limit(1)
    )
    opening = db.scalar(
        select(PeriodOpening).where(
            PeriodOpening.book_id == new_book.id, PeriodOpening.subject_id == retained.id
        )
    )
    assert opening is not None and opening.amount == Decimal("-1000")   # 贷方未分配利润


def test_新账套期初可被新凭证正确累加(db, book, periods, admin_user, make_voucher):
    """跨年度后，期初必须能被引擎读到并继续滚存。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    r = year_end_carry(db, book, admin_user)
    new_book = db.get(Book, r["new_book_id"])

    first = db.scalar(
        select(Period).where(Period.book_id == new_book.id).order_by(Period.ord).limit(1)
    )
    second = db.scalar(
        select(Period).where(Period.book_id == new_book.id, Period.ord == 2)
    )
    bank = db.scalar(select(Subject).where(Subject.book_id == new_book.id, Subject.code == "100201"))
    revenue = db.scalar(select(Subject).where(Subject.book_id == new_book.id, Subject.code == "600101"))

    from datetime import date
    create_voucher(db, book_id=new_book.id, user=admin_user,
                   voucher_date=date(2027, 2, 10),
                   entries=[
                       {"subject_id": bank.id, "direction": "DEBIT", "amount": Decimal("50"), "summary": "n"},
                       {"subject_id": revenue.id, "direction": "CREDIT", "amount": Decimal("50"), "summary": "n"},
                   ])
    post_vouchers(db, [db.scalar(select(Voucher.id).where(
        Voucher.book_id == new_book.id).order_by(Voucher.id.desc()).limit(1))], admin_user)

    eng = LedgerEngine(db, new_book.id)
    assert eng.figures(bank.id, first.ord).closing == Decimal("1000")
    assert eng.figures(bank.id, second.ord).closing == Decimal("1050")
