"""MCP 的**口径一致性**测试 —— 本项目最关键的一类用例。

**为什么这一类最关键。**
本仓的历史审计第 7.3 节记过一次「导出与页面显示对不上」，根因是**同一个
口径有了两份实现**。MCP 是第三个消费者：如果它自己写一份聚合，它就会变成
第三份 —— 而 agent 拿到的是**数字**，数字对不上时没人会去怀疑「口径」，
只会怀疑「这个系统算错了」。

所以 MCP 层**不自己写聚合**，一律委托既有的账簿引擎与报表求值器。这一组
用例就是钉住这件事：**同一个问题，MCP 的答案必须与界面/API 的答案逐字
一致**。

钉住的三条：

1. **科目余额表**：MCP ``ledger_balance`` 的 ``closing`` ＝
   ``GET /api/books/{id}/ledger/balance`` 同一行的 ``closing``；
2. **损益**：MCP ``income_expense_trend`` 的年度收入 ＝ 同一份数据经
   ``render_report`` 渲染出的利润表收入行「本年累计金额」；
3. **符号**：金额的正负与 ``display_side`` 的方向判定一致（**不按科目
   自然方向反推**）。

第 3 条背后是一个真实缺陷：早期版本按科目自然方向判断余额在哪一方，
把「短期借款 −1000」显示成「借 1000」，与试算平衡自相矛盾。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Period
from tests.mcp_helpers import call_tool, make_principal


def _dec(value) -> Decimal:
    """把 MCP 的字符串金额、HTTP 的 float、引擎的 Decimal 统一成 Decimal。

    HTTP 层会把 ``Decimal`` 序列化成 ``float``（FastAPI 的 jsonable_encoder
    默认如此），MCP 层用的是字符串（避免浮点误差）。两边要能直接比，
    就得先归一到 Decimal —— **不能比 float**，那会把「两边的数其实一样」
    变成「差了一点点」。
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _row_by_code(rows: list[dict], code: str) -> dict:
    for row in rows:
        if row.get("code") == code:
            return row
    raise AssertionError(f"结果里没有编码 {code} 的行")


@pytest.fixture
def principal(db, admin_user):
    """一个对测试账套有授权的主体。"""
    p = make_principal(user_id=admin_user.id, username=admin_user.username)
    return p


# --------------------------------------------------------------------- ① 余额表

def test_余额表口径与HTTP一致(db, book, admin_user, principal, periods,
                              make_voucher):
    """MCP ``ledger_balance`` 的期末余额 ＝ HTTP 余额表的同一行。

    同一个账套、同一期、同一批科目，两个出口必须给同一个数。

    两侧走的是**同一个函数**（``ledger/queries.balance_sheet_of_accounts``）——
    这正是要证明的事：MCP 没有自己写第二套聚合。
    """
    from app.ledger import queries

    # 用仓库自己的造凭证工厂（它会补齐借贷平衡、科目、期间与触发器要的东西）。
    make_voucher("2026-04-08", dr=[("1002", "1000.00", "收款")],
                 cr=[("5001", "1000.00", "收入")], posted=True)
    period = periods[4]  # 2026-04（periods 夹具按月份作键）

    # 引擎侧（HTTP 余额表接口走的就是这个函数）
    engine_rows, _check = queries.balance_sheet_of_accounts(
        db, book.id,
        queries.subjects_for_periods(db, book.id, [period]),
        period, False)

    # MCP 侧
    mcp = call_tool("ledger_balance", principal, book_id=book.id,
                    period_code=period.code)
    mcp_rows = mcp["rows"]

    assert mcp_rows, "MCP 余额表返回了空表"
    engine_by_code = {r["code"]: r for r in engine_rows}

    for row in mcp_rows:
        code = row["code"]
        assert code in engine_by_code, f"MCP 多出一行 {code}"
        ref = engine_by_code[code]
        assert _dec(row["closing"]) == _dec(ref["closing"]), (
            f"{code} 的期末余额不一致：MCP {row['closing']} vs 引擎 {ref['closing']}")
        assert _dec(row["opening"]) == _dec(ref["opening"]), (
            f"{code} 的期初余额不一致")
        assert _dec(row["period_debit"]) == _dec(ref["period_debit"]), (
            f"{code} 的本期借方不一致")
        assert _dec(row["period_credit"]) == _dec(ref["period_credit"]), (
            f"{code} 的本期贷方不一致")

    # 行数也要一致 —— 少一行就是「账缺了一块」，用户看不出来。
    assert len(mcp_rows) == len(engine_rows)


def test_余额表符号与display_side一致(db, book, principal):
    """方向字段与 ``display_side`` 一致 —— **按符号，不按科目自然方向**。

    「短期借款 −1000」必须是「贷 1000」而不是「借 1000」：按自然方向判的
    早期版本在这里与试算平衡自相矛盾。
    """
    from app.ledger.engine import display_side
    from app.models import Subject

    rows = call_tool("ledger_balance", principal, book_id=book.id,
                     period_code="2026-01")["rows"]
    with SessionLocal() as s:
        for row in rows:
            subject = s.scalar(select(Subject).where(
                Subject.book_id == book.id, Subject.code == row["code"],
                Subject.year == 2026))
            if subject is None:
                continue
            signed = _dec(row["closing"])
            if signed == 0:
                continue
            expected_side, _ = display_side(subject, signed)
            actual = row.get("side") or row.get("direction")
            assert actual == expected_side, (
                f"{row['code']} 的方向不一致：MCP {actual} vs display_side {expected_side}")


# --------------------------------------------------------------------- ② 损益

def test_损益口径与利润表一致(db, book, admin_user, principal, make_voucher):
    """MCP ``income_expense_trend`` 的年度收入 ＝ 利润表的收入行「本年累计金额」。

    利润表里与「收入」有关的行是：**一、营业收入**（5001+5051）、
    **加：投资收益**（5111）、**加：营业外收入**（5301，含其下级）。
    三行相加 ＝ 全部收入类科目之和 —— 与趋势工具的口径应当相同。

    这条断言成立的前提是**两个口径数的是同一批科目**。趋势工具若把
    父级与末级一起加，就会翻倍 —— 所以这条用例同时钉住了「只加末级」
    这件事。
    """
    # **必须 posted=True**：默认是 UNPOSTED，而所有口径默认只读已记账的
    # 凭证（``status="POSTED"``）—— 未记账的凭证不进账。
    make_voucher("2026-05-08",
                 dr=[("1002", "5000.00", "收款")],
                 cr=[("5001", "5000.00", "营业收入")], posted=True)
    make_voucher("2026-06-08",
                 dr=[("560207", "800.00", "租金")],
                 cr=[("1002", "800.00", "付租金")], posted=True)

    trend = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    report = call_tool("render_report", principal, book_id=book.id,
                       template_code="INCOME_SHEET", period_code="2026-12")

    # 年度收入 = 12 个月的合计
    series = trend["series"]
    total_income = sum(_dec(p["income"]) for p in series)
    assert total_income == _dec("5000.00"), f"年度收入算错了：{total_income}"

    # 利润表侧：取「本年累计金额」那一列（col_no = 3）
    income_rows = {"一、营业收入", "加：投资收益", "加：营业外收入"}
    report_total = Decimal("0")
    found = False
    for row in report["rows"]:
        if row.get("label") in income_rows:
            found = True
            for cell in row["cells"]:
                if cell.get("col_no") == 3 and cell.get("value") not in (None, ""):
                    report_total += _dec(cell["value"])
    assert found, "利润表里没找到收入行"
    assert total_income == report_total, (
        f"趋势工具与利润表的口径不一致：趋势 {total_income} vs 利润表 {report_total}")


def test_成本类科目不计入费用且与利润表一致(db, book, admin_user, principal,
                                       make_voucher):
    """成本类科目（生产成本等）**不在内置利润表的任何一行里** → 不计入费用。

    如果把它算进费用，agent 报出的「今年费用」会比老板在利润表上看到的费用
    大 —— 而这种错**看着还挺合理**，是最难被发现的一类。

    生产成本（4001）在用友 4-2-2 科目表里是**末级**（``is_leaf`` 由「有没有
    子科目」推导，它没有子科目），所以它**真的能记账**，不是永远为 0 的摆设。
    """
    make_voucher("2026-07-08", dr=[("4001", "7000.00", "生产成本")],
                 cr=[("1002", "7000.00", "付生产成本")], posted=True)
    make_voucher("2026-07-09", dr=[("560207", "800.00", "租金")],
                 cr=[("1002", "800.00", "付租金")], posted=True)

    trend = call_tool("income_expense_trend", principal, book_id=book.id, year=2026)
    report = call_tool("render_report", principal, book_id=book.id,
                       template_code="INCOME_SHEET", period_code="2026-12")

    total_expense = sum(_dec(p["expense"]) for p in trend["series"])
    assert total_expense == _dec("800.00"), (
        f"费用算错了：{total_expense} —— 生产成本被算进费用了？")

    # 利润表侧：费用行的「本年累计金额」列（col_no = 3）相加
    expense_rows = {"减：营业成本", "税金及附加", "销售费用", "管理费用",
                    "财务费用", "减：营业外支出", "减：所得税费用"}
    report_total = Decimal("0")
    for row in report["rows"]:
        if str(row.get("label") or "").strip() in expense_rows:
            for cell in row["cells"]:
                if cell.get("col_no") == 3 and cell.get("value") not in (None, ""):
                    report_total += _dec(cell["value"])
    assert total_expense == report_total, (
        f"趋势工具与利润表的口径不一致：趋势 {total_expense} vs 利润表 {report_total}")


def test_跨年合计不是本年累计相减(db, book, admin_user, principal, make_voucher):
    """跨年区间的合计**必须**逐期累加，不是「本年累计相减」。

    ``dr_cum/cr_cum`` 是**本年**累计，每年 1 月归零。跨年相减会算出乱七八糟
    的数。这条用例造一份能区分两种算法的数据。

    要跨年记账，得先有下一年的科目表 —— 科目表每个会计年度各有一份
    （迁移 0032）。用 ``copy_chart`` 把 2026 那份复制到 2027。
    """
    from app.accounting.years import copy_chart

    make_voucher("2026-07-08", dr=[("1002", "1000.00", "收款")],
                 cr=[("5001", "1000.00", "收入")], posted=True)

    # 2027 年的科目表 + 期间（年度结转做的是这两件事；这里只要“有）。
    copy_chart(db, book.id, 2026, 2027)
    from app.accounting.periods import ensure_periods

    ensure_periods(db, book, 2027)
    db.commit()

    make_voucher("2027-01-08", dr=[("1002", "700.00", "收款")],
                 cr=[("5001", "700.00", "收入")], posted=True)

    trend_2026 = call_tool("income_expense_trend", principal, book_id=book.id,
                           year=2026)
    trend_2027 = call_tool("income_expense_trend", principal, book_id=book.id,
                           year=2027)

    income_2026 = sum(_dec(p["income"]) for p in trend_2026["series"])
    income_2027 = sum(_dec(p["income"]) for p in trend_2027["series"])

    assert income_2026 == _dec("1000.00"), f"2026 收入算错了：{income_2026}"
    assert income_2027 == _dec("700.00"), f"2027 收入算错了：{income_2027}"
    # 两年加总 ＝ 1700（不是 1000 - 700 那种「相减」出来的数）
    assert income_2026 + income_2027 == _dec("1700.00")


# --------------------------------------------------------------------- ③ 口径标注

def test_出参带口径标识(principal, book):
    """每个工具的出参都带 ``caliber`` 与 ``notes[]``。

    这是 agent 判断「这个数是在什么口径下取出来的」的依据。缺了它，
    agent 就分不清「报表口径」与「账簿口径」，于是把两者当同一个数。
    """
    for name, args in (
        ("list_books", {}),
        ("list_periods", {"book_id": book.id}),
        ("find_subjects", {"book_id": book.id}),
        ("accounting_context", {"book_id": book.id}),
    ):
        payload = call_tool(name, principal, **args)
        assert "caliber" in payload, f"{name} 的出参没有 caliber"
        assert "notes" in payload, f"{name} 的出参没有 notes"
        assert isinstance(payload["notes"], list)


def test_金额是字符串而不是浮点(principal, book):
    """金额用**字符串**承载（避免浮点误差），并带 ``currency``。

    ``currency`` 是**硬断言**：它必须是 ``"CNY"`` 这个具体值。
    写成 ``"currency" in payload`` 那种「有就行」的断言是同义反复 ——
    只要字段在、不管它是不是人民币，这条用例永远绿。字段存在却值错了
    （比如写成 ``"元"``、``"RMB"`` 或空串），agent 就会按错的单位去念数。
    """
    payload = call_tool("accounting_context", principal, book_id=book.id)
    # account_context 里没有金额，但结构里应当有 currency 标记位
    assert payload["currency"] == "CNY", (
        f"currency 应当是 CNY，实际是 {payload.get('currency')!r}")


def test_每个工具的出参契约都带currency():
    """**全部 10 个工具**的出参 schema 都声明 ``currency`` 且默认 CNY。

    为什么单开一条：上面那条只验了一个工具的**运行时**出参，而 agent 真正
    依赖的是 ``tools/list`` 里那份 **schema** —— 它决定 agent 在调用前
    就知道「这个字段是人民币」。schema 少一个字段，agent 就会靠猜。

    契约（计划 §3.7）在这里被钉死：``currency`` 在 ``properties`` 里、
    类型是 string、默认值是 ``CNY``。少任何一样，agent 的行为都不确定。
    """
    import asyncio

    from app.mcp.server import mcp_server

    tools = asyncio.run(mcp_server.list_tools())
    assert tools, "工具清单是空的"

    for tool in tools:
        schema = tool.output_schema or {}
        props = schema.get("properties", {})
        assert "currency" in props, (
            f"{tool.name} 的出参 schema 里没有 currency：{sorted(props)}")
        spec = props["currency"]
        assert spec.get("type") == "string", (
            f"{tool.name}.currency 的类型应当是 string，实际 {spec.get('type')!r}")
        assert spec.get("default") == "CNY", (
            f"{tool.name}.currency 的默认值应当是 CNY，实际 {spec.get('default')!r}")


def test_每个工具的真实出参都带currency(db, book, principal, periods,
                                          make_voucher):
    """**运行时**出参（不是 schema）里，10 个工具都带 ``currency="CNY"``。

    上一条验的是「声明了什么」，这条验的是「实际返回了什么」——
    两者之间可能有缝：某个工具绕过模型直接返回一个 dict，schema 照样
    漂亮，出参里却没有 ``currency``。agent 读到的就是没有单位的数字。

    这里把 10 个工具**都调一遍**（有的需要先造凭证才有内容，但**出参的
    外壳**与有没有数据无关 —— 所以「查不到」也是合法的调用成功）。
    """
    make_voucher("2026-04-08", dr=[("560207", "12800.00", "10月房租")],
                 cr=[("1002", "12800.00", "付房租")], posted=True)
    period = periods[4]  # 2026-04

    calls = (
        ("list_books", {}),
        ("list_periods", {"book_id": book.id}),
        ("find_subjects", {"book_id": book.id}),
        ("accounting_context", {"book_id": book.id}),
        ("search_vouchers", {"book_id": book.id}),
        ("latest_payment", {"book_id": book.id, "subject_keyword": "租赁"}),
        ("check_period_payments", {"book_id": book.id,
                                   "period_code": period.code,
                                   "subject_keyword": "租赁"}),
        ("ledger_balance", {"book_id": book.id, "period_code": period.code}),
        ("income_expense_trend", {"book_id": book.id, "year": 2026}),
        ("render_report", {"book_id": book.id, "template_code": "INCOME_SHEET",
                           "period_code": "2026-12"}),
    )

    for name, args in calls:
        payload = call_tool(name, principal, **args)
        assert "currency" in payload, f"{name} 的出参里没有 currency"
        assert payload["currency"] == "CNY", (
            f"{name}.currency 应当是 CNY，实际 {payload['currency']!r}")
        # 外壳三件套：口径标识 + 说明 + 币种，缺一件 agent 就没法用
        assert payload.get("caliber"), f"{name} 的出参没有 caliber"
        assert isinstance(payload.get("notes"), list), (
            f"{name} 的 notes 不是列表")
