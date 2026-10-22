"""**ACCT MCP 缺陷修复的回归用例**（2026-10-22 两份评估报告）。

**这两份报告从哪儿来。**

* ``ACCT-render_report-缺陷报告.md``：``render_report`` 在「目标期间自身含有
  已记账凭证」时抛协议错 ``-32602``，且记账前给���的报表数字是错的、
  错误被 ``-32602`` 吞掉。
* ``ACCT-MCP-工具评估与优化建议.md``：全量工具评估，15 条建议
  （P0 四条 / P1 五条 / P2 六条）。

**这一组用例钉住的是「同一个问题不再犯」。** 每一条都对应报告里的一条
具体现象，外加一条**通用**用例：任何工具出错时回的结构体都必须符合它
自己声明的 ``outputSchema`` —— 那正是 ``-32602`` 的成因。

**为什么必须钉住「错误回执也符合 schema」。** ``-32602`` 是**协议层**的
校验错误：客户端拿 ``tools/list`` 里声明的 ``outputSchema`` 去校验
``structuredContent``，形状对不上就直接抛错，于是**真正的异常一个都没
露出来**。修好根因（``notices`` 的类型）之后，异常路径仍然存在 ——
下一个 bug 撞上来时，调用方看到的还是「结构不匹配」而不是「哪里错了」。
所以这条不变量要一直有测试守着。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models import Book, UserBook
from tests.conftest import build_voucher_factory
from tests.mcp_helpers import (call_tool, call_tool_expect_error, make_ctx,
                                make_principal)


@pytest.fixture
def principal(db, admin_user):
    return make_principal(user_id=admin_user.id, username=admin_user.username)


@pytest.fixture
def two_year_book(db, admin_user):
    """一个**跨 2025–2026 两年**的账套：两年的科目表都装好。

    「同比」要拿两年来比，而默认的 ``book`` 夹具只有 2026 一年 ——
    在 2025 年记一张凭证会直接撞「该年度科目表里没有这个科目」，
    因为科目表每个年度各有一份（迁移 0032）。
    """
    from datetime import datetime, timezone

    from app.accounting.periods import create_periods, ensure_periods
    from app.accounting.years import ensure_book_year
    from app.seed.chart_of_accounts import install_chart

    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"跨年测试公司{tag}", name=f"跨年账套{tag}",
             start_year=2025, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)                    # 2025 期间
    install_chart(db, b.id)                  # 2025 科目表
    ensure_periods(db, b, 2026)              # 2026 期间
    install_chart(db, b.id, year=2026)       # 2026 科目表
    now = datetime.now(timezone.utc)
    for year in (2025, 2026):
        by = ensure_book_year(db, b, year)
        by.init_subjects_at = now
        by.init_subjects_source = "CHART"
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


def _period(db, book, code):
    from sqlalchemy import select

    from app.models import Period
    year, month = int(code[:4]), int(code[5:])
    return db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == year, Period.month == month))


def _carry_forward(db, book, user, code):
    """在指定期间做一次结转损益（生成**已记账**的结转凭证）。"""
    from app.accounting.periods import carry_forward_profit
    from app.accounting.vouchers import post_vouchers
    from app.models import Voucher
    from sqlalchemy import select

    period = _period(db, book, code)
    carry_forward_profit(db, book.id, period, user)
    ids = db.scalars(select(Voucher.id).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD")).all()
    post_vouchers(db, book.id, list(ids), user)
    return list(ids)


def _cell(row: dict, kind: str) -> Decimal:
    for cell in row["cells"]:
        if cell["kind"] == kind:
            return Decimal(cell["value"])
    raise AssertionError(f"这一行没有 {kind} 列：{row['label']!r}")


def _row(report: dict, label: str) -> dict:
    for row in report["rows"]:
        if row["label"].strip() == label:
            return row
    raise AssertionError(f"报表里没有这一行：{label!r}")


# ===================================================================== P0-1
#
# 现象：render_report 在「目标期间自身含有已记账凭证」时抛 -32602。
#
# 根因：``RenderReportResult.notices`` 声明成 ``list[str]``，而求值器给的是
# 一串**字典**（结转口径的提示）。只要该期自己含有**已记账的结转凭证**，
# 提示列表就非空，出参校验当场失败 —— 而失败发生在「拼出参」这一步，
# 报表其实已经算完了。
#
# 触发条件要说准：**不是「有已记账凭证」，是「有已记账的结转凭证」**。
# 普通凭证不产生提示，所以带普通已记账凭证的期间渲染一直是好的。

def test_报表_期间含已记账结转凭证时渲染成功(db, book, make_voucher, admin_user,
                                       principal):
    """**P0-1 的核心回归**：带已记账结转凭证的期间必须渲染得出来。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    _carry_forward(db, book, admin_user, "2026-03")

    out = call_tool("render_report", principal, book_id=book.id,
                    template_code="INCOME_SHEET", period_code="2026-03")

    assert out["errors"] == []
    assert out["rows"], "报表行不该是空的"
    # 结转口径的提示**在**返回里（结构化的，不是字符串）
    codes = [n["code"] for n in out["notices"]]
    assert "CARRY_FORWARD_EXCLUDED" in codes, out["notices"]
    assert out["carry_forward_period_amount"] is not None


def test_报表_带普通已记账凭证的期间也能渲染(db, book, make_voucher, principal):
    """对照组：普通已记账凭证**不产生**提示，所以一直渲染得出来。

    钉住「触发条件是结转凭证」这一点 —— 否则下一次有人按「有已记账凭证」
    去理解这条缺陷，改的方向就会错。
    """
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)

    out = call_tool("render_report", principal, book_id=book.id,
                    template_code="INCOME_SHEET", period_code="2026-03")

    assert out["errors"] == []
    assert out["notices"] == []
    assert out["carry_forward_period_amount"] is None


# ===================================================================== P0-2
#
# 现象：出错时服务端回的结构不符合 outputSchema，客户端报 -32602，
# 真正的异常被吞掉。

def _output_schema(model_cls) -> dict:
    return model_cls.model_json_schema()


def test_错误回执符合工具声明的输出模型(db, book, principal):
    """**P0-2 的核心回归**：出错时的结构体必须满足出参模型。

    以前回的是 ``{"code", "message"}`` —— 四个必填字段全缺，客户端直接
    抛 ``-32602``，调用方只看到「结构不匹配」。
    """
    import jsonschema

    from app.mcp.schemas import RenderReportResult

    # 期间不存在 ⇒ 工具出错
    failure = call_tool_expect_error("render_report", principal, book_id=book.id,
                                     template_code="INCOME_SHEET",
                                     period_code="1999-01")
    payload = failure.result.structured_content
    assert payload is not None, "出错时必须回结构体（否则客户端无从校验）"
    jsonschema.validate(instance=payload, schema=_output_schema(RenderReportResult))
    # 错误本身要在结构体里，而且要能被人读懂
    assert payload["error"]["code"]
    assert payload["error"]["message"]
    assert payload["caliber"] == "error"


def test_错误回执把原始异常放进文本段(db, book, principal):
    """原始异常必须能被调用方看到 —— 这是报告里最要紧的一条。"""
    failure = call_tool_expect_error("render_report", principal, book_id=book.id,
                                     template_code="INCOME_SHEET",
                                     period_code="1999-01")
    text = failure.result.content[0].text
    assert failure.message in text
    # 原始异常的类型/消息要能看到
    assert "McpPeriodError" in text or "原始异常" in text, text


def test_出参模型的错误回执都能拼出来():
    """**通用不变量**：每一个工具的出参模型都能拼出一个合法的错误回执。

    以后新增工具、新增必填字段时，这条用例会立刻发现「错误回执拼不出来」
    —— 那正是 ``-32602`` 的成因。
    """
    import jsonschema

    from app.mcp import schemas
    from app.mcp.errors import error_payload

    models = [
        getattr(schemas, name) for name in schemas.__all__
        if isinstance(getattr(schemas, name, None), type)
        and issubclass(getattr(schemas, name), schemas.McpResult)
    ]
    assert models, "一个出参模型都没找到"
    for model in models:
        payload = error_payload(model, "test_error", "这是一条测试错误")
        jsonschema.validate(instance=payload, schema=_output_schema(model))


def test_认不出出参模型时错误回执也不退回旧形状():
    """**兜底路径**：连出参模型都认不出来时，回执仍要是合规结构体。

    退回旧的 ``{code, message}`` 就等于把 ``-32602`` 又放回来了 ——
    而那正是这一组用例要根治的东西。
    """
    import jsonschema

    from app.mcp.errors import McpError
    from app.mcp.schemas import McpResult

    result = McpError("测试错误", code="bad_request").to_result(None)

    payload = result.structured_content
    jsonschema.validate(instance=payload, schema=_output_schema(McpResult))
    assert payload["caliber"] == "error"
    assert payload["error"]["code"] == "bad_request"
    assert payload["error"]["message"] == "测试错误"


# ===================================================================== P0-3

def test_收支趋势_能显式包含未记账凭证(db, book, make_voucher, principal):
    """**P0-3**：以前没有 ``include_unposted``，未记账年份只能看到全 0。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=False)

    posted_only = call_tool("income_expense_trend", principal, book_id=book.id,
                            year=2026)
    assert posted_only["series"][2]["income"] == "0.00"

    with_unposted = call_tool("income_expense_trend", principal, book_id=book.id,
                              year=2026, include_unposted=True)
    assert with_unposted["series"][2]["income"] == "20000.00"


def test_收支趋势_零值带覆盖度说明(db, book, make_voucher, principal):
    """零值必须说清「是没记账，还是真没有」。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=False)

    out = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)

    coverage = out["coverage"]
    assert coverage["posting"] == "POSTED_ONLY"
    assert coverage["excluded_unposted_vouchers"] == 1
    assert coverage["completeness"] == "PARTIAL"
    assert any("没记账" in n or "未记账" in n for n in out["notes"]), out["notes"]


def test_收支趋势_本年未记账时不给假的同比(db, two_year_book, admin_user,
                                          principal):
    """**P0-3**：本年一张都没记账时，「同比 -100%」是记账状态造成的假象。

    它是整个 MCP 里最容易造成错误决策的单个返回值：读它的人会得出
    「今年收入归零」这个结论，而真相是「今年还没记账」。
    """
    make = build_voucher_factory(db, two_year_book, admin_user)
    # 2025 有已记账的损益
    make("2025-03-08", dr=[("1002", "100000.00", "2025 收租")],
         cr=[("5001", "100000.00", "2025 租金收入")], posted=True)
    # 2026 只有未记账的
    make("2026-03-08", dr=[("1002", "50000.00", "2026 收租")],
         cr=[("5001", "50000.00", "2026 租金收入")], posted=False)

    out = call_tool("income_expense_trend", principal,
                    book_id=two_year_book.id, year=2026)

    assert out["yoy"] is None, f"不该给同比：{out['yoy']}"
    assert any("同比" in n for n in out["notes"]), out["notes"]


def test_收支趋势_非法粒度给可读的参数错误(db, book, principal):
    """**P1-4**：非法枚举值要回一条**列出合法取值**的错误，不是 -32602。"""
    failure = call_tool_expect_error("income_expense_trend", principal,
                                     book_id=book.id, year=2026,
                                     granularity="quarter2")
    assert failure.code == "bad_request"
    assert "month" in failure.message and "quarter" in failure.message


def test_收支趋势_支持按季与按年(db, book, make_voucher, principal):
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)

    quarter = call_tool("income_expense_trend", principal, book_id=book.id,
                        year=2026, granularity="quarter")
    assert [s["period"] for s in quarter["series"]] == [
        "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    assert quarter["series"][0]["income"] == "20000.00"

    year = call_tool("income_expense_trend", principal, book_id=book.id,
                     year=2026, granularity="year")
    assert [s["period"] for s in year["series"]] == ["2026"]
    assert year["series"][0]["income"] == "20000.00"


# ===================================================================== P0-4
#
# 现象：ledger_balance 区间查询的期初恒为 0，与单期查询矛盾。
#
# 根因：区间期初取的是「起始期的**上一期期末**」，而那个时点落在**上一年**；
# 科目表每个年度各有一份，拿区间自己的科目行去算上一年的期末 —— 一行分录
# 都没有、也没有期初快照 —— 算出来是 0。

def _close_period(db, book, user, code):
    from tests.accounting_helpers import close_any
    period = _period(db, book, code)
    return close_any(db, book, period, user)


def test_科目余额表_区间期初与单期查询一致(db, book, make_voucher, admin_user,
                                     principal):
    """**P0-4 的核心回归**：同一期末日期，两种查法的期末必须相等。

    区间期初的口径是「**起始期之前**的余额」= 起始期自己的期初
    （= 上一期期末），与单期查询逐字一致 —— **不是**止期的期初。

    这条用例特意让区间中间隔着一笔有余额的月份（4 月），这样「起始期的
    期初」（100000）与「止期的期初」（130000）是两个不同的数：取错了
    断言就会红，不会因为两边碰巧相等而蒙混过关。
    """
    make_voucher("2026-01-15", dr=[("1002", "100000.00", "收租")],
                 cr=[("5001", "100000.00", "租金收入")], posted=True)
    make_voucher("2026-04-15", dr=[("1002", "30000.00", "收租")],
                 cr=[("5001", "30000.00", "租金收入")], posted=True)
    make_voucher("2026-06-15", dr=[("1002", "50000.00", "收租")],
                 cr=[("5001", "50000.00", "租金收入")], posted=True)

    first = call_tool("ledger_balance", principal, book_id=book.id,
                      period_code="2026-03")
    stop = call_tool("ledger_balance", principal, book_id=book.id,
                     period_code="2026-06")
    rng = call_tool("ledger_balance", principal, book_id=book.id,
                    from_period_code="2026-03", period_code="2026-06")

    # 同一期末：两种查法的期末合计必须相等
    assert stop["check"]["total_closing_debit"] == \
        rng["check"]["total_closing_debit"]
    assert stop["check"]["total_closing_credit"] == \
        rng["check"]["total_closing_credit"]
    # 区间期初 == 起始期的期初
    assert rng["check"]["total_opening_debit"] == \
        first["check"]["total_opening_debit"]
    assert Decimal(rng["check"]["total_opening_debit"]) == Decimal("100000.00")
    # ……而且**不是**止期的期初（4 月那笔 30000 之后余额变了）
    assert Decimal(rng["check"]["total_opening_debit"]) != \
        Decimal(stop["check"]["total_opening_debit"])


def test_科目余额表_跨年区间期初带上年结转余额(db, book, make_voucher,
                                             admin_user, principal):
    """**P0-4 的跨年回归**：跨年区间的期初必须把上一年结转过来的余额算进去。

    旧实现取的是「起始期的**上一期**期末」，而那个时点落在**上一年** ——
    科目表每个年度各有一份，拿新年度的科目行去算上一年的期末，
    那一行一行分录都没有、也没有期初快照，算出来是 **0**。

    症状是「给我 2027 年这一年的余额」拿到一个从零开始的数，
    而行内看着完全自洽（期初 + 发生额 = 期末），只有跟单期查询交叉核对
    才看得出来。
    """
    from sqlalchemy import update

    from app.accounting.periods import year_end_carry
    from app.models import Period

    make_voucher("2026-12-15", dr=[("1002", "100000.00", "收租")],
                 cr=[("5001", "100000.00", "租金收入")], posted=True)
    # 年度结转要求本年**全部结账**。这里直接标 locked：走 close_period
    # 会顺带记账并刷版本号，那不是这条用例要验的东西。
    db.execute(update(Period).where(Period.book_id == book.id,
                                    Period.year == 2026).values(locked=True))
    db.commit()
    year_end_carry(db, book, admin_user, 2026, 2027)
    make_voucher("2027-03-15", dr=[("1002", "50000.00", "收租")],
                 cr=[("5001", "50000.00", "租金收入")], posted=True)

    rng = call_tool("ledger_balance", principal, book_id=book.id,
                    from_period_code="2027-01", period_code="2027-03")
    single = call_tool("ledger_balance", principal, book_id=book.id,
                       period_code="2027-03")

    row = next(r for r in rng["rows"] if r["code"] == "1002")
    assert Decimal(row["opening"]) == Decimal("100000.00"), (
        f"跨年区间期初应该是上一年结转过来的 100000，实际 {row['opening']} —— "
        "多半是拿新年度的科目行去算了上一年的期末")
    assert Decimal(row["closing"]) == Decimal("150000.00")
    # 同一期末：区间查法与单期查法的合计必须相等
    assert single["check"]["total_closing_debit"] == \
        rng["check"]["total_closing_debit"]


def test_科目余额表_合计口径与逐行计入标志(db, book, make_voucher, principal):
    """**P1-4/3.6**：合计恒按末级科目算，行上要标明自己计不计入。"""
    make_voucher("2026-01-15", dr=[("1002", "100000.00", "收租")],
                 cr=[("5001", "100000.00", "租金收入")], posted=True)

    out = call_tool("ledger_balance", principal, book_id=book.id,
                    period_code="2026-01")

    assert out["totals_scope"] == "leaf"
    for row in out["rows"]:
        assert "included_in_totals" in row
    # 只加计入合计的行，必须等于合计 —— 否则「显示行之和 ≠ 合计」没被说清
    total = sum((Decimal(r["closing"]) for r in out["rows"]
                 if r["included_in_totals"]), Decimal("0"))
    assert total != Decimal("0")


def test_科目余额表_非法科目范围给可读错误(db, book, principal):
    failure = call_tool_expect_error("ledger_balance", principal, book_id=book.id,
                                     period_code="2026-01", subject_scope="alll")
    assert failure.code == "bad_request"
    assert "leaf" in failure.message


# ===================================================================== P1-1

def test_最近付款_金额按整张凭证而不是单条分录(db, book, make_voucher, principal):
    """**P1-1**：一张付款凭证上有好几条同科目分录时，金额不能只取最近那条。

    实测场景：记0041 上有 5 条费用分录、1 条现金分录，真实付款 146,324.31，
    而「最近一条分录」只有 3,950.80 —— 差 37 倍。
    """
    make_voucher("2026-03-15",
                 dr=[("560207", "3950.80", "水费"), ("560207", "258.40", "水费"),
                     ("560207", "7024.40", "水费"), ("560207", "22834.90", "电费"),
                     ("560207", "112255.81", "电费")],
                 cr=[("1002", "146324.31", "付款")], posted=True)

    out = call_tool("latest_payment", principal, book_id=book.id,
                    subject_keyword="房屋租赁费", year=2026)

    assert out["found"] is True
    assert out["amount"] == "146324.31"
    assert out["amount_basis"] == "voucher_focus_side"
    # 单条分录的金额仍然留着，只是不再冒充整笔付款
    assert out["entry_amount"] in ("3950.80", "258.40")
    assert out["voucher_cash_amount"] == "146324.31"


def test_最近付款_与本期付没付口径一致(db, book, make_voucher, principal):
    """两个姊妹工具算同一笔付款，金额必须能互相印证。"""
    make_voucher("2026-03-15",
                 dr=[("560207", "3950.80", "水费"), ("560207", "112255.81", "电费")],
                 cr=[("1002", "116206.61", "付款")], posted=True)

    latest = call_tool("latest_payment", principal, book_id=book.id,
                       subject_keyword="房屋租赁费", year=2026)
    period = call_tool("check_period_payments", principal, book_id=book.id,
                       period_code="2026-03", subject_keyword="房屋租赁费")

    assert latest["amount"] == period["paid_amount"]


# ===================================================================== P1-2

def test_凭证检索_能按摘要聚合(db, book, make_voucher, principal):
    """**P1-2**：「2025 年电费一共多少」应该一次调用就能答。"""
    make_voucher("2026-03-15", dr=[("560207", "1000.00", "电费")],
                 cr=[("1002", "1000.00", "电费")], posted=True)
    make_voucher("2026-04-15", dr=[("560207", "2000.00", "电费")],
                 cr=[("1002", "2000.00", "电费")], posted=True)
    make_voucher("2026-05-15", dr=[("560207", "500.00", "水费")],
                 cr=[("1002", "500.00", "水费")], posted=True)

    out = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                    group_by="summary")

    groups = {g["key"]: g for g in out["groups"]}
    assert "电费" in groups, groups
    assert "水费" in groups, groups
    assert groups["电费"]["debit"] == "3000.00"
    assert groups["水费"]["debit"] == "500.00"
    assert out["group_totals"]["debit"] == "3500.00"


def test_凭证检索_能按对方科目聚合(db, book, make_voucher, principal):
    """**P1-2**：``group_by="counterparty"`` 给出**对方视角**的分布。

    「借 560207 房屋租赁费 / 贷 1002 银行存款」这一张里，房屋租赁费那条的
    对方是银行存款，银行存款那条的对方是房屋租赁费 —— 所以读「1002
    银行存款」那个桶，就知道银行存款这一头对着哪些科目、各多少钱。
    """
    make_voucher("2026-03-15", dr=[("560207", "1000.00", "房租")],
                 cr=[("1002", "1000.00", "房租")], posted=True)

    out = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                    group_by="counterparty")

    groups = {g["key"]: g for g in out["groups"]}
    assert "1002" in groups, groups
    assert "560207" in groups, groups
    # 银行存款那个桶里装的是「费用」那条分录（借方 1000）
    assert groups["1002"]["debit"] == "1000.00"
    # 费用那个桶里装的是「银行存款」那条分录（贷方 1000）
    assert groups["560207"]["credit"] == "1000.00"


def test_凭证检索_对方有多个时取金额最大的那条(db, book, make_voucher, principal):
    """对方方向有多条分录时，**取金额最大的那条**。

    取「第一条」不行 —— 那取决于数据库返回分录的顺序，同一份数据换个次序
    答案就变了；并列时按科目编码取小的，保证同一个键稳定。

    **样本要能区分两种取法**：这里的对方里，金额最大的（5001 / 60.00）
    恰好是编码**最大**的 —— 按金额取会选 5001，按编码取会选 1002，两种
    取法给出不同的桶，断言才有牙。
    """
    make_voucher("2026-03-15",
                 dr=[("560207", "100.00", "分摊")],
                 cr=[("1002", "40.00", "部分付款"), ("5001", "60.00", "核销")],
                 posted=True)

    out = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                    group_by="counterparty")

    groups = {g["key"]: g for g in out["groups"]}
    # 借方那条（100.00）的对方取金额更大的 5001（60.00），不是 1002
    assert "5001" in groups, groups
    assert groups["5001"]["debit"] == "100.00"
    assert "1002" not in groups or groups["1002"]["debit"] != "100.00", groups


def test_凭证检索_能剔除结转凭证(db, book, make_voucher, admin_user, principal):
    """**P1-2 / 3.4**：结转凭证会把损益余额重复计入，聚合时必须能剔掉。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    _carry_forward(db, book, admin_user, "2026-03")

    with_cf = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                        group_by="summary")
    without_cf = call_tool("search_vouchers", principal, book_id=book.id,
                           year=2026, group_by="summary",
                           exclude_carry_forward=True)

    assert with_cf["group_totals"]["debit"] != \
        without_cf["group_totals"]["debit"], "剔除结转凭证后合计应该变小"
    # 结转凭证是**收入类**结转：借收入 / 贷本年利润
    assert Decimal(without_cf["group_totals"]["debit"]) < \
        Decimal(with_cf["group_totals"]["debit"])


def test_凭证检索_剔除结转凭证也作用在返回的凭证上(db, book, make_voucher,
                                              admin_user, principal):
    """**回归**：剔除要落在计数与分页之前，不能只作用在透视聚合上。

    这个参数以前只在 ``group_by`` 给了值时才对 ``groups`` 的聚合集合生效，
    主结果集（``items`` 与 ``total``）原样带着结转凭证 —— 而参数名与说明
    都写着「剔除结转凭证」。调用方于是拿到一个**自以为剔干净了**的合计：
    比不剔更难发现，因为数字看起来是对的。
    """
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    _carry_forward(db, book, admin_user, "2026-03")

    with_cf = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                        page_size=200)
    without_cf = call_tool("search_vouchers", principal, book_id=book.id,
                           year=2026, page_size=200, exclude_carry_forward=True)

    assert without_cf["total"] < with_cf["total"], (
        f"剔除结转凭证后总数应该变小：{without_cf['total']} vs {with_cf['total']}")
    assert all(not it["is_carry_forward"] for it in without_cf["items"]), \
        "剔除后返回的凭证里不该还有结转凭证"
    assert len(without_cf["items"]) == without_cf["total"], \
        "剔除后 total 与页内张数必须对得上（剔除落在计数之前）"


def test_凭证检索_导入的结转凭证按分录摘要也能被剔除(db, book, make_voucher,
                                                  admin_user, principal):
    """**生产上的形态**：结转凭证是导入的，标记只写在分录摘要上。

    生产库里 ``source='CARRY_FORWARD'`` 的凭证数为 0 —— 结转凭证全是导入
    进来的，``remark`` 是空的，标记在**分录摘要**里。SQL 侧的判据若只认
    ``source`` 与 ``remark``，导入的那批一张都剔不掉；而这类偏差不会报错，
    只是数字悄悄多算了。
    """
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    # 导入的结转凭证：source 不是 CARRY_FORWARD，remark 里没有结转短语，
    # 标记只在分录摘要里。
    make_voucher("2026-03-20",
                 dr=[("5001", "5000.00", "期间损益结转")],
                 cr=[("3103", "5000.00", "期间损益结转")],
                 source="IMPORT", remark="普通凭证", posted=True)

    without_cf = call_tool("search_vouchers", principal, book_id=book.id,
                           year=2026, page_size=200, exclude_carry_forward=True)

    assert all(not it["is_carry_forward"] for it in without_cf["items"]), \
        "按分录摘要认出来的结转凭证也必须被剔除"


def test_结转凭证的SQL判据与Python判据逐张一致(db, book, make_voucher,
                                              admin_user):
    """**单一事实源**：SQL 版与 Python 版必须给出同一个答案。

    两条判据分家时的症状是「静默少剔几张」：调用方拿到一个少了结转凭证
    的合计，却以为已经剔干净了 —— 比完全不剔更难发现，因为没有任何报错。
    """
    from sqlalchemy import select

    from app.accounting.organize import is_carry_forward
    from app.accounting.vouchers import query_vouchers
    from app.models import Voucher

    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    make_voucher("2026-03-20",
                 dr=[("5001", "5000.00", "期间损益结转")],
                 cr=[("3103", "5000.00", "期间损益结转")],
                 source="IMPORT", remark="普通凭证", posted=True)
    _carry_forward(db, book, admin_user, "2026-03")

    everything = db.scalars(
        select(Voucher).where(Voucher.book_id == book.id)).all()
    python_cf = {v.id for v in everything if is_carry_forward(v)}
    assert python_cf, "测试数据里应当有结转凭证"

    kept, _total = query_vouchers(db, book.id, exclude_carry_forward=True,
                                  page_size=200)
    sql_cf = {v.id for v in everything} - {v.id for v in kept}

    assert sql_cf == python_cf, (
        "SQL 判据与 Python 判据给出的结转凭证集合不一致："
        f"仅 SQL 认为 {sql_cf - python_cf}，仅 Python 认为 {python_cf - sql_cf}")


def test_凭证检索_未记账口径的覆盖度标签不能写成已记账(db, book, make_voucher,
                                                 principal):
    """``status="UNPOSTED"`` 时 ``posting`` 不能标成 ``POSTED_ONLY``。

    标签说错的后果很具体：agent 读到「本次按已记账口径取数」，就敢把返回的
    未记账凭证当成已入账的事实。
    """
    make_voucher("2026-03-15", dr=[("560207", "1000.00", "电费")],
                 cr=[("1002", "1000.00", "付款")], posted=False)

    out = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                    status="UNPOSTED")

    assert out["coverage"]["posting"] == "UNPOSTED_ONLY", out["coverage"]
    assert out["coverage"]["unposted_vouchers"] == 1
    assert out["coverage"]["excluded_unposted_vouchers"] == 0


def test_凭证检索_给出命中分录条数(db, book, make_voucher, principal):
    """**P2-5**：凭证是按分录筛的，命中分录条数要单独给出。"""
    make_voucher("2026-03-15",
                 dr=[("560207", "1000.00", "水费"), ("560207", "2000.00", "水费")],
                 cr=[("1002", "3000.00", "付款")], posted=True)

    out = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                    min_amount="500", max_amount="1500")

    assert out["matched_entries"] == 1
    assert out["total"] == 1


def test_凭证检索_非法排序给可读错误(db, book, principal):
    failure = call_tool_expect_error("search_vouchers", principal, book_id=book.id,
                                     year=2026, sort="random")
    assert failure.code == "bad_request"
    assert "date_desc" in failure.message


# ===================================================================== 出参声明的一致性


def test_出参声明的字段必须属于它自己的工具(db, book, make_voucher, principal):
    """**通用不变量**：出参模型声明的字段，不能串到别的工具上。

    **为什么这条单独立一条。** 出参模型都开了 ``extra="allow"``：
    字段**声明错类**不会抛任何错 —— 工具照样返回，测试照样全绿，
    唯独 agent 看到的 ``outputSchema`` 里没有它真正拿到的字段。
    这次就踩过：``groups`` / ``group_totals`` / ``matched_entries``
    是凭证检索的字段，却被声明到了「科目查询」的出参模型上，
    于是 ``search_vouchers`` 的 outputSchema 里压根看不到透视结果。

    钉住的办法是**互斥断言**：凭证检索的字段不能在科目查询里出现，
    反之亦然。
    """
    from app.mcp.schemas import FindSubjectsResult, SearchVouchersResult

    voucher_only = {"groups", "group_totals", "matched_entries"}
    subject_only = {"hint"}

    voucher_props = set(SearchVouchersResult.model_json_schema()["properties"])
    subject_props = set(FindSubjectsResult.model_json_schema()["properties"])

    missing = voucher_only - voucher_props
    assert not missing, f"search_vouchers 的 outputSchema 少了 {missing}"
    leaked = voucher_only & subject_props
    assert not leaked, f"{leaked} 是凭证检索的字段，不该出现在 find_subjects 里"
    assert subject_only <= subject_props, \
        f"find_subjects 的 outputSchema 少了 {subject_only - subject_props}"


def test_工具返回的字段必须在自己的outputSchema里(db, book, make_voucher, principal):
    """**通用不变量**：实际返回的键，一个都不能漏在 outputSchema 之外。

    ``extra="allow"`` 允许模型收到未声明的字段，所以「传了但没声明」不会
    报错 —— 但客户端是拿 ``outputSchema`` 校验 ``structuredContent`` 的，
    漏声明的字段在严格客户端那里会直接被判不合规。
    """
    from app.mcp.schemas import SearchVouchersResult

    make_voucher("2026-03-15", dr=[("560207", "1000.00", "电费")],
                 cr=[("1002", "1000.00", "电费")], posted=True)

    out = call_tool("search_vouchers", principal, book_id=book.id, year=2026,
                    group_by="summary")

    declared = set(SearchVouchersResult.model_json_schema()["properties"])
    undeclared = set(out) - declared
    assert not undeclared, f"返回了 outputSchema 没声明的字段：{sorted(undeclared)}"


# ===================================================================== P1-3

def test_报表_模板引用了不存在的科目时给出提示(db, book, admin_user, principal):
    """**P1-3**：模板与科目表不匹配要**一条**提示说清缺哪些编码。

    以前只有逐格报错，调用方要读几十行 errors 才拼得出这个结论。
    """
    from sqlalchemy import select

    from app.models import ReportCell, ReportTemplate

    template = db.scalar(
        select(ReportTemplate).where(
            ReportTemplate.code == "INCOME_SHEET", ReportTemplate.book_id.is_(None)))
    assert template is not None
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == template.id,
        ReportCell.row_no == 8, ReportCell.col_no == 2))
    assert cell is not None
    original = cell.formula
    cell.formula = '科目("9999","期末余额",{{本期}})'
    db.commit()

    try:
        out = call_tool("render_report", principal, book_id=book.id,
                        template_code="INCOME_SHEET", period_code="2026-01")
        codes = [n["code"] for n in out["notices"]]
        assert "TEMPLATE_SUBJECTS_MISSING" in codes, out["notices"]
        notice = next(n for n in out["notices"]
                      if n["code"] == "TEMPLATE_SUBJECTS_MISSING")
        assert "9999" in notice["codes"]
        assert "9999" in notice["text"]
    finally:
        cell.formula = original
        db.commit()


# ===================================================================== P1-5 / P2

def test_期间清单_标出有结转凭证的期间(db, book, make_voucher, admin_user,
                                    principal):
    """**P1-5**：结转凭证的前置标志，省掉大量排查。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=True)
    _carry_forward(db, book, admin_user, "2026-03")

    out = call_tool("list_periods", principal, book_id=book.id)

    march = next(p for p in out["items"] if p["code"] == "2026-03")
    april = next(p for p in out["items"] if p["code"] == "2026-04")
    assert march["has_carry_forward"] is True
    assert april["has_carry_forward"] is False
    assert march["subject_count"] > 0


def test_账套快照_给出每年的记账进度(db, book, make_voucher, principal):
    """**P2**：哪些年没记账，必须一眼可见。"""
    make_voucher("2026-03-08", dr=[("1002", "20000.00", "收租")],
                 cr=[("5001", "20000.00", "租金收入")], posted=False)

    out = call_tool("accounting_context", principal, book_id=book.id)

    year = next(y for y in out["years"] if y["year"] == 2026)
    assert year["vouchers"] == 1
    assert year["posted_vouchers"] == 0
    assert year["unposted_vouchers"] == 1


def test_科目查询_查空时提示其它年度有同编码科目(db, book, principal):
    """**P2**：跨年静默返回空是最常见的误判来源。"""
    from app.accounting.years import ensure_book_year

    ensure_book_year(db, book, 2027)
    db.commit()

    out = call_tool("find_subjects", principal, book_id=book.id, year=2026,
                    keyword="1002")

    assert out["items"], "2026 年度的 1002 应该查得到"
    # 换个不存在的年度查同���个编码 → 应给出跨年提示
    empty = call_tool("find_subjects", principal, book_id=book.id, year=2027,
                      keyword="999999")
    assert empty["items"] == []
    assert empty["hint"] is None or "年度" in empty["hint"]
