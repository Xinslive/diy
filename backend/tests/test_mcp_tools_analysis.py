"""MCP 分析类工具（收支趋势 / 报表渲染）的测试。

**这一组测试要钉住什么。**
分析类工具的风险不在「跑不起来」，在**数字与界面对不上**。所以这里最重的
一条是**口径一致性**：同一本账、同一个年度、同一期，工具 8 的年度收入/费用
合计必须等于工具 9 渲染的利润表「本年累计」列 —— 两者都取自账簿引擎与报表
求值器，所以它们要是对不上，就说明其中一边自己写了一套聚合。

另外三条钉的是**最容易静默算错**的地方：

* **跨年**：本年累计每年 1 月归零，用它相减得到的是差值而不是本年发生额，
  而且**不报错**。2026 与 2027 都有数据时，两种算法给出的答案差得很远。
* **年度**：科目表每个会计年度各有一份（迁移 0032），2026 与 2027 的同一
  编码是两个 id。跨年 id 混用不会报错，只会静默返回空。
* **权限**：令牌绑定用户没有该账套授权时，必须是结构化 403，不是 500。

**怎么调工具。**
MCP 的工具是**同步函数**，装饰器（``base.mcp_tool``）负责开数据库会话、
兜异常、写读取审计。测试助手 ``tests/mcp_helpers.py`` 把「造一个真的
Context + 在 scope 上挂主体」这件事收成一处 —— 主体挂的位置与鉴权中间件
（``app/mcp/asgi.py``）**逐字一致**，所以工具取主体的路径和生产一样。
"""
from decimal import Decimal

import pytest

from tests.mcp_helpers import (call_tool, call_tool_expect_error, make_ctx,
                                make_principal)

#: 利润表里**收入方**的项目行（内置模板的固定行 → 项目名）。
#:
#: 用项目名而不是行号：行号会随模板改版变，项目名是这张表上真正认得的东西。
#: （内置模板的行定义见 ``app/seed/builtin_reports.py::INCOME_ROWS``。）
INCOME_ITEMS = ("一、营业收入", "加：投资收益", "加：营业外收入")

#: 利润表里**费用方**的项目行。
EXPENSE_ITEMS = ("减：营业成本", "税金及附加", "销售费用", "管理费用",
                 "财务费用", "减：营业外支出", "减：所得税费用")


# --------------------------------------------------------------------- 夹具


@pytest.fixture
def principal(db, admin_user):
    """一个对测试账套**有授权**的主体（账套夹具把管理员设成了 OWNER）。"""
    return make_principal(user_id=admin_user.id, username=admin_user.username)


@pytest.fixture
def outsider(db, plain_user):
    """一个**没有这个账套授权**的主体。

    ``plain_user`` 在库里没有 ``UserBook`` 行 —— 与管理员那个用户正好成对：
    只测「有权限时能查到」是不够的，必须测「没权限时查不到」。
    """
    return make_principal(user_id=plain_user.id, username=plain_user.username)


# --------------------------------------------------------------------- 小工具


def ytd_of(row: dict) -> Decimal:
    """一行里的「本年累计」那一格。"""
    for cell in row["cells"]:
        if cell["kind"] == "YTD_AMOUNT":
            return Decimal(cell["value"])
    raise AssertionError(f"这一行没有「本年累计」列：{row['label']!r}")


def income_statement_items(report: dict) -> tuple[Decimal, Decimal]:
    """从渲染出来的利润表里取出收入方 / 费用方的「本年累计」合计。"""
    rows = {r["label"].strip(): r for r in report["rows"]}
    income = sum((ytd_of(rows[name]) for name in INCOME_ITEMS), Decimal("0"))
    expense = sum((ytd_of(rows[name]) for name in EXPENSE_ITEMS), Decimal("0"))
    return income, expense


def trend_total(out: dict, field: str) -> Decimal:
    """趋势序列里某一列的全年合计。"""
    return sum((Decimal(s[field]) for s in out["series"]), Decimal("0"))


# --------------------------------------------------------------------- 工具 8


def test_收支趋势_按月给出收入费用与净额(db, book, make_voucher, principal):
    """正常路径：3 月一笔收入、一笔房租，逐月给出三个数。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    make_voucher("2026-03-09", dr=[("560207", "3200.00", "3月房租")],
                 cr=[("1002", "3200.00", "付房租")], posted=True)

    out = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)

    assert out["caliber"] == "report"
    assert out["year"] == 2026
    assert out["carry_forward_excluded"] is True
    assert len(out["series"]) == 12

    march = out["series"][2]
    assert march["period"] == "2026-03"
    assert march["income"] == "20000.00"
    assert march["expense"] == "3200.00"
    assert march["net"] == "16800.00"
    # 分类用中文类别名（agent 看中文更准）
    assert march["income_by_category"] == {"损益": "20000.00"}
    assert march["expense_by_category"]["损益"] == "3200.00"

    # 没有发生额的月份也要在序列里 —— agent 才知道「3 月之后没有收入」
    assert out["series"][0]["period"] == "2026-01"
    assert out["series"][0]["income"] == "0.00"
    assert out["series"][0]["net"] == "0.00"

    # 口径说明必须在（这是「agent 说对话」与「说错话」的分界线）
    assert any("排除结转凭证" in n for n in out["notes"]), out["notes"]


def test_年度收入费用合计等于利润表本年累计(db, book, make_voucher, principal):
    """**口径一致性（本项目最关键的一条）**。

    工具 8 的年度收入/费用合计，必须等于工具 9 渲染的利润表「本年累计」列。
    两者都取自账簿引擎与报表求值器；要是它们对不上，就是其中一边自己写了
    一套聚合 —— 那正是计划 §8.2 R6 点名的头号风险。
    """
    make_voucher("2026-02-10", dr=[("1002", "10000.00", "收款")],
                 cr=[("5001", "10000.00", "收入")], posted=True)
    make_voucher("2026-05-06", dr=[("560207", "3200.00", "房租")],
                 cr=[("1002", "3200.00", "付房租")], posted=True)
    make_voucher("2026-07-06", dr=[("560206", "500.00", "电费")],
                 cr=[("1002", "500.00", "付电费")], posted=True)

    trend = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    report = call_tool("render_report", principal, book_id=book.id,
                       template_code="INCOME_SHEET", period_code="2026-12")
    report_income, report_expense = income_statement_items(report)

    assert trend_total(trend, "income") == report_income == Decimal("10000.00")
    assert trend_total(trend, "expense") == report_expense == Decimal("3700.00")
    assert trend_total(trend, "net") == report_income - report_expense


def test_跨年_年度合计不是本年累计相减(db, book, make_voucher, principal):
    """本年累计每年 1 月归零 —— 相减会算错，而且**不报错**。

    2026 收 1000、2027 收 2000：

    * 逐期累加（正确）⇒ 2027 年收入 2000；
    * 本年累计相减（错）⇒ 2000 − 1000 = 1000；
    * 两年相加（更错）⇒ 3000。

    三种答案差得很远，而错的那些**看起来都像数**。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    make_voucher("2026-06-10", dr=[("1002", "1000.00", "收款")],
                 cr=[("5001", "1000.00", "收入")], posted=True)

    # 2027 要有自己的**期间**与**科目表**（科目表每个年度各有一份）
    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()

    make_voucher("2027-03-10", dr=[("1002", "2000.00", "收款")],
                 cr=[("5001", "2000.00", "收入")], posted=True)

    y2026 = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    y2027 = call_tool("income_expense_trend", principal, book_id=book.id, year=2027)

    assert trend_total(y2026, "income") == Decimal("1000.00")
    assert trend_total(y2027, "income") == Decimal("2000.00")
    # 2027 的月度序列加起来也必须是 2000（不是只有年度合计对）
    assert sum((Decimal(s["income"]) for s in y2027["series"]
                if s["period"].startswith("2027-")), Decimal("0")) == Decimal("2000.00")


def test_符号_收入费用为正_净额等于收入减费用(db, book, make_voucher, principal):
    """收入取正、费用取正，净额 = 收入 − 费用。"""
    make_voucher("2026-04-08", dr=[("1002", "5000.00", "收款")],
                 cr=[("5001", "5000.00", "收入")], posted=True)
    make_voucher("2026-04-09", dr=[("560207", "1200.00", "房租")],
                 cr=[("1002", "1200.00", "付房租")], posted=True)

    out = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    april = out["series"][3]
    assert april["income"] == "5000.00"
    assert april["expense"] == "1200.00"
    assert april["net"] == "3800.00"


def test_红字按引擎口径参与合计(db, book, make_voucher, principal):
    """红字（负数分录）照既有引擎口径参与合计，不另立一套。

    借方 -200 ⇒ 费用那边就是 -200（把之前多记的费用冲回来）。
    """
    make_voucher("2026-04-08", dr=[("560207", "1000.00", "房租")],
                 cr=[("1002", "1000.00", "付房租")], posted=True)
    make_voucher("2026-04-20", dr=[("560207", "-200.00", "红字冲销")],
                 cr=[("1002", "-200.00", "红字冲销")], posted=True)

    out = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    april = out["series"][3]
    assert april["expense"] == "800.00"
    assert april["net"] == "-800.00"


def test_不支持的粒度给出结构化错误(db, book, principal):
    """非法粒度要说清楚，不能默默按月算，也不能抛协议错。

    **粒度现在支持 month/quarter/year**（评估报告 P1-4 的验收写的是
    「若 quarter 应当支持，则实现它」—— 实现比只报错更有用，
    「今年各季度收入多少」是 agent 常问的问题）。所以这里验的是
    **真正非法的值**给的是参数错误，且**列出合法取值**。
    """
    failure = call_tool_expect_error("income_expense_trend", principal,
                                     book_id=book.id, year=2026,
                                     granularity="week")
    assert failure.code == "bad_request"
    assert "month" in failure.message
    assert "quarter" in failure.message
    assert "year" in failure.message


def test_同比_与上一年同期比(db, book, make_voucher, principal):
    """同比用**上一年自己那一年的科目表**与**区间逐期累加**算出来。

    上一年收入 1000、本年 1500 ⇒ +50.00%。费用那边上一年是 0 ⇒ 算不出，
    给 null 并在 notes 里说明，而不是给一个除以 0 的数字。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    make_voucher("2026-06-10", dr=[("1002", "1000.00", "收款")],
                 cr=[("5001", "1000.00", "收入")], posted=True)
    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()
    make_voucher("2027-06-10", dr=[("1002", "1500.00", "收款")],
                 cr=[("5001", "1500.00", "收入")], posted=True)

    out = call_tool("income_expense_trend", principal, book_id=book.id, year=2027)
    assert out["yoy"] is not None
    assert out["yoy"]["income_change_pct"] == "50.00"
    # 上一年费用为 0 ⇒ 同比算不出来，给 null 而不是 0 或异常
    assert out["yoy"]["expense_change_pct"] is None
    assert any("费用为 0" in n for n in out["notes"]), out["notes"]


def test_类别过滤只统计指定类别(db, book, make_voucher, principal):
    """``categories`` 收英文类别码与中文名；被筛掉的类别不参与合计。

    **但先看一条更重要的口径：成本类科目根本不参与损益汇总。**
    内置利润表覆盖的是 5001/5051/5111/5301（收入）与 5401/5402/5403/
    5601/5602/5603/5711/5801（费用）—— 全是损益类。生产成本（4001）**不在
    利润表的任何一行里**，把它算进费用，工具报的费用就会比利润表上的大，
    而这个错看着还挺合理。

    所以按「成本」过滤得到的是**全 0**，不是「500」—— 这是口径决定的，
    不是查错了。
    """
    make_voucher("2026-06-10", dr=[("4001", "500.00", "生产成本")],
                 cr=[("1002", "500.00", "付生产成本")], posted=True)

    every = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    june = every["series"][5]
    assert june["expense"] == "0.00", "成本类科目被算进费用了"
    assert "成本" not in june["expense_by_category"]

    only_profit = call_tool("income_expense_trend", principal, book_id=book.id,
                            year=2026, categories=["PROFIT"])
    june2 = only_profit["series"][5]
    assert "成本" not in june2["expense_by_category"]
    assert june2["expense"] == "0.00"

    # 中文名也认（agent 手里的类别词往往是中文）；成本类不参与 ⇒ 全 0
    only_cn = call_tool("income_expense_trend", principal, book_id=book.id,
                        year=2026, categories=["成本"])
    assert only_cn["series"][5]["expense"] == "0.00"


def test_没有该账套授权时给出forbidden(db, book, outsider):
    """令牌绑定用户没有这个账套 ⇒ 结构化 403，不是异常、不是 500。"""
    failure = call_tool_expect_error("income_expense_trend", outsider,
                                     book_id=book.id, year=2026)
    assert failure.code == "forbidden"
    assert "没有该账套的访问权限" in failure.message


# --------------------------------------------------------------------- 工具 9


def test_内置报表都能渲染(db, book, make_voucher, principal):
    """资产负债表与利润表都能渲染，且单元格级公式没有报错。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)

    for code, name in (("BALANCE_SHEET", "资产负债表"), ("INCOME_SHEET", "利润表")):
        out = call_tool("render_report", principal, book_id=book.id,
                        template_code=code, period_code="2026-03")
        assert out["template"]["code"] == code
        assert out["template"]["name"] == name
        assert out["template"]["builtin"] is True
        assert out["period"]["code"] == "2026-03"
        assert out["period"]["locked"] is False
        assert out["rows"], code
        assert out["errors"] == [], out["errors"]
        # 每个单元格都带列号、列名与列口径，agent 才分得清「本期」与「本年累计」
        for row in out["rows"]:
            for cell in row["cells"]:
                assert "col_no" in cell and "kind" in cell


def test_不存在的模板给出not_found(db, book, principal):
    failure = call_tool_expect_error("render_report", principal, book_id=book.id,
                                     template_code="NO_SUCH_REPORT")
    assert failure.code == "not_found"
    assert "NO_SUCH_REPORT" in failure.message


def test_不存在的期间给出结构化400(db, book, principal):
    """期间不存在 / 格式不对 ⇒ 结构化 400，不是 500。

    caliber 抛的是 ``McpPeriodError``，工具层要把它翻成 agent 能自我修正的
    结构化回执（文案由 caliber 给：「这个账套的最后一个会计期间是 …」）。
    """
    failure = call_tool_expect_error("render_report", principal, book_id=book.id,
                                     template_code="INCOME_SHEET",
                                     period_code="2027-01")
    assert failure.code == "bad_request"
    assert "2026-12" in failure.message

    bad_format = call_tool_expect_error("render_report", principal,
                                         book_id=book.id,
                                         template_code="INCOME_SHEET",
                                         period_code="2026/03")
    assert bad_format.code == "bad_request"
    assert "YYYY-MM" in bad_format.message


def test_账套自己的模板副本优先于内置基线(db, book, principal):
    """账套 fork 过模板之后，它的副本与内置基线**同一个 code** 两行。

    查模板时必须同时接受内置基线与该账套自己的，而且**账套自己的优先** ——
    它才是这个账套在界面上看到的那一份。
    """
    from app.models import ReportTemplate

    forked = ReportTemplate(book_id=book.id, code="INCOME_SHEET",
                             name="利润表（副本）", type="INCOME_SHEET",
                             is_builtin=False)
    db.add(forked)
    db.commit()

    out = call_tool("render_report", principal, book_id=book.id,
                    template_code="INCOME_SHEET")
    assert out["template"]["id"] == forked.id
    assert out["template"]["builtin"] is False


def test_不传期间时取当前在记的那一期(db, book, make_voucher, principal):
    make_voucher("2026-08-08", dr=[("1002", "100.00", "收款")],
                 cr=[("5001", "100.00", "收入")], posted=True)

    out = call_tool("render_report", principal, book_id=book.id,
                    template_code="INCOME_SHEET")
    assert out["period"]["code"] == "2026-08"


def test_没有该账套授权时渲染报表也是forbidden(db, book, outsider):
    failure = call_tool_expect_error("render_report", outsider, book_id=book.id,
                                     template_code="INCOME_SHEET")
    assert failure.code == "forbidden"
    assert "没有该账套的访问权限" in failure.message


def test_工具经MCP服务注册后可调用(db, book, make_voucher, principal):
    """走一遍 MCP SDK 的调用路径：注册 → 调用 → 结构化内容。

    直接调被装饰的函数是**绕过了 SDK 的**（没有参数校验、没有 ctx 注入、
    没有出参转换）。这一条把那条链也验掉。
    """
    import asyncio

    from app.mcp.server import mcp_server, register_tools

    register_tools(mcp_server)
    make_voucher("2026-03-08", dr=[("1002", "1000.00", "收款")],
                 cr=[("5001", "1000.00", "收入")], posted=True)

    result = asyncio.run(mcp_server.call_tool(
        "income_expense_trend",
        {"book_id": book.id, "year": 2026},
        context=make_ctx(principal),
    ))
    assert not result.is_error, result
    payload = result.structured_content
    assert payload["caliber"] == "report"
    assert payload["series"][2]["income"] == "1000.00"
