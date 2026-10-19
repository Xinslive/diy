"""内置报表模板（资产负债表 / 利润表）。

这些模板看起来是"数据"，其实是代码 —— 公式串写错一个字，
资产负债表就会悄无声息地少算一列，而报表本身照样渲染得出来。
所以这里钉的是**数字之间的勾稽关系**，不是公式的字符串。

**资产负债表是一行两栏。**
------------------------
模板按《小企业会计准则》印的会小企01表排版：左边资产、右边负债和所有者权益，
一行同时承载两个项目，8 列（C/D 属资产侧，G/H 属负债权益侧）。
因此**不能再用 ``{row["label"]: cells}`` 取值** —— 那张字典会把左右两半
撞进同一个 key。取值的唯一稳定坐标是「列号 + 那一半的项目名」，
见下面的 ``at()``。
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.accounting_helpers import close_any
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
    """利润表这类**单栏**表用：label -> [项目, 本期金额, 本年累计]。

    只有一行一个项目时才能这么建字典；资产负债表一行两个项目，不适用。
    """
    return {r["label"]: [c["value"] for c in r["cells"]] for r in data["rows"]}


# ------------------------------------------------------------- 资产负债表坐标
#
# 8 列 = 左（资产）1-4 + 右（负债和所有者权益）5-8。两侧镜像：
# 期末余额 / 年初余额各占两列，中间夹着 STATIC 与 LINE_NO ——
# 公式只认列号，所以两侧天然串不到一起去。

L_CLOSING, L_OPENING = 3, 4        # 资产侧：期末余额 / 年初余额
R_CLOSING, R_OPENING = 7, 8        # 负债和所有者权益侧：期末余额 / 年初余额

LEFT, RIGHT = "left", "right"


def _row_no(data, label, side=LEFT):
    """按**那一半**的项目名找行号。

    左半边的名字在 ``row["label"]``，右半边在 ``row["style"]["label_right"]`` ——
    表结构里没有右栏标题这个字段，它随行走 JSONB（见 builtin_reports.py）。
    """
    for r in data["rows"]:
        name = r["label"] if side == LEFT else (r.get("style") or {}).get("label_right", "")
        if name == label:
            return r["row_no"]
    raise AssertionError(f"报表里没有这个{'左' if side == LEFT else '右'}栏项目：{label!r}")


def at(data, col_no, label, side=LEFT):
    """(列号, 项目名) -> 那一格的原始值。

    行号由项目名反查，所以这条表改了行序也不会让用例指错地方；
    但列号写死是故意的 —— 口径的错位（期末/年初取串了）正是这里要抓的东西。
    """
    row = next(r for r in data["rows"] if r["row_no"] == _row_no(data, label, side))
    return next(c["value"] for c in row["cells"] if c["col_no"] == col_no)


def check_value(data, col_no=L_CLOSING):
    """平衡校验行的值。

    认 ``style["kind_left"] == "CHECK"`` 而不是项目名：那一行的 label 长得像
    一句提示语（「平衡校验（资产总计 − 负债和所有者权益（或股东权益）总计，应为 0）」），
    改了措辞就会让测试莫名其妙地红。

    注意是 ``kind_left`` 而不是行级的 ``style["kind"]``：同一行上左右两半
    可以不同性质（第 16 行左边是「流动资产合计」合计、右边是「递延收益」
    普通项目），所以行级的 kind 只是两半的默认值，真正生效的是分列的那两个。
    """
    row = next(r for r in data["rows"]
               if (r.get("style") or {}).get("kind_left") == "CHECK")
    return next(c["value"] for c in row["cells"] if c["col_no"] == col_no)


def num(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal("0")


# --------------------------------------------------------------------- 期间口径


def test_年初余额列必须取年初而不是本期(db, tpl):
    """回归：KIND_FOR 曾经对所有 kind 都写死 {{本期}}。

    后果是资产负债表的「年初余额」列显示成了当前月的期初
    （也就是上月底余额），跟上年年报的期末数对不上 ——
    而这恰恰是财务唯一会手工去核的那一列。

    双栏版式里「年初余额」有两列（第 4、8 列），两侧都要查：
    只查资产侧的话，右半边照抄 {{本期}} 也没人拦得住。
    """
    from app.models import ReportCol

    bs = tpl("BALANCE_SHEET")
    opening_cols = db.scalars(select(ReportCol.col_no).where(
        ReportCol.template_id == bs.id, ReportCol.kind == "BALANCE_OPENING"
    ).order_by(ReportCol.col_no)).all()
    assert opening_cols == [L_OPENING, R_OPENING], opening_cols

    opening = db.scalars(select(ReportCell).where(
        ReportCell.template_id == bs.id,
        ReportCell.col_no.in_((L_OPENING, R_OPENING)),
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
    # 货币资金 = 1001 + 1002 + 1012（资产侧第 4 列 = 年初余额）
    assert num(at(data, L_OPENING, "货币资金")) == Decimal("20000")
    assert num(at(data, L_OPENING, "应收账款")) == Decimal("0")
    # 右半边同样：未分配利润（第 8 列 = 负债权益侧的年初余额）
    assert num(at(data, R_OPENING, "未分配利润", RIGHT)) == Decimal("0")


def test_年初余额不随月份变化(db, book, periods, subjects, make_voucher, tpl):
    """同一个账套里，选 3 月和选 12 月，年初余额必须一模一样。"""
    from app.models import PeriodOpening

    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("20000")))
    db.commit()
    make_voucher("2026-05-10", dr=[("1001", 500, "x")], cr=[("5001", 500, "x")], posted=True)

    mar = render(db, book, periods[3], "BALANCE_SHEET")
    dec = render(db, book, periods[12], "BALANCE_SHEET")
    assert num(at(mar, L_OPENING, "货币资金")) == Decimal("20000")
    assert num(at(dec, L_OPENING, "货币资金")) == Decimal("20000")


# --------------------------------------------------------------------- 勾稽


def _make_year(db, book, periods, subjects, admin_user):
    """造一年业务：每月一笔收租 + 一笔费用。"""
    from app.accounting.periods import carry_forward_profit, close_period
    from app.accounting.vouchers import post_vouchers
    from app.models import Voucher, Period

    for m in range(1, 13):
        v = _mk(db, book, admin_user, subjects, m)
        post_vouchers(db, book.id, [v.id], admin_user)
        carry_forward_profit(db, book.id, periods[m], admin_user)
        post_vouchers(db, book.id, [
            x.id for x in db.scalars(select(Voucher).where(
                Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD",
                Voucher.period_id == periods[m].id)).all()
        ], admin_user)
        # 真实流程就是这样：当月记账 → 结转损益 → 结账，下个月才允许再记凭证。
        # 少了这一步，m+1 月会因为「上一期未结账」而记不了账。
        close_any(db, book, periods[m], admin_user)
    db.commit()


def _mk(db, book, admin_user, subjects, m):
    from datetime import date

    from app.accounting.vouchers import create_voucher

    return create_voucher(db, book_id=book.id, user=admin_user,
                          voucher_date=date(2026, m, 10),
                          entries=[
                              {"subject_id": subjects["1002"].id, "direction": "DEBIT",
                               "amount": "10000", "summary": "收租"},
                              {"subject_id": subjects["5001"].id, "direction": "CREDIT",
                               "amount": "10000", "summary": "收租"},
                          ])


def test_结转记账后资产负债表两端相等(db, book, periods, subjects, admin_user, tpl):
    """资产总计 == 负债和所有者权益总计。

    这是资产负债表的定义。差一分钱都说明公式链上有个科目取错了。

    双栏版式里这两头**在同一行**（第 32 行）—— 资产总计在左半边、
    负债和所有者权益（或股东权益）总计在右半边，但**列号不同**（C 与 G），
    所以仍得按列号分别取，这是最容易取错的地方。
    """
    _make_year(db, book, periods, subjects, admin_user)
    data = render(db, book, periods[12], "BALANCE_SHEET")

    assert num(at(data, L_CLOSING, "资产总计")) == \
        num(at(data, R_CLOSING, "负债和所有者权益（或股东权益）总计", RIGHT))
    # 平衡校验行（C33 = 资产总计 − 负债和所有者权益（或股东权益）总计）是同一件事的另一种写法：
    # 它不占法定行次、排在最后一行，必须照样是 0。
    assert num(check_value(data)) == Decimal("0"), check_value(data)


def test_年初余额列的合计必须取年初(db, book, periods, subjects, make_voucher, tpl):
    """合计行的**年初余额**列必须是「年初」的数，不能是期末数抄一遍。

    明细行是按列生成公式的（``科目(...,"期初余额","{{年初}}")``），年初列天然正确；
    合计行却是显式公式，只要里头的行引用写死了列字母，就会出事 ——
    写死 ``C16+C31`` 的话，第 4 列（年初余额）拿到的还是 C 列（期末余额）的数。

    期初 1001 = 20,000，3 月再收租 30,000：
    资产总计的**年初**必须是 20,000，**期末**才是 50,000。
    """
    from app.models import PeriodOpening

    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("20000")))
    db.commit()
    make_voucher("2026-03-10", dr=[("1001", 30000, "x")], cr=[("5001", 30000, "x")],
                 posted=True)

    data = render(db, book, periods[3], "BALANCE_SHEET")
    # 明细行的年初余额是对的 —— 科目口径跟着列走
    assert num(at(data, L_OPENING, "货币资金")) == Decimal("20000")
    assert num(at(data, L_CLOSING, "货币资金")) == Decimal("50000")
    # 合计行必须自己对上：年初 = 各明细年初之和，不是期末
    assert num(at(data, L_OPENING, "流动资产合计")) == Decimal("20000")
    assert num(at(data, L_OPENING, "资产总计")) == Decimal("20000")


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
    post_vouchers(db, book.id, [_mk(db, book, admin_user, subjects, 3).id], admin_user)
    data = render(db, book, periods[3], "BALANCE_SHEET")
    gap = num(at(data, L_CLOSING, "资产总计")) - \
        num(at(data, R_CLOSING, "负债和所有者权益（或股东权益）总计", RIGHT))
    assert gap == Decimal("10000"), "差额应当等于本期净利润 10000"
    assert num(check_value(data)) == Decimal("10000"), "平衡校验行应当显示同一个差额"

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
        Subject.book_id == book.id, Subject.code == "5001"))
    incl = LedgerEngine(db, book.id, exclude_carry_forward=False).figures(sid, periods[12].ord)
    assert incl.dr_cum - incl.cr_cum == Decimal("0"), "含结转凭证时收入应被冲平"


def test_存货扣减跌价准备(db, book, periods, subjects, admin_user, tpl, make_voucher):
    """存货 = 材料采购 + 在途物资 + 原材料 + 库存商品 + 委托加工物资。

    减项的机制本身没变（还在科目编码上打减号，见固定资产的累计折旧、
    生产性生物资产的累计折旧），但**存货这一行不再减 1471 跌价准备** ——
    换成用友 4-2-2 科目表之后表里就没有这个科目了。科目都不存在就谈不上
    减值准备，挂一个不存在的编码只会让这一行永远取不到数。
    """
    from app.models import PeriodOpening

    for code, amt in (("1401", "10000"), ("1402", "20000"), ("1403", "30000"),
                      ("1405", "100000"), ("1408", "40000")):
        db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                             subject_id=subjects[code].id, amount=Decimal(amt)))
    db.commit()

    data = render(db, book, periods[12], "BALANCE_SHEET")
    assert num(at(data, L_OPENING, "存货")) == Decimal("200000")


def test_固定资产扣累计折旧(db, book, periods, subjects, admin_user, tpl):
    """固定资产原价、减：累计折旧、账面价值是三行，账面价值 = 原价 − 折旧。

    以前是一行「固定资产（扣累计折旧）」，现在按会小企01表拆成第 20/21/22 行，
    第 22 行用 ``{c}20-{c}21`` 显式相减 —— 这条公式的 ``{c}`` 必须在
    年初余额列展开成 D20-D21，不能拿 C 列来减。
    """
    from app.models import PeriodOpening

    for code, amt in (("1601", "2000000"), ("1602", "-400000")):
        db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                             subject_id=subjects[code].id, amount=Decimal(amt)))
    db.commit()

    data = render(db, book, periods[12], "BALANCE_SHEET")
    assert num(at(data, L_OPENING, "固定资产原价")) == Decimal("2000000")
    assert num(at(data, L_OPENING, "减：累计折旧")) == Decimal("400000")
    assert num(at(data, L_OPENING, "固定资产账面价值")) == Decimal("1600000")


def test_报表求值不报错(db, book, periods, subjects, make_voucher, tpl):
    """模板改动后至少要能跑完，不抛异常。

    顺带钉住两张表的**形状**：资产负债表 8 列（一行两栏），
    利润表 3 列（一行一个项目）。形状变了却不出错，正是这套模板最危险的
    那种「悄无声息」—— 财务看到的是一张少了一栏的表，而不是一条报错。
    """
    make_voucher("2026-01-10", dr=[("1002", 100, "x")], cr=[("5001", 100, "x")], posted=True)
    expect_cols = {"BALANCE_SHEET": 8, "INCOME_SHEET": 3}
    expect_rows = {"BALANCE_SHEET": 33, "INCOME_SHEET": 13}
    for code in ("BALANCE_SHEET", "INCOME_SHEET"):
        data = render(db, book, periods[1], code)
        assert data["rows"]
        assert not data.get("errors"), data.get("errors")
        assert len(data["cols"]) == expect_cols[code], data["cols"]
        assert len(data["rows"]) == expect_rows[code], len(data["rows"])


def test_资产负债表的静态列与行次列没有公式(db, book, periods, subjects, make_voucher, tpl):
    """第 1/2/5/6 列（资产、行次、负债和所有者权益、行次）不是数值列。

    它们的内容在**行**上（label / style），不来自科目，所以播种时公式是空的、
    evaluate 给出 value=null。反过来说：这两列一旦挂上公式，
    行次里的「31」就会被当成金额显示在表上 —— 这条钉的是「空」。
    """
    from app.models import ReportCell

    tid = tpl("BALANCE_SHEET").id
    bad = db.scalars(select(ReportCell).where(
        ReportCell.template_id == tid,
        ReportCell.col_no.in_((1, 2, 5, 6)),
        ReportCell.formula != "",
    )).all()
    assert not bad, [(c.row_no, c.col_no, c.formula) for c in bad]

    data = render(db, book, periods[1], "BALANCE_SHEET")
    row2 = next(r for r in data["rows"] if r["row_no"] == 2)
    for col in (1, 2, 5, 6):
        cell = next(c for c in row2["cells"] if c["col_no"] == col)
        assert cell["value"] is None, cell
    # 行次存在 style 里，行次列拿到的仍然是 null
    assert row2["style"]["line_left"] == 1 and row2["style"]["line_right"] == 31


# --------------------------------------------------------------------- 跨年度


def test_跨年后年初余额等于上年期末(db, book, periods, subjects, admin_user, make_voucher):
    """年度结转之后，资产负债表下一年 1 月的「年初余额」必须等于上一年 12 月的「期末余额」。

    不相等意味着期初断档 —— 整条利润链会在跨年那一刻悄悄错掉，
    而且平时根本看不出来（每个年度单独看都平）。

    双栏表要**左右各取一个**：资产侧第 4 列对上第 3 列，
    负债权益侧第 8 列对上第 7 列。这里取的都是明细行 —— 明细行的公式是
    按列生成的（``科目(...,"期初余额","{{年初}}")``），年初列天然正确；
    合计行的年初列另有一条用例盯着（test_年初余额列的合计必须取年初）。

    顺带说明这条用例本身的成色：2027 年 1 月没有凭证，期末本来就等于年初，
    所以它守的是「结转链没断」，不是「两列真的算出了不同的数」。
    """
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry
    from app.models import Period

    make_voucher("2026-01-10", dr=[("1002", 1000, "x")], cr=[("5001", 1000, "x")], posted=True)
    for m in range(1, 13):
        has = db.scalar(select(Voucher.id).where(
            Voucher.book_id == book.id, Voucher.period_id == periods[m].id))
        if has:
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_any(db, book, periods[m], admin_user)
    year_end_carry(db, book, admin_user)

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                          Period.year == 2027, Period.month == 1))
    last = render(db, book, jan27, "BALANCE_SHEET")
    prev = render(db, book, periods[12], "BALANCE_SHEET")

    # (项目名, 所在半边, 年初列, 期末列)
    for label, side, opening_col, closing_col in (
        ("货币资金", LEFT, L_OPENING, L_CLOSING),
        ("实收资本（或股本）", RIGHT, R_OPENING, R_CLOSING),
    ):
        assert num(at(last, opening_col, label, side)) == \
            num(at(prev, closing_col, label, side)), label


def test_跨年后本年累计不带上一年(db, book, periods, subjects, admin_user, make_voucher):
    """`dr_cum` 是「本年累计」，每年 1 月必须归零。

    不归零的话 2027 年 1 月的利润表会把 2026 全年的收入也算进「本年金额」。
    """
    from app.accounting.periods import carry_forward_profit, close_period, year_end_carry
    from app.models import Period, Voucher

    make_voucher("2026-01-10", dr=[("1002", 1000, "x")], cr=[("5001", 1000, "x")], posted=True)
    for m in range(1, 13):
        if db.scalar(select(Voucher.id).where(
                Voucher.book_id == book.id, Voucher.period_id == periods[m].id)):
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_any(db, book, periods[m], admin_user)
    year_end_carry(db, book, admin_user)

    jan27 = db.scalar(select(Period).where(Period.book_id == book.id,
                                          Period.year == 2027, Period.month == 1))
    pf = cells(render(db, book, jan27, "INCOME_SHEET"))
    assert num(pf["一、营业收入"][0]) == Decimal("0")
    assert num(pf["三、利润总额"][0]) == Decimal("0")
