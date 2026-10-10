"""内置报表模板（资产负债表 / 利润表）。

这些模板看起来是"数据"，其实是代码 —— 公式串写错一个字，
资产负债表就会悄无声息地少算一列，而报表本身照样渲染得出来。
所以这里钉的是**数字之间的勾稽关系**，不是公式的字符串。
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.reports.evaluate import evaluate
from app.models import ReportCell, ReportTemplate, Subject, Voucher


@pytest.fixture
def tpl(db):
    def _get(code):
        return db.scalar(select(ReportTemplate).where(ReportTemplate.code == code))
    return _get


def render(db, book, period, code):
    t = db.scalar(select(ReportTemplate).where(ReportTemplate.code == code))
    return evaluate(db, t.id, book, period, False)


def cells(data):
    """label -> [本期/期末, 上期/年初]"""
    return {r["label"]: [c["value"] for c in r["cells"]] for r in data["rows"]}


def num(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal("0")


# --------------------------------------------------------------------- 期间口径


def test_年初余额列必须取年初而不是本期(db, tpl):
    """回归：KIND_FOR 曾经对所有 kind 都写死 {{本期}}。

    后果是资产负债表的「年初余额」列显示成了当前月的期初
    （也就是上月底余额），跟上年年报的期末数对不上 ——
    而这恰恰是财务唯一会手工去核的那一列。
    """
    opening = db.scalars(select(ReportCell).where(
        ReportCell.template_id == tpl("BALANCE_SHEET").id,
        ReportCell.col_no == 3,
        ReportCell.formula.like("%期初余额%"),
    )).all()
    assert opening, "资产负债表没有期初余额公式"
    for c in opening:
        assert "{{年初}}" in c.formula, c.formula
        assert "{{本期}}" not in c.formula, c.formula


def test_年初余额等于账套启用月期初(db, book, periods, subjects, make_voucher, tpl):
    """1001 期初录了 20,000，年初余额列就必须是 20,000。"""
    from app.models import PeriodOpening

    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("20000")))
    db.commit()

    data = render(db, book, periods[12], "BALANCE_SHEET")
    rows = cells(data)
    # 货币资金 = 1001 + 1002 + 1012
    assert num(rows["货币资金"][2]) == Decimal("20000")
    assert num(rows["应收账款"][2]) == Decimal("0")


def test_年初余额不随月份变化(db, book, periods, subjects, make_voucher, tpl):
    """同一个账套里，选 3 月和选 12 月，年初余额必须一模一样。"""
    from app.models import PeriodOpening

    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("20000")))
    db.commit()
    make_voucher("2026-05-10", dr=[("1001", 500, "x")], cr=[("600101", 500, "x")], posted=True)

    mar = cells(render(db, book, periods[3], "BALANCE_SHEET"))
    dec = cells(render(db, book, periods[12], "BALANCE_SHEET"))
    assert num(mar["货币资金"][2]) == num(dec["货币资金"][2]) == Decimal("20000")


# --------------------------------------------------------------------- 勾稽


def _make_year(db, book, periods, subjects, admin_user):
    """造一年业务：每月一笔收租 + 一笔费用。"""
    from app.accounting.periods import carry_forward_profit, close_period
    from app.accounting.vouchers import post_vouchers
    from app.models import Voucher, Period

    for m in range(1, 13):
        v = _mk(db, book, admin_user, subjects, m)
        post_vouchers(db, [v.id], admin_user)
        carry_forward_profit(db, book.id, periods[m], admin_user)
        post_vouchers(db, [
            x.id for x in db.scalars(select(Voucher).where(
                Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD",
                Voucher.period_id == periods[m].id)).all()
        ], admin_user)
        # 真实流程就是这样：当月记账 → 结转损益 → 结账，下个月才允许再记凭证。
        # 少了这一步，m+1 月会因为「上一期未结账」而记不了账。
        close_period(db, periods[m], admin_user)
    db.commit()


def _mk(db, book, admin_user, subjects, m):
    from datetime import date

    from app.accounting.vouchers import create_voucher

    return create_voucher(db, book_id=book.id, user=admin_user,
                          voucher_date=date(2026, m, 10),
                          entries=[
                              {"subject_id": subjects["100201"].id, "direction": "DEBIT",
                               "amount": "10000", "summary": "收租"},
                              {"subject_id": subjects["600101"].id, "direction": "CREDIT",
                               "amount": "10000", "summary": "收租"},
                          ])


def test_结转记账后资产负债表两端相等(db, book, periods, subjects, admin_user, tpl):
    """资产总计 == 负债和所有者权益总计。

    这是资产负债表的定义。差一分钱都说明公式链上有个科目取错了。
    """
    _make_year(db, book, periods, subjects, admin_user)
    rows = cells(render(db, book, periods[12], "BALANCE_SHEET"))

    assert num(rows["资产总计"][1]) == num(rows["负债和所有者权益总计"][1])
    assert num(rows["资产总计"][2]) == num(rows["负债和所有者权益总计"][2])


def test_未结转时差额正好是本期净利润(db, book, periods, subjects, admin_user, tpl):
    """结转前资产负债表不平是**对的**，差额必须恰好等于本期利润。

    这条同时反过来锁死了上一条：结转后差额归零，
    说明差额不是算错了，而是确实就是还没进本年利润的那部分损益。
    """
    from app.accounting.vouchers import post_vouchers
    from app.models import Period

    # 按规矩记账前得先封住前两个月；这个用例只关心第 3 个月「还没结转」的状态
    for p in db.scalars(
        select(Period).where(Period.book_id == book.id, Period.month.in_([1, 2]))
    ).all():
        p.locked = True
    db.commit()
    post_vouchers(db, [_mk(db, book, admin_user, subjects, 3).id], admin_user)
    rows = cells(render(db, book, periods[3], "BALANCE_SHEET"))
    gap = num(rows["资产总计"][1]) - num(rows["负债和所有者权益总计"][1])
    assert gap == Decimal("10000"), "差额应当等于本期净利润 10000"

    is_rows = cells(render(db, book, periods[3], "INCOME_SHEET"))
    assert num(is_rows["四、净利润"][1]) == Decimal("10000")


def test_利润表本年累计等于各月已结转加工期(db, book, periods, subjects, admin_user, tpl):
    _make_year(db, book, periods, subjects, admin_user)
    rows = cells(render(db, book, periods[12], "INCOME_SHEET"))
    # 下标 1 = 本期金额（12 月一张 = 10,000），下标 2 = 本年累计（12 张 = 120,000）
    assert num(rows["一、营业收入"][1]) == Decimal("10000")
    assert num(rows["一、营业收入"][2]) == Decimal("120000")
    assert num(rows["四、净利润"][2]) == Decimal("120000")


def test_利润表排除结转凭证(db, book, periods, subjects, admin_user, tpl):
    """月度结转之后，收入科目本期净额应该是 0；利润表靠 exclude_carry_forward
    才能继续显示真实经营收入。这是结转功能成立的前提。"""
    _make_year(db, book, periods, subjects, admin_user)
    rows = cells(render(db, book, periods[12], "INCOME_SHEET"))
    assert num(rows["一、营业收入"][2]) == Decimal("120000"), "结转凭证把收入冲掉了"
    # 反证：把结转凭证算进去，本年累计确实归零 —— 证明「排除」这件事是必需的
    from app.ledger.engine import LedgerEngine

    sid = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == "600101"))
    incl = LedgerEngine(db, book.id, exclude_carry_forward=False).figures(sid, periods[12].ord)
    assert incl.dr_cum - incl.cr_cum == Decimal("0"), "含结转凭证时收入应被冲平"


def test_存货扣减跌价准备(db, book, periods, subjects, admin_user, tpl, make_voucher):
    """模板里 1405 和 -1471 同在一个 sum 里，减项必须真的被减掉。"""
    from app.models import PeriodOpening

    for code, amt in (("1405", "100000"), ("1471", "-12000")):
        db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                             subject_id=subjects[code].id, amount=Decimal(amt)))
    db.commit()

    rows = cells(render(db, book, periods[12], "BALANCE_SHEET"))
    assert num(rows["存货（扣跌价准备）"][2]) == Decimal("88000")


def test_固定资产扣累计折旧(db, book, periods, subjects, admin_user, tpl):
    from app.models import PeriodOpening

    for code, amt in (("1601", "2000000"), ("1602", "-400000")):
        db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                             subject_id=subjects[code].id, amount=Decimal(amt)))
    db.commit()

    rows = cells(render(db, book, periods[12], "BALANCE_SHEET"))
    assert num(rows["固定资产（扣累计折旧）"][2]) == Decimal("1600000")


def test_报表求值不报错(db, book, periods, subjects, make_voucher, tpl):
    """模板改动后至少要能跑完，不抛异常。"""
    make_voucher("2026-01-10", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")], posted=True)
    for code in ("BALANCE_SHEET", "INCOME_SHEET"):
        data = render(db, book, periods[1], code)
        assert data["rows"]
        assert not data.get("errors"), data.get("errors")


# --------------------------------------------------------------------- 跨年度


def test_跨年后年初余额等于上年期末(db, book, periods, subjects, admin_user, make_voucher):
    """年度结转之后，资产负债表下一年 1 月的「年初余额」必须等于上一年 12 月的「期末余额」。

    不相等意味着期初断档 —— 整条利润链会在跨年那一刻悄悄错掉，
    而且平时根本看不出来（每个年度单独看都平）。
    """
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry
    from app.models import Period

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    for m in range(1, 13):
        has = db.scalar(select(Voucher.id).where(
            Voucher.book_id == book.id, Voucher.period_id == periods[m].id))
        if has:
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_period(db, periods[m], admin_user)
    year_end_carry(db, book, admin_user)

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                          Period.year == 2027, Period.month == 1))
    last = cells(render(db, book, jan27, "BALANCE_SHEET"))
    prev = cells(render(db, book, periods[12], "BALANCE_SHEET"))

    # 资产负债表每行的第 2 格是期末余额，第 3 格是年初余额
    for label in ("货币资金", "负债和所有者权益总计"):
        assert num(last[label][2]) == num(prev[label][1]), label


def test_跨年后本年累计不带上一年(db, book, periods, subjects, admin_user, make_voucher):
    """`dr_cum` 是「本年累计」，每年 1 月必须归零。

    不归零的话 2027 年 1 月的利润表会把 2026 全年的收入也算进「本年金额」。
    """
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry
    from app.models import Period, Voucher

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")], posted=True)
    for m in range(1, 13):
        if db.scalar(select(Voucher.id).where(
                Voucher.book_id == book.id, Voucher.period_id == periods[m].id)):
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_period(db, periods[m], admin_user)
    year_end_carry(db, book, admin_user)

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                          Period.year == 2027, Period.month == 1))
    pf = cells(render(db, book, jan27, "INCOME_SHEET"))
    assert num(pf["一、营业收入"][0]) == Decimal("0")
    assert num(pf["三、利润总额"][0]) == Decimal("0")
