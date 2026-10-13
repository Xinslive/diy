"""会计期间：结转损益 / 结账 / 批量反结账 / 年度结转。

这一组对应用户明确点名的四个诉求，是整个系统最容易出错也最不能出错的地方：
    1. 月度结转 = 自动生成结转损益凭证
    2. 不审核凭证，只有 未记账 / 已记账 两态
    3. 要有便捷的批量反结账（改老凭证时用）
    4. 年度结转给同一个账套追加下一年度
所以这里每条用例都按「用户实际操作顺序」写，而不是按函数名排列。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accounting.organize import apply_organize
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
    post_vouchers(db, book.id, [
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


def test_重复结转不会翻倍而是删旧重建(db, book, periods, subjects, admin_user, make_voucher):
    """连点两次「结转损益」不能生成两张凭证 —— 本年利润会凭空翻倍。

    处理方式不是报错拦人，而是删掉上一轮自己生成的结转凭证、按最新余额重算。
    这是财务最常见的动作：结完发现金额不对，改完直接再点一次。
    """
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    first = carry_forward_profit(db, book.id, periods[1], admin_user)

    second = carry_forward_profit(db, book.id, periods[1], admin_user)

    assert second["removed"] == first["vouchers"]
    assert second["replaced"] is True
    # 全账套的结转凭证仍然只有本次生成的这一张，没有叠加
    assert db.scalar(
        select(func.count()).select_from(Voucher).where(
            Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    ) == len(second["vouchers"])


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


def test_重新结转删掉旧凭证再按最新余额生成(db, book, periods, subjects, admin_user, make_voucher):
    """财务改完凭证发现上次结转金额不对时，要能一键重结转，而不是自己去凭证列表删。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    first = carry_forward_profit(db, book.id, periods[1], admin_user)
    old_ids = set(db.scalars(select(Voucher.id).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all())

    # 补一笔新的收入（月初的），重结转后金额应按最新余额重算。
    # 这笔补在月初、凭证号却比月末的结转凭证还大 —— 时序不对，先别急着记账
    late = make_voucher("2026-01-20", dr=[("100201", 200, "y")], cr=[("600101", 200, "y")])
    with pytest.raises(ValueError, match="时序不对"):
        post_vouchers(db, book.id, [late.id], admin_user)
    db.rollback()
    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)
    post_vouchers(db, book.id, [late.id], admin_user)

    # 整理把时序排好了：结转凭证垫底，号也跟着挪了位
    now = sorted(v.voucher_no for v in db.scalars(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all())

    r = carry_forward_profit(db, book.id, periods[1], admin_user, replace=True)

    assert r["replaced"] is True
    assert r["removed"] == now                        # 旧凭证号被回报给前端做提示
    new_ids = set(db.scalars(select(Voucher.id).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all())
    assert new_ids and not (new_ids & old_ids)        # 旧行真的没了，不是被改标记

    # 金额按 300+200 重算，而不是沿用旧的 300
    income = dict((c, Decimal(a)) for c, _n, a in r["income"])
    assert income["600101"] == Decimal("500")


def test_重新结转不误伤同期手工凭证(db, book, periods, subjects, admin_user, make_voucher):
    """只有 source=CARRY_FORWARD 的凭证可以被删，同期手工凭证必须原样留着。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    manual = make_voucher("2026-01-11", dr=[("100201", 50, "手工")],
                          cr=[("600101", 50, "手工")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)

    carry_forward_profit(db, book.id, periods[1], admin_user, replace=True)

    still = db.get(Voucher, manual.id)
    assert still is not None, "手工凭证被误删了"


def test_重新结转留下审计日志(db, book, periods, subjects, admin_user, make_voucher):
    """凭证号可复用，删掉的结转凭证必须留快照，否则事后查无对证。"""
    from app.models import AuditLog

    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    first = carry_forward_profit(db, book.id, periods[1], admin_user)
    carry_forward_profit(db, book.id, periods[1], admin_user, replace=True)

    row = db.scalar(
        select(AuditLog)
        .where(AuditLog.entity == "voucher", AuditLog.action == "DELETE")
        .order_by(AuditLog.id.desc())
    )
    assert row is not None
    assert first["vouchers"][0] in row.detail
    assert row.before, "删除审计必须带凭证快照"


def test_重新结转后余额仍然结平(db, book, periods, subjects, admin_user, make_voucher):
    """重结转是一次完整替换：跑完之后损益科目照旧能结平，不能留下两张半吊子凭证。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    carry_forward_profit(db, book.id, periods[1], admin_user, replace=True)

    assert db.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")) == 1

    # 结转凭证默认未记账，账簿看不见；记账后损益才归零
    post_vouchers(db, book.id, [
        v.id for v in db.scalars(
            select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
        ).all()
    ], admin_user)

    assert bal(db, book, periods, subjects, "600101") == Decimal("0")
    assert bal(db, book, periods, subjects, "4103") == Decimal("-300")


def test_重新结转保留已记账状态(db, book, periods, subjects, admin_user, make_voucher):
    """旧凭证如果已记账，重结转生成的新凭证也应当是已记账的。

    否则用户明明已经记完账，点一次重结转，账簿上的数字会突然「消失」一部分。
    """
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    old = db.scalars(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all()
    post_vouchers(db, book.id, [v.id for v in old], admin_user)

    carry_forward_profit(db, book.id, periods[1], admin_user, replace=True)
    db.expire_all()

    new = db.scalars(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all()
    assert new and all(v.status == "POSTED" for v in new), "重结转后凭证掉回未记账了"
    assert bal(db, book, periods, subjects, "600101") == Decimal("0")


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

    post_vouchers(db, book.id, [v.id], admin_user)
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
    post_vouchers(db, book.id, [
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


# ----------------------------------------------------------------- 反结账 → 结转 → 结账


def test_反结账后能直接结转损益(db, book, periods, subjects, admin_user, make_voucher):
    """这一条钉的是「结转+结账不顺」那个真问题。

    批量反结账会把区间内的凭证全部退回**未记账**，而账簿默认口径只统计
    已记账凭证 —— 于是按已记账算出来的损益全是 0，结转损益回一句
    「本期损益类科目全部为 0，无需结转」，用户明明看到满屏的收入凭证却
    结不了转。结转要的是「本期实际发生额」，跟记账状态无关。
    """
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    assert db.get(Period, periods[1].id).locked is True

    batch_unclose(db, book.id, periods[1], admin_user)          # 凭证全部回到未记账

    r = carry_forward_profit(db, book.id, periods[1], admin_user)   # 不该再报「无需结转」
    assert r["replaced"] is True
    assert r["income"] == [("600101", subjects["600101"].name, "300.00")]

    close_period(db, periods[1], admin_user)                   # 结账跟着走通
    assert db.get(Period, periods[1].id).locked is True


def test_结账会把结转凭证一起自动记账(db, book, periods, subjects, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    assert db.scalars(select(Voucher.status).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all() == ["UNPOSTED"]

    close_period(db, periods[1], admin_user)
    assert db.scalars(select(Voucher.status).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all() == ["POSTED"]


def test_损益没结平就不许结账并说明先结转(db, book, periods, subjects, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    with pytest.raises(ValueError, match="还没做「结转损益」"):
        close_period(db, periods[1], admin_user)
    db.rollback()
    assert db.get(Period, periods[1].id).locked is False


# --------------------------------------------------------------------- 年度结转


def _close_year(db, book, periods, admin_user):
    """年度结转要求 12 期全部结账，逐月「有损益就结转 -> 结账」。"""
    for m in range(1, 13):
        if _has_pl(db, book, periods, None, m):
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_period(db, periods[m], admin_user)


def test_年度结转给同一账套追加下一年度(db, book, periods, admin_user, make_voucher):
    """年度是账套的属性：一个账套一个年度一个账套的做法已经取消。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    before = db.scalar(select(func.count()).select_from(Book))
    r = year_end_carry(db, book, admin_user)

    assert r["new_year"] == 2027
    assert r["book_id"] == book.id
    # 账套数量没变 —— 以前这里是 +1，多出一条「租金账套 XXXX 2027」
    assert db.scalar(select(func.count()).select_from(Book)) == before

    years = db.scalars(
        select(Period.year).where(Period.book_id == book.id)
        .group_by(Period.year).order_by(Period.year)).all()
    assert years == [2026, 2027]


def test_年度结转把本年期末写成下一年1月期初(db, book, periods, subjects, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user)

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id, Period.year == 2027,
                                          Period.month == 1))
    bank = sid(subjects, "100201")
    opening = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id, PeriodOpening.subject_id == bank))
    assert opening is not None and opening.amount == Decimal("1000")


def test_跨年后账簿连续且本年累计归零(db, book, periods, subjects, admin_user, make_voucher):
    """跨年最容易出的两个错：期初断档、本年累计变成两年累计。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user)

    from datetime import date
    jan27 = db.scalar(select(Period).where(Period.book_id == book.id, Period.year == 2027,
                                          Period.month == 1))
    feb27 = db.scalar(select(Period).where(Period.book_id == book.id, Period.year == 2027,
                                          Period.month == 2))
    dec26 = db.scalar(select(Period).where(Period.book_id == book.id, Period.year == 2026,
                                          Period.month == 12))
    bank, revenue = sid(subjects, "100201"), sid(subjects, "600101")

    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2027, 2, 10),
                   entries=[
                       {"subject_id": bank, "direction": "DEBIT", "amount": Decimal("50"), "summary": "n"},
                       {"subject_id": revenue, "direction": "CREDIT", "amount": Decimal("50"), "summary": "n"},
                   ])
    # 2027-02 要记账，先按规矩把 2027-01 结账（上一期封了账这一期才能记）
    jan27.locked = True
    db.commit()
    post_vouchers(db, book.id, [db.scalar(select(Voucher.id).where(
        Voucher.book_id == book.id).order_by(Voucher.id.desc()).limit(1))], admin_user)

    eng = LedgerEngine(db, book.id)
    # ord 跨年度连续：2027-01 紧接 2026-12
    assert jan27.ord == dec26.ord + 1
    # 期初断档是最致命的错：上一年期末必须等于下一年期初
    assert eng.figures(bank, jan27.ord).opening == eng.figures(bank, dec26.ord).closing == Decimal("1000")
    assert eng.figures(bank, feb27.ord).closing == Decimal("1050")
    # 本年累计只在 2027 年里数：不能把 2026 年那 1000 也算进来
    assert eng.figures(bank, jan27.ord).dr_cum == Decimal("0")
    assert eng.figures(bank, feb27.ord).dr_cum == Decimal("50")
    # 2026 年的本年累计仍然是 1000
    assert eng.figures(bank, dec26.ord).dr_cum == Decimal("1000")


def test_年度结转把本年利润转到未分配利润(db, book, periods, subjects, admin_user, make_voucher):
    """利润表算出来的钱，最终要落到 4104 未分配利润上。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user)

    retained = sid(subjects, "4104")
    jan27 = db.scalar(select(Period).where(Period.book_id == book.id, Period.year == 2027,
                                          Period.month == 1))
    opening = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id, PeriodOpening.subject_id == retained))
    assert opening is not None and opening.amount == Decimal("-1000")   # 贷方未分配利润


def test_下一年没记账时可以再结转刷新期初(db, book, periods, subjects, admin_user, make_voucher):
    """结完 2026 → 2027 之后，回头改了 2026 的数，还得能再结一次把期初带过来。

    「下一年 1 月已经有期初余额」不是拒绝重结的理由 —— 期初是快照，重写就重写。
    只要下一年还没记凭证，覆盖它不会破坏任何已记账的数据。
    """
    from app.accounting.periods import ensure_periods

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    ensure_periods(db, book, 2027)
    db.commit()

    year_end_carry(db, book, admin_user, 2026, 2027)

    # 回退重开 2026-01，再补一笔 2000（银行存款变成 3000），重新结账
    batch_unclose(db, book.id, periods[1], admin_user)
    # 日期取月末：本期已有结转凭证落在月末，凭证必须按日期递增
    make_voucher("2026-01-31", dr=[("100201", 2000, "补")], cr=[("600101", 2000, "补")],
                 posted=True)
    # 只有 2026-01 的损益变了（补了一笔收入），重结转它即可；
    # 其余月份原样结账。反结账/重结转会留下断号，结账前先排号。
    carry_forward_profit(db, book.id, periods[1], admin_user)
    apply_organize(db, book_id=book.id, user=admin_user)
    for m in range(1, 13):
        close_period(db, periods[m], admin_user)

    # 再结一次：刷新 2027 年 1 月的期初，而不是拒绝
    year_end_carry(db, book, admin_user, 2026, 2027)

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2027, Period.month == 1))
    bank = sid(subjects, "100201")
    # 旧快照被覆盖，不会叠成两份 1000 + 3000
    rows = db.scalars(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id, PeriodOpening.subject_id == bank)).all()
    assert len(rows) == 1
    assert rows[0].amount == Decimal("3000")


def test_下一年已记账时拒绝刷新期初(db, book, periods, subjects, admin_user, make_voucher):
    """下一年一旦记了凭证，它的期初就被凭证引用，不能再被悄悄改掉。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user)

    make_voucher("2027-01-10", dr=[("100201", 50, "n")], cr=[("600101", 50, "n")],
                 posted=True)

    with pytest.raises(ValueError, match="2027 年已经有 1 张凭证，不能再刷新期初余额"):
        year_end_carry(db, book, admin_user, 2026, 2027)
    db.rollback()


def test_已结出下一年度后不能再结一次(db, book, periods, admin_user, make_voucher):
    """结完 2026 之后，账套的「当前年度」就是 2027。

    再点年度结转应该针对 2027，而且得先结完 2027 的账 ——
    不能悄悄把 2027 直接跳到 2028。
    """
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user)

    with pytest.raises(ValueError, match="2027 年度结转前必须全部结账"):
        year_end_carry(db, book, admin_user)
    db.rollback()


def test_下一年空期间已预建时仍结转本年(db, book, periods, subjects, admin_user, make_voucher):
    """回归：账套提前铺好了下一年的 12 个空期间时，年度结转不能被它们挡住。

    账套会跨年前就把下一年开好（seed_demo 也这么建），所以 period 表里
    最后一年永远是「还没开始的空年度」。以前这里取 periods[-1].year 当本年，
    于是 2026 年 12 期全结完了，点「结转到 2027 年」却被告知
    「2027 年度结转前必须全部结账」—— 报错年度和用户点的年度对不上。
    """
    from app.accounting.periods import ensure_periods

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    ensure_periods(db, book, 2027)          # 预建下一年，账上还没有任何动静
    db.commit()

    # 站在 2026 年结转，2027 的 12 个空期间不该挡路
    r = year_end_carry(db, book, admin_user, 2026, 2027)

    assert r["new_year"] == 2027
    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                           Period.year == 2027, Period.month == 1))
    opening = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id,
        PeriodOpening.subject_id == sid(subjects, "100201")))
    assert opening is not None and opening.amount == Decimal("1000")


def test_年度结转拒绝账套里不存在的年度(db, book, periods, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    with pytest.raises(ValueError, match="本账套没有 2019 年的会计期间"):
        year_end_carry(db, book, admin_user, 2019)
    db.rollback()


def test_年度结转不接受跨过一年的目标(db, book, periods, admin_user, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    with pytest.raises(ValueError, match="只能结转到紧接的下一个年度"):
        year_end_carry(db, book, admin_user, new_year=2029)
    db.rollback()


# --------------------------------------------------------------------- HTTP 层
#
# 上面测的是 service 层。这里补两条真实请求：前端按钮就是靠这两个接口驱动的，
# 少测一层就可能「函数对了但页面点不动」。


def test_状态接口在未结转时返回空(client, auth, book, periods):
    r = client.get(f"/api/books/{book.id}/periods/{periods[1].id}/carry-forward", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"period": "2026-01", "has": False, "vouchers": [], "posted": False}


def test_重新结转走HTTP接口(client, auth, book, periods, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")], posted=True)
    first = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/carry-forward",
                        json={}, headers=auth)
    assert first.status_code == 200, first.text
    old = first.json()["vouchers"]

    st = client.get(f"/api/books/{book.id}/periods/{periods[1].id}/carry-forward", headers=auth).json()
    assert st["has"] is True and st["vouchers"] == old

    # 重复点同一个按钮不再报错：直接删旧重建
    rebuilt = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/carry-forward",
                          json={"replace": True}, headers=auth)
    assert rebuilt.status_code == 200, rebuilt.text
    body = rebuilt.json()
    assert body["replaced"] is True and body["removed"] == old
    assert body["vouchers"]                              # 生成了新的


def test_只读账套不能结转(client, db, book, periods, plain_user, plain_auth):
    """只读成员连「重新结转」都不该能点 —— 那会删凭证。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/carry-forward",
                    json={"replace": True}, headers=plain_auth)
    assert r.status_code == 403
    assert "只读" in r.json()["detail"]


# ---------------------------------------------- 删掉上月结转凭证后的连锁事故
#
# 用户 2026-10-11 实操踩到的：删掉 11 月的结转损益凭证 -> 在 12 月结转损益
# -> 结账 12 月。结果 11 月、12 月双双「结账成功」，而 11 月的收入跑到了
# 12 月的凭证里。两处失守：
#   a) close_period 只体检最后那一期，11 月被顺带锁进历史时压根没验；
#   b) carry_forward_profit 不问上一月结没结账，本年累计口径把 11 月的
#      收入一并扫进了 12 月的凭证。
#
# 注意：凭证一律建成「未记账」。make_voucher(posted=True) 会顺手把之前所有
# 期间标成已结账（那是它为「上期结账才能记账」做的测试专用变通），会把
# 这里要验的「未结账」状态提前抹掉。

def test_上月没结账就不许给本月结转损益(db, book, periods, subjects, admin_user, make_voucher):
    """上一个月没封口，这个月就别想结转 —— 断掉入口。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    make_voucher("2026-02-10", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])

    with pytest.raises(ValueError, match="2026-01 还没结账"):
        carry_forward_profit(db, book.id, periods[2], admin_user)
    db.rollback()

    assert not db.scalar(
        select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    )


def test_上月结账后本月就能结转损益(db, book, periods, subjects, admin_user, make_voucher):
    """守门不能把正常流程也堵死：上月结完，本月照常结转。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    make_voucher("2026-02-10", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])

    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)

    r = carry_forward_profit(db, book.id, periods[2], admin_user)
    assert r["period"] == "2026-02"


def test_删掉上月结转凭证后不许在本月结转(db, book, periods, subjects, admin_user, make_voucher):
    """复现用户那一步：11 月结转过又删掉，12 月结转必须被拒绝。

    没有这条，12 月那张凭证会把 11 月的收入按本年累计一起扫走。
    """
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    make_voucher("2026-02-10", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)

    # 反结账 1 月并删掉结转凭证 —— 用户就是这么干的
    batch_unclose(db, book.id, periods[1], admin_user, revert_posting=False)
    for v in db.scalars(
        select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    ).all():
        db.delete(v)
    db.commit()

    with pytest.raises(ValueError, match="2026-01 还没结账"):
        carry_forward_profit(db, book.id, periods[2], admin_user)
    db.rollback()

    # 关键：12 月一张结转凭证都不许有，11 月的收入还老老实实待在收入科目上
    assert not db.scalar(
        select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    )
    assert bal(db, book, periods, subjects, "600101", month=2) == Decimal("-100")


def test_上月没结转就不许结账_即使本月已结转(db, book, periods, subjects, admin_user, make_voucher):
    """结账要体检链上每一期，不只是最后那一期。

    这是事故的另一半：11 月的损益还挂着，却因为「只查 target」被 12 月的
    结账顺带锁进了历史。
    """
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    make_voucher("2026-02-10", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)

    # 反结账 1 月并删掉结转凭证：1 月那 100 又回到收入科目上
    batch_unclose(db, book.id, periods[1], admin_user, revert_posting=False)
    for v in db.scalars(
        select(Voucher).where(Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")
    ).all():
        db.delete(v)
    db.commit()

    # 手工造出「2 月已结转、但那张凭证把 1+2 月的 300 一起扫走」的账面状态
    eng = LedgerEngine(db, book.id, include_unposted=True)
    fmap = eng.compute([sid(subjects, "600101")], periods[2].ord)
    assert fmap[sid(subjects, "600101")].closing == Decimal("-300")

    with pytest.raises(ValueError) as ei:
        close_period(db, periods[2], admin_user)
    db.rollback()

    # 报错必须指名道姓说是哪个月，不能只说「有损益没结平」
    assert "2026-01" in str(ei.value)

    # 两个月都不能被锁住
    db.refresh(periods[1])
    db.refresh(periods[2])
    assert periods[1].locked is False
    assert periods[2].locked is False


def test_两个月都结转过就能一路结账(db, book, periods, subjects, admin_user, make_voucher):
    """守门不能误伤：各月都规规矩矩结转了，结账就该一路顺畅。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    make_voucher("2026-02-10", dr=[("100201", 200, "x")], cr=[("600101", 200, "x")])

    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    carry_forward_profit(db, book.id, periods[2], admin_user)
    r = close_period(db, periods[2], admin_user)

    assert r["period"] == "2026-02"
    db.refresh(periods[2])
    assert periods[2].locked is True


# ===================================================================== 乐观锁的 HTTP 收尾
# 审计 P1-19。服务层（``periods.assert_version``）由前一批做完，HTTP 层三件事：
#   1. ``GET /periods`` 要下发 ``version`` —— 不下发前端手里就没有可比对的数；
#   2. ``close`` / ``unclose`` 的入参要收 ``version``；
#   3. 版本冲突要回 **409**，而且那条 except 必须排在 ``except ValueError`` 前面
#      （``PeriodVersionConflict`` 继承 ValueError，写在后面会被 400 先接走）。
#
# 为什么必须是 409 而不是 400：400 的语义是「你参数写错了」，前端会让人去检查
# 表单；而这里的事实是「别人已经动过这个期间，你手里的状态过期了」。
# 答错状态码，用户就会反复重试一个永远错得一样的请求。


def _periods(client, auth, book):
    r = client.get(f"/api/books/{book.id}/periods", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_期间列表要下发version(client, auth, book):
    """前端要靠这个数判断「我看到的还是不是现在的状态」。"""
    for p in _periods(client, auth, book):
        assert isinstance(p.get("version"), int), \
            f"期间 {p['code']} 没有 version 字段：{p}"
        assert p["version"] >= 0


def test_带过期version结账得到409(client, auth, db, book, periods, subjects,
                                 admin_user, make_voucher):
    """陈旧的「结账」按钮不该把别人刚结过的账再结一遍。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    carry_forward_profit(db, book.id, periods[1], admin_user)
    db.commit()

    stale = [p for p in _periods(client, auth, book) if p["id"] == periods[1].id][0]
    stale_version = stale["version"] - 1
    # 期间本身也要能结（损益已结平），确保 409 的来源是版本而不是别的校验
    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/close",
                    headers=auth, json={"version": stale_version})
    assert r.status_code == 409, f"版本冲突得到了 {r.status_code}：{r.text}"
    assert "已被他人操作" in r.json()["detail"], r.text

    db.expire_all()
    assert db.get(Period, periods[1].id).locked is False, "冲突之后数据库被改了"


def test_带过期version反结账得到409(client, auth, db, book, periods, subjects,
                                   admin_user, make_voucher):
    """反结账尤其要挡：它会把区间内的凭证批量退回未记账。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    db.commit()

    cur = [p for p in _periods(client, auth, book) if p["id"] == periods[1].id][0]
    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/unclose",
                    headers=auth,
                    json={"revert_posting": True, "version": cur["version"] - 1})
    assert r.status_code == 409, f"版本冲突得到了 {r.status_code}：{r.text}"

    db.expire_all()
    assert db.get(Period, periods[1].id).locked is True, "冲突之后数据库被改了"


def test_带当前version可以正常结账(client, auth, db, book, periods, subjects,
                                   admin_user, make_voucher):
    """乐观锁不能变成「永远锁死」：版本对得上就该放行。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    carry_forward_profit(db, book.id, periods[1], admin_user)
    db.commit()

    cur = [p for p in _periods(client, auth, book) if p["id"] == periods[1].id][0]
    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/close",
                    headers=auth, json={"version": cur["version"]})
    assert r.status_code == 200, r.text


def test_不传version的老调用方不被拦(client, auth, db, book, periods, subjects,
                                     admin_user, make_voucher):
    """上线期前端可能还没升级：body 可选，不带版本 = 不比对，与从前完全一致。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    carry_forward_profit(db, book.id, periods[1], admin_user)
    db.commit()

    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/close", headers=auth)
    assert r.status_code == 200, r.text


# ===================================================================== 失败路径不许半提交
# 审计 P2-2：``close`` / ``unclose`` 原来只 raise HTTPException，缺 ``db.rollback()``，
# 靠 ``Session.close()`` 的隐式回滚兜底。同文件的 carry_forward / year_end 一直都有
# 那一行，只有这两条漏了 —— 隐式依赖在冲突与异常交错的路径上尤其危险。
#
# 这里钉的是**可观测的后果**：失败之后库里的期间状态一个都不能变，
# 而且下一次操作必须照常能跑（没有被毒死的会话）。原始的 400/409 文案
# 由上面两条用例钉住。


def test_结账被拒之后期间状态一个都没动(client, auth, db, book, periods, subjects,
                                        admin_user, make_voucher):
    """没结转损益时结账必然被拒（守门「损益没结平」）。这时不能有一期被锁上。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    db.commit()
    before = {p.id: (p.locked, p.version) for p in
              db.scalars(select(Period).where(Period.book_id == book.id)).all()}

    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/close", headers=auth)
    assert r.status_code == 400, r.text

    db.expire_all()
    after = {p.id: (p.locked, p.version) for p in
             db.scalars(select(Period).where(Period.book_id == book.id)).all()}
    assert after == before, f"结账失败却改了库：{before} → {after}"


def test_失败的结账之后还能正常结账(client, auth, db, book, periods, subjects,
                                   admin_user, make_voucher):
    """失败之后下一次操作必须照常 —— 会话被毒住的话这里就是 500。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    db.commit()
    assert client.post(f"/api/books/{book.id}/periods/{periods[1].id}/close",
                       headers=auth).status_code == 400

    carry_forward_profit(db, book.id, periods[1], admin_user)
    db.commit()
    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/close", headers=auth)
    assert r.status_code == 200, f"上一次失败把会话毒住了：{r.status_code} {r.text}"


def test_未结账时反结账被拒且状态不变(client, auth, db, book, periods):
    before = {p.id: (p.locked, p.version) for p in
              db.scalars(select(Period).where(Period.book_id == book.id)).all()}
    r = client.post(f"/api/books/{book.id}/periods/{periods[1].id}/unclose",
                    headers=auth, json={"revert_posting": True})
    assert r.status_code == 400, r.text

    db.expire_all()
    after = {p.id: (p.locked, p.version) for p in
             db.scalars(select(Period).where(Period.book_id == book.id)).all()}
    assert after == before, f"反结账失败却改了库：{before} → {after}"
