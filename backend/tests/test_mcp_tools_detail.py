"""MCP 明细类工具的测试（工具 4/5/6/7）。

**怎么测 MCP 工具。**
MCP 的工具是同步函数，靠 ``ctx``（``mcp.server.mcpserver.Context``）拿到
请求上下文，主体挂在请求 scope 的 ``state["mcp_principal"]`` 上。所以这里
构造一个**真的** ``Context``（带一个 starlette ``Request``），再直接调用
被装饰过的函数 —— 装饰器会照常开数据库会话、判账套权限、写读取审计。

**这些用例在钉什么。**
主要是三件事：

1. 「钱到底付出去没有」的四种状态分得开（已付 / 仅计提未付 / 有凭证但未记账 /
   本期无记录）—— 这是 MCP 面最容易答错的一件事（计划 §8.2 R9）；
2. 口径不漂：工具 7 的期末余额与 ``GET /api/books/{id}/ledger/balance``
   同一行数值一致（计划 §6.1 的核心）；
3. 只读与越权：默认不混入未记账凭证，没有账套授权时是**结构化 403**，
   不是异常、不是 500。
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult
from pydantic import BaseModel
from mcp.server.context import ServerRequestContext
from starlette.requests import Request

from app.mcp.auth import McpPrincipal
from app.mcp.asgi import PRINCIPAL_KEY
from app.mcp.tools import detail
from app.models import Subject
from tests.mcp_helpers import error_of


# --------------------------------------------------------------------- 夹具


def _make_ctx(principal: McpPrincipal) -> Context:
    """构造一个带主体的真 ``Context``。

    主体挂在 ``scope["state"]["mcp_principal"]`` 上 —— 与鉴权中间件
    （``app/mcp/asgi.py``）挂的位置**逐字一致**，因为工具取主体的路径就是
    ``request.scope["state"][PRINCIPAL_KEY]``。
    """
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/mcp",
        "headers": [],
        "state": {PRINCIPAL_KEY: principal},
    }
    request = Request(scope)
    request_context = ServerRequestContext(
        session=None,
        lifespan_context=None,
        protocol_version="2025-06-18",
        method="tools/call",
        request=request,
    )
    return Context(request_context=request_context)


def _call(fn, **kwargs):
    """直接调一个工具，把出参**摊成字典**再返回。

    出参是 Pydantic 模型（``app/mcp/schemas.py``）—— 模型化是为了给 agent
    发布 JSON Schema 与结构化输出，不是为了让用例改写法。这里摊平，
    用例读起来仍是 ``result["rows"]``。

    出错时工具回的是 ``CallToolResult``（带 ``is_error``）—— 那不是模型，
    原样返回，让断言去查 ``is_error`` 与 ``structured_content``。
    """
    result = fn(**kwargs)
    # **顺序要紧**：``CallToolResult`` 自己也是 Pydantic 模型，先判它 ——
    # 错误回执要原样留着，用例查的是 ``is_error`` 与 ``structured_content``。
    if isinstance(result, CallToolResult):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump()
    return result


@pytest.fixture
def principal(admin_user) -> McpPrincipal:
    """一个已授权的管理员主体。"""
    return McpPrincipal(
        user_id=admin_user.id,
        username=admin_user.username,
        token_id=0,
        token_name="test",
    )


@pytest.fixture
def ctx(principal) -> Context:
    return _make_ctx(principal)


@pytest.fixture
def outsider_ctx(plain_user) -> Context:
    """一个**没有**任何账套授权的主体。

    ``plain_user`` 在库里没有 ``UserBook`` 行 —— 与管理员那个用户正好成对：
    只测「有权限时能查到」是不够的，必须测「没权限时查不到」。
    """
    return _make_ctx(McpPrincipal(
        user_id=plain_user.id, username=plain_user.username,
        token_id=0, token_name="test"))


# --------------------------------------------------------------------- 工具 4


def test_search_vouchers_normal(db, book, make_voucher, ctx):
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id)

    assert result["total"] == 1
    assert result["page"] == 1
    assert result["page_size"] == 50
    item = result["items"][0]
    assert item["voucher_date"] == "2026-10-08"
    assert item["period_code"] == "2026-10"
    assert item["status"] == "POSTED"
    assert item["total_debit"] == "12800.00"
    assert item["total_credit"] == "12800.00"
    codes = {e["subject_code"] for e in item["entries"]}
    assert codes == {"560207", "1002"}
    # 费用那条分录的对方是现金类 ⇒ counterparty_cash 为真
    rent = [e for e in item["entries"] if e["subject_code"] == "560207"][0]
    bank = [e for e in item["entries"] if e["subject_code"] == "1002"][0]
    assert rent["counterparty_cash"] is True
    assert bank["counterparty_cash"] is False


def test_search_vouchers_keyword_does_not_match_subject_name(
        db, book, make_voucher, ctx):
    """**keyword 不搜科目名** —— 这条口径要钉死。

    ``query_vouchers`` 的 keyword 只搜凭证号 / 备注 / 分录摘要
    （``accounting/vouchers.py:1240-1250``）。凭证里出现了「房屋租赁费」
    这个科目名，但摘要与备注都没写它 ⇒ 按名字搜**搜不到**。
    找科目要用 ``find_subjects``，不能拿 keyword 当科目名过滤器用。
    """
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月房租")],
                 cr=[("1002", "12800.00", "付房租")], posted=True)

    by_name = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                                     keyword="房屋租赁费")
    assert by_name["total"] == 0

    by_summary = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                                        keyword="房租")
    assert by_summary["total"] == 1


def test_search_vouchers_pagination_total_and_clamp(db, book, make_voucher, ctx):
    for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"):
        make_voucher(day, dr=[("560206", "100.00", "电费")],
                     cr=[("1002", "100.00", "付电费")], posted=True)

    first = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, page=1,
                                   page_size=2)
    assert first["total"] == 4          # total 是过滤后的总数，不是本页条数
    assert len(first["items"]) == 2

    second = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, page=2,
                                    page_size=2)
    assert second["total"] == 4
    assert len(second["items"]) == 2
    numbers = {i["voucher_no"] for i in first["items"] + second["items"]}
    assert len(numbers) == 4

    # 页长超限被夹住（mcp_max_page_size=200）
    over = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, page_size=9999)
    assert over["page_size"] == 200


def test_search_vouchers_excludes_unposted_by_default(db, book, make_voucher, ctx):
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=False)

    default = _call(detail.search_vouchers, ctx=ctx, book_id=book.id)
    assert default["total"] == 0
    assert default["items"] == []

    with_unposted = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                                            include_unposted=True)
    assert with_unposted["total"] == 1

    only_unposted = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                                            status="UNPOSTED")
    assert only_unposted["total"] == 1


def test_search_vouchers_cross_year_subject_id_is_a_readable_400(
        db, book, make_voucher, ctx):
    """用 2026 年的科目 id 查 2027 年 ⇒ **结构化 400**，不是空结果。

    科目表每个会计年度各有一份（迁移 0032），2026 与 2027 的同名科目是
    **两行、两个 id**。跨年 id 混用匹配不到任何分录 —— 而「空」是最坏的
    一种答法：agent 会把它读成「那一年没有这笔业务」，然后把这个结论
    告诉老板。

    计划 §5.3 要求这里回 400 并说明「两份科目表，请指定 year」——
    agent 知道是**自己传错了 id**，才能换个年度重取。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()

    make_voucher("2027-03-05", dr=[("560207", "900.00", "次年租金")],
                 cr=[("1002", "900.00", "付租金")], posted=True)

    subject_2026 = db.query(Subject).filter_by(
        book_id=book.id, year=2026, code="560207").one()
    subject_2027 = db.query(Subject).filter_by(
        book_id=book.id, year=2027, code="560207").one()
    # 两份科目表是两个 id —— 前提本身就是这条用例要守的东西
    assert subject_2026.id != subject_2027.id

    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, year=2027,
                                     subject_ids=[subject_2026.id])
    assert result.is_error
    error = error_of(result)
    assert error["code"] == "bad_request", error
    message = error["message"]
    assert "2026" in message and "2027" in message, message
    assert "年度科目表" in message or "每一份" in message, message

    # 用对年度的 id ⇒ 能查到（同一张凭证不会因为 id 用错就串到别的科目上）
    right = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, year=2027,
                                    subject_ids=[subject_2027.id])
    assert right["total"] == 1
    assert right["items"][0]["voucher_date"] == "2027-03-05"


def test_search_vouchers_multi_subject_merges(db, book, make_voucher, ctx):
    """多科目：分别查再合并，去重后总数正确。"""
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    make_voucher("2026-10-09", dr=[("560206", "300.00", "电费")],
                 cr=[("1002", "300.00", "付电费")], posted=True)

    subjects = {s.code: s for s in db.query(Subject).filter_by(
        book_id=book.id, year=2026).all()}

    single = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                                    subject_ids=[subjects["560207"].id])
    assert single["total"] == 1

    both = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                                   subject_ids=[subjects["560207"].id,
                                                subjects["560206"].id])
    assert both["total"] == 2
    assert len(both["items"]) == 2
    # 排序与单科目查询同一套键：日期倒序
    assert both["items"][0]["voucher_date"] == "2026-10-09"


def test_search_vouchers_red_letter_totals_go_to_the_other_side(
        db, book, make_voucher, ctx):
    """红字（负数）算到对面去 —— 合计口径不能简单相加。

    借方红字（-100）实质上是贷方、贷方红字（-100）实质上是借方，所以这张
    冲销凭证的**两栏都是 100**（各自的红字跑到对面去了），而分录金额本身
    照原样保留符号（不改成正数）。
    """
    make_voucher("2026-10-08", dr=[("560207", "-100.00", "红字冲销")],
                 cr=[("1002", "-100.00", "红字冲销")], posted=True)

    item = _call(detail.search_vouchers, ctx=ctx, book_id=book.id)["items"][0]
    assert item["total_debit"] == "100.00"
    assert item["total_credit"] == "100.00"
    amounts = {e["subject_code"]: e["amount"] for e in item["entries"]}
    assert amounts["560207"] == "-100.00"     # 符号照原样保留
    assert amounts["1002"] == "-100.00"


def test_search_vouchers_amount_filter_is_per_entry(db, book, make_voucher, ctx):
    """min_amount 筛的是**单条分录**的金额，不是整张凭证的合计。

    第二张凭证的合计（2×300）是 600，但没有任何一条分录到 500 ⇒ 不该被捞出来。
    """
    make_voucher("2026-10-01", dr=[("560207", "1000.00", "大额")],
                 cr=[("1002", "1000.00", "付")], posted=True)
    make_voucher("2026-10-02", dr=[("560206", "300.00", "小额一")],
                 cr=[("1001", "300.00", "付")], posted=True)
    make_voucher("2026-10-02", dr=[("560206", "300.00", "小额二")],
                 cr=[("1001", "300.00", "付")], posted=True)

    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, min_amount="500")
    assert result["total"] == 1
    assert result["items"][0]["voucher_date"] == "2026-10-01"


def test_tool_callable_through_sdk(db, book, make_voucher, ctx):
    """端到端：工具能通过 MCP SDK 的正常调用路径跑通。

    这条走的是真路径 —— SDK 按 schema 校验入参、把 ``Context`` 注进来、
    再把返回值转成内容。直接调被装饰的函数只证明「函数体对」，证明不了
    「注册得对」（参数名对不上、注解解析不了，都会在这里才暴露）。
    """
    import asyncio

    from app.mcp.server import mcp_server, register_tools

    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    register_tools(mcp_server)
    result = asyncio.run(mcp_server.call_tool(
        "search_vouchers", {"book_id": book.id}, context=ctx))
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["total"] == 1
    assert payload["items"][0]["period_code"] == "2026-10"


# --------------------------------------------------------------------- 工具 5


def test_latest_payment_paid(db, book, make_voucher, ctx):
    make_voucher("2026-09-05", dr=[("560207", "12800.00", "9月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_keyword="房屋租赁费")
    assert result["found"] is True
    assert result["payment_date"] == "2026-09-05"
    assert result["amount"] == "12800.00"
    assert result["subject"]["code"] == "560207"
    assert result["counterparty"]["code"] == "1002"
    assert result["cash_flow_kind"] == "PAYMENT"
    assert result["status"] == "POSTED"
    assert result["period_code"] == "2026-09"
    assert "已付" in result["note"]


def test_latest_payment_accrued_only_is_not_paid(db, book, make_voucher, ctx):
    """借 费用 / 贷 应付账款 ⇒ **仅计提未付**，不能报成已付。"""
    make_voucher("2026-09-30", dr=[("560207", "12800.00", "9月租金")],
                 cr=[("2202", "12800.00", "计提租金")], posted=True)

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_keyword="房屋租赁费")
    assert result["found"] is False
    assert result["payment_date"] is None
    assert "仅计提未付" in result["note"]
    assert result["alternatives"][0]["kind"] == "ACCRUED"
    assert result["alternatives"][0]["counterparty"]["code"] == "2202"


def test_latest_payment_unposted_only(db, book, make_voucher, ctx):
    """有凭证但未记账 ⇒ 不是「已付」。"""
    make_voucher("2026-09-30", dr=[("560207", "12800.00", "9月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=False)

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_keyword="房屋租赁费")
    assert result["found"] is False
    assert "未记账" in result["note"]


def test_latest_payment_no_record(db, book, make_voucher, ctx):
    make_voucher("2026-09-05", dr=[("560206", "300.00", "电费")],
                 cr=[("1002", "300.00", "付电费")], posted=True)

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_keyword="房屋租赁费")
    assert result["found"] is False
    assert result["alternatives"] == []
    assert "没有付款记录" in result["note"]


def test_latest_payment_distinguishes_paid_and_received(db, book, make_voucher, ctx):
    """「房屋租赁费」（付出去）与「租金收入」（收到）要分得开。

    内置科目表里没有「租金收入」，这里用 5001 主营业务收入收款来对照：
    同一个关键字命中两个科目时，借方一侧与贷方一侧都要给出候选并分别标注。
    """
    make_voucher("2026-09-05", dr=[("560207", "12800.00", "付租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    make_voucher("2026-09-20", dr=[("1002", "5000.00", "收租金")],
                 cr=[("5001", "5000.00", "收租金")], posted=True)

    subjects = {s.code: s for s in db.query(Subject).filter_by(
        book_id=book.id, year=2026).all()}

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_id=subjects["560207"].id)
    assert result["found"] is True
    assert result["subject"]["code"] == "560207"
    assert result["counterparty"]["code"] == "1002"

    income = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_id=subjects["5001"].id)
    assert income["found"] is False      # 收到钱不是「付出去」
    assert income["alternatives"][0]["kind"] == "RECEIVED"
    assert income["alternatives"][0]["counterparty"]["code"] == "1002"


def test_latest_payment_alternatives_hold_older_candidates(db, book, make_voucher,
                                                           ctx):
    make_voucher("2026-06-05", dr=[("560207", "12800.00", "6月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    make_voucher("2026-09-05", dr=[("560207", "12800.00", "9月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_keyword="房屋租赁费")
    assert result["payment_date"] == "2026-09-05"
    assert result["alternatives"][0]["date"] == "2026-06-05"


def test_latest_payment_range_gate_rejects_huge_lookback(db, book, make_voucher,
                                                         ctx):
    """区间上限闸：跨太多个月 ⇒ 400 + 明确上限（不是让它把整本账拉走）。

    这道闸只有账本本身超过 ``mcp_max_range_months``（60）个月时才够得着 ——
    所以这里先把账套铺成 6 年（72 个期间），否则区间会被「账套起点」夹住，
    闸门永远不触发，测试也就成了摆设。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    for year in range(2027, 2032):
        ensure_periods(db, book, year)
        copy_chart(db, book.id, 2026, year)
    db.commit()

    make_voucher("2031-06-05", dr=[("560207", "12800.00", "租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                   subject_keyword="房屋租赁费",
                                   lookback_months=100)
    assert result.is_error is True
    error = error_of(result)
    assert error["code"] == "bad_request"
    assert "个月" in error["message"]

    # 区间收窄到 12 个月就能查 —— 闸门是「拆成多次查」，不是「不许查」。
    ok = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                                subject_keyword="房屋租赁费",
                                lookback_months=12)
    assert ok["found"] is True
    assert ok["payment_date"] == "2031-06-05"


def test_search_vouchers_year_without_periods_is_empty(db, book, ctx):
    """问一个没有期间的年度 ⇒ 空结果 + 说明（不是 500，也不是串数据）。"""
    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, year=2031)
    assert result["total"] == 0
    assert result["items"] == []
    assert any("2031" in n for n in result["notes"])


# --------------------------------------------------------------------- 工具 6


def test_check_period_payments_paid(db, book, make_voucher, ctx):
    make_voucher("2026-10-08", dr=[("560206", "3260.00", "10月电费")],
                 cr=[("1002", "3260.00", "付电费")], posted=True)

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2026-10",
                                           subject_keyword="水电费")
    assert result["period"] == "2026-10"
    assert result["paid"] is True
    assert result["accrued_only"] is False
    assert result["unposted_only"] is False
    assert result["paid_amount"] == "3260.00"
    assert result["paid_dates"] == ["2026-10-08"]
    assert result["records"][0]["counterparty"]["code"] == "1002"
    assert "已付" in result["note"]


def test_check_period_payments_accrued_only(db, book, make_voucher, ctx):
    make_voucher("2026-10-30", dr=[("560206", "3260.00", "10月电费")],
                 cr=[("2202", "3260.00", "计提电费")], posted=True)

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2026-10",
                                           subject_keyword="水电费")
    assert result["paid"] is False
    assert result["accrued_only"] is True
    assert result["unposted_only"] is False
    assert result["paid_amount"] == "0.00"
    assert "仅计提未付" in result["note"]


def test_check_period_payments_unposted_only(db, book, make_voucher, ctx):
    make_voucher("2026-10-08", dr=[("560206", "3260.00", "10月电费")],
                 cr=[("1002", "3260.00", "付电费")], posted=False)

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2026-10",
                                           subject_keyword="水电费")
    assert result["paid"] is False
    assert result["accrued_only"] is False
    assert result["unposted_only"] is True
    assert "未记账" in result["note"]
    # 默认不把未记账凭证列进 records[]
    assert result["records"] == []

    with_unposted = _call(detail.check_period_payments, 
        ctx=ctx, book_id=book.id, period_code="2026-10",
        subject_keyword="水电费", include_unposted=True)
    assert len(with_unposted["records"]) == 1
    assert with_unposted["records"][0]["status"] == "UNPOSTED"


def test_check_period_payments_no_record(db, book, make_voucher, ctx):
    make_voucher("2026-09-08", dr=[("560206", "3260.00", "9月电费")],
                 cr=[("1002", "3260.00", "付电费")], posted=True)

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2026-10",
                                           subject_keyword="水电费")
    assert result["paid"] is False
    assert result["accrued_only"] is False
    assert result["unposted_only"] is False
    assert result["records"] == []
    assert "没有" in result["note"]


def test_check_period_payments_flags_are_mutually_exclusive(db, book, make_voucher,
                                                             ctx):
    """三个标志**互斥**：任何一种情况下最多只有一个为真。"""
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "付租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    make_voucher("2026-10-30", dr=[("560207", "6000.00", "计提租金")],
                 cr=[("2202", "6000.00", "计提租金")], posted=True)

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2026-10",
                                           subject_keyword="房屋租赁费")
    flags = [result["paid"], result["accrued_only"], result["unposted_only"]]
    assert sum(1 for f in flags if f) <= 1
    # 已付优先：本期确实付了 ⇒ paid 为真、accrued_only 为假
    assert result["paid"] is True
    assert result["accrued_only"] is False
    assert result["unposted_only"] is False
    assert result["paid_amount"] == "12800.00"


def test_check_period_payments_by_year_and_month(db, book, make_voucher, ctx):
    make_voucher("2026-10-08", dr=[("560206", "3260.00", "10月电费")],
                 cr=[("1002", "3260.00", "付电费")], posted=True)

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           year=2026, month=10,
                                           subject_keyword="水电费")
    assert result["period"] == "2026-10"
    assert result["paid"] is True


def test_check_period_payments_locked_flag(db, book, make_voucher, ctx):
    from app.models import Period

    make_voucher("2026-10-08", dr=[("560206", "3260.00", "10月电费")],
                 cr=[("1002", "3260.00", "付电费")], posted=True)
    period = db.query(Period).filter_by(book_id=book.id, year=2026, month=10).one()
    period.locked = True
    db.commit()

    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2026-10",
                                           subject_keyword="水电费")
    assert result["locked"] is True


# --------------------------------------------------------------------- 工具 7


def test_ledger_balance_matches_api_balance(db, book, make_voucher, ctx):
    """MCP 的期末余额必须与界面接口**同行同值**。

    这是计划 §6.1 里最关键的一条：两边都委托
    ``ledger/queries.balance_sheet_of_accounts``，所以数值必然一致 ——
    这条用例把「同源」钉住，将来谁在 MCP 侧自己算一遍就会红。
    """
    from app.ledger import queries
    from app.models import Period

    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    period = db.query(Period).filter_by(book_id=book.id, year=2026, month=10).one()
    subjects = queries.subjects_for_periods(db, book.id, [period])
    api_rows, api_check = queries.balance_sheet_of_accounts(
        db, book.id, subjects, period, False, from_period=None)
    api_by_code = {r["code"]: r for r in api_rows}

    result = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                   period_code="2026-10")
    mcp_by_code = {r["code"]: r for r in result["rows"]}

    assert mcp_by_code["560207"]["closing"] == f"{api_by_code['560207']['closing']:.2f}"
    assert mcp_by_code["560207"]["period_debit"] == \
        f"{api_by_code['560207']['period_debit']:.2f}"
    assert mcp_by_code["1002"]["closing"] == f"{api_by_code['1002']['closing']:.2f}"
    assert result["check"]["period_balanced"] == api_check["period_balanced"]
    assert result["check"]["total_debit"] == f"{api_check['total_debit']:.2f}"


def test_ledger_balance_side_uses_sign_not_natural_direction(db, book, make_voucher,
                                                             ctx):
    """side 按**符号**判定，不是按科目自然方向反推。

    2001 短期借款的自然方向是**贷**，但这里给它记了一笔借方发生额（还钱），
    于是期末是借方余额 —— side 必须显示「借」，而 direction 仍然是科目的
    自然方向「CREDIT」。早期版本按自然方向判，会把这种行显示成「贷」，
    与试算平衡自相矛盾（引擎 ``display_side`` 的注释记的就是这个缺陷）。
    """
    make_voucher("2026-10-08", dr=[("2001", "1000.00", "归还借款")],
                 cr=[("1002", "1000.00", "还钱")], posted=True)

    result = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                   period_code="2026-10")
    row = [r for r in result["rows"] if r["code"] == "2001"]
    assert row, "科目表里应当有 2001 短期借款"
    assert row[0]["side"] == "借"          # 按符号
    assert row[0]["direction"] == "CREDIT"  # 科目的自然方向
    assert row[0]["closing"] == "1000.00"


def test_ledger_balance_subject_scope(db, book, make_voucher, ctx):
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    every = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                   period_code="2026-10")
    leaf = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                  period_code="2026-10", subject_scope="leaf")
    top = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                 period_code="2026-10", subject_scope="top")

    assert len(leaf["rows"]) <= len(every["rows"])
    assert len(top["rows"]) <= len(every["rows"])
    # 一级科目比全部科目少（科目表是多层的）
    assert len(top["rows"]) < len(every["rows"])
    # 试算合计不随档位变化
    assert leaf["check"] == every["check"]


def test_ledger_balance_range(db, book, make_voucher, ctx):
    from app.models import Period

    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)
    period = db.query(Period).filter_by(book_id=book.id, year=2026, month=10).one()

    result = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                   period_code="2026-10",
                                   from_period_code="2026-09")
    rent = [r for r in result["rows"] if r["code"] == "560207"][0]
    assert rent["period_debit"] == "12800.00"


def test_跨年科目级次不一致时回400而不是栈(db, book, ctx):
    """同一个编码在两个年度一个末级一个上级 ⇒ **400 + 可读错误**。

    计划 §5.3 的一格：跨年科目末级性不一致（``merge_by_code`` 抛
    ``ValueError``）⇒ 400 + 转成可读错误，不透出栈。

    **为什么这种数据不能「凑合算出来」。** 跨年归并是把两年的数相加，可
    上级那年的数**含它的下级**、下级又单独计一次 —— 相加既不是这个编码的
    总额，也不是它的发生额，试算平衡还会**重复计数**。引擎选择报错是对的：
    报一个说不清的数，比报一个错数更糟。

    这里守的是**报错这件事怎么出去**：必须是 agent 能读懂的 400（它知道该
    按年度分别查），而不是 500 加一段栈。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()

    # 2027 年给 560207 挂一个下级 ⇒ 2027 的 560207 不再是末级，
    # 而 2026 的 560207 仍是末级 ⇒ 同一编码跨年级次不一致。
    #
    # **顺序不能反。** 库层护栏 ``fn_subject_leaf_parent_guard`` 要求挂下级
    # 之前父科目**已经不是**末级（``is_leaf`` 由服务端按子级关系维护）——
    # 这正是写路径「加子科目」的两步：先把父级改成非末级，再插子行。
    parent_2027 = db.query(Subject).filter_by(
        book_id=book.id, year=2027, code="560207").one()
    parent_2027.is_leaf = False
    db.commit()
    db.add(Subject(book_id=book.id, year=2027, code="56020701", name="租金押金",
                    category="PROFIT", direction="DEBIT", level=4, is_leaf=True,
                    active=True, parent_id=parent_2027.id))
    db.commit()
    db.refresh(parent_2027)
    assert parent_2027.is_leaf is False, "挂了下级之后父科目不该还是末级"

    result = _call(detail.ledger_balance, ctx=ctx, book_id=book.id,
                                    period_code="2027-02",
                                    from_period_code="2026-12")
    assert result.is_error is True
    error = error_of(result)
    assert error["code"] == "bad_request", error
    assert "级次不一致" in error["message"], error
    # 不透出栈
    assert "Traceback" not in error["message"]
    assert ".py" not in error["message"], error


# --------------------------------------------------------------------- 权限


def test_tool_denies_book_without_grant(db, book, make_voucher, outsider_ctx):
    """没有该账套授权 ⇒ **结构化错误 forbidden**，不是异常、不是 500。"""
    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    result = _call(detail.search_vouchers, ctx=outsider_ctx, book_id=book.id)
    assert result.is_error is True
    error = error_of(result)
    assert error["code"] == "forbidden"
    assert "没有该账套的访问权限" in error["message"]


def test_every_tool_denies_book_without_grant(db, book, outsider_ctx):
    for call in (
        lambda: _call(detail.search_vouchers, ctx=outsider_ctx, book_id=book.id),
        lambda: _call(detail.latest_payment, ctx=outsider_ctx, book_id=book.id,
                                       subject_keyword="租金"),
        lambda: _call(detail.check_period_payments, ctx=outsider_ctx, book_id=book.id,
                                              period_code="2026-10",
                                              subject_keyword="租金"),
        lambda: _call(detail.ledger_balance, ctx=outsider_ctx, book_id=book.id),
    ):
        result = call()
        assert result.is_error is True
        assert error_of(result)["code"] == "forbidden"


def test_tool_reports_bad_period_as_readable_error(db, book, ctx):
    """期间不存在 ⇒ 400 + 可读文案（不是 500）。"""
    result = _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                           period_code="2030-01",
                                           subject_keyword="租金")
    assert result.is_error is True
    assert error_of(result)["code"] == "bad_request"


def test_tool_hides_unknown_book_behind_forbidden(db, ctx):
    """账套不存在 ⇒ 也是 403，不是 404。

    鉴权**先于**存在性检查（与 ``core/deps.get_my_book`` 同一顺序）：一个
    拿不到授权的主体不该靠「404 还是 403」探测出哪些账套存在。
    """
    result = _call(detail.search_vouchers, ctx=ctx, book_id=999999)
    assert result.is_error is True
    assert error_of(result)["code"] == "forbidden"


# --------------------------------------------------------------------- 只读


def test_detail_tools_do_not_write_business_tables(db, book, make_voucher, ctx):
    """MCP 读调用**不得**写业务表。

    读调用本身会写一条 ``mcp.read`` 审计（那是可观测性，不是业务数据），
    但凭证、分录、科目、期间四张表的行数必须纹丝不动。
    """
    from app.models import Entry, Period, Subject, Voucher

    make_voucher("2026-10-08", dr=[("560207", "12800.00", "10月租金")],
                 cr=[("1002", "12800.00", "付租金")], posted=True)

    def counts():
        return (db.query(Voucher).count(), db.query(Entry).count(),
                db.query(Subject).count(), db.query(Period).count())

    before = counts()
    _call(detail.search_vouchers, ctx=ctx, book_id=book.id)
    _call(detail.latest_payment, ctx=ctx, book_id=book.id, subject_keyword="房屋租赁费")
    _call(detail.check_period_payments, ctx=ctx, book_id=book.id,
                                  period_code="2026-10", subject_keyword="水电费")
    _call(detail.ledger_balance, ctx=ctx, book_id=book.id, period_code="2026-10")
    db.expire_all()
    assert counts() == before


def test_tools_are_registered():
    """四个工具都要能被 MCP 服务列出来。"""
    import asyncio

    from app.mcp.server import mcp_server, register_tools

    register_tools(mcp_server)
    names = [t.name for t in asyncio.run(mcp_server.list_tools())]
    for expected in ("search_vouchers", "latest_payment",
                     "check_period_payments", "ledger_balance"):
        assert expected in names
