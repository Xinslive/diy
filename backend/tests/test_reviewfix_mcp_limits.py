"""审查修复：MCP 面的三处边界（H-03 / L-26 / L-27）。

**H-03（高）：``search_vouchers`` 的 ``subject_ids`` 没有长度上限。**

多科目路径是「按科目分别查、再合并到同一个字典」，而每个科目各自取满合并
上限张凭证（还带着全部分录的对象图）。以前 ``MAX_SUBJECTS`` **只管关键词
路径**，显式传 ``subject_ids`` 是一条**没有闸**的路 —— 一次调用就能把内存
放大成一次拒绝服务。

这里钉住三件事：

1. 显式传的 ``subject_ids`` 超过上限 ⇒ **结构化 400**（不是把数据全拉出来）；
2. 合并集是**总量闸**：合并起来超了与单个科目就超了，错误信息要能分清；
3. 限额**取自配置**（``mcp_max_subjects`` / ``mcp_max_merge_vouchers``），
   而不是模块里的常量 —— 配置才是唯一的口径来源。

**L-26：MCP 限流是「每令牌 × 每进程」的内存计数，换一枚令牌就换一个桶。**

**L-27：``mcp_max_range_months`` 在 ``search_vouchers`` 上永远不触发。**
"""
from __future__ import annotations

import pytest
from mcp.server.mcpserver import Context
from mcp.server.context import ServerRequestContext
from mcp.types import CallToolResult
from starlette.requests import Request

from app.core.config import settings
from app.core.db import SessionLocal
from app.mcp.asgi import PRINCIPAL_KEY
from app.mcp.auth import McpPrincipal
from app.mcp.tools import detail
from app.models import Subject
from tests.mcp_helpers import error_of


def _ctx(principal: McpPrincipal) -> Context:
    scope = {
        "type": "http", "method": "POST", "path": "/api/mcp", "headers": [],
        "state": {PRINCIPAL_KEY: principal},
    }
    request = Request(scope)
    return Context(request_context=ServerRequestContext(
        session=None, lifespan_context=None, protocol_version="2025-06-18",
        method="tools/call", request=request))


@pytest.fixture
def principal(admin_user) -> McpPrincipal:
    return McpPrincipal(user_id=admin_user.id, username=admin_user.username,
                         token_id=0, token_name="test")


@pytest.fixture
def ctx(principal) -> Context:
    return _ctx(principal)


def _call(fn, **kwargs):
    """成功时摊成字典；出错时原样返回 ``CallToolResult``。"""
    result = fn(**kwargs)
    if isinstance(result, CallToolResult):
        return result
    return result.model_dump() if hasattr(result, "model_dump") else result


def _expect_error(result) -> dict:
    """断言这一步返回了结构化错误，并取出 ``error`` 段。"""
    assert isinstance(result, CallToolResult) and result.is_error, f"本应报错：{result!r}"
    return error_of(result)


def _leaf_ids(db, book_id: int, year: int = 2026) -> list[int]:
    return [row.id for row in db.query(Subject).filter_by(
        book_id=book_id, year=year).order_by(Subject.code).all()]


# --------------------------------------------------------------------- H-03


def test_显式传入的科目个数超过上限时报400而不是把数据全拉出来(db, book, ctx, monkeypatch):
    """显式传 ``subject_ids`` 也有闸 —— 以前这条路是没有闸的。

    报错必须说清「上限是多少、现在传了多少、该怎么办」。
    """
    ids = _leaf_ids(db, book.id)
    assert len(ids) > 2, "这个账套的科目太少，测不出闸"
    monkeypatch.setattr(settings, "mcp_max_subjects", 3)

    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                    subject_ids=ids[:5])

    error = _expect_error(result)
    assert error["code"] == "bad_request", error
    message = error["message"]
    assert "3" in message and "5" in message, message
    assert "关键字" in message or "科目" in message, message


def test_科目个数闸与关键词路径是同一个口径(db, book, ctx, monkeypatch):
    """配置改小之后，**两条路径**一起变 —— 不是只管其中一条。

    关键词路径在 ``latest_payment`` 上（``search_vouchers`` 的 keyword 搜的是
    凭证文本，不是科目名），它以前就受 ``MAX_SUBJECTS`` 限制；现在两条路径
    读的是**同一个配置**。
    """
    ids = _leaf_ids(db, book.id)
    monkeypatch.setattr(settings, "mcp_max_subjects", 1)

    # 关键词路径：命中科目数超过同一个上限 ⇒ 同样是 400。
    keyword_result = _call(detail.latest_payment, ctx=ctx, book_id=book.id,
                            subject_keyword="费")
    assert _expect_error(keyword_result)["code"] == "bad_request"

    # 显式路径：同样的上限。
    explicit = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                      subject_ids=ids[:2])
    assert _expect_error(explicit)["code"] == "bad_request"


def test_合并集是总量闸_合并起来超了与单个科目超了要能分清(db, book, make_voucher, ctx, monkeypatch):
    """合并上限是**总量**闸，错误信息要分清两种情形。

    * **合并起来超了**：每个科目单独看都没超，是并集太大；
    * **单个科目就超了**：与合并无关，缩小这个科目的区间才行。
    """
    monkeypatch.setattr(settings, "mcp_max_merge_vouchers", 1)
    monkeypatch.setattr(settings, "mcp_max_subjects", 60)

    # 两个科目各有一张凭证：单个都没超，**合并起来**超了。
    make_voucher("2026-10-08", dr=[("560207", "10.00", "甲")], cr=[("1002", "10.00", "甲")], posted=True)
    make_voucher("2026-10-09", dr=[("560206", "20.00", "乙")], cr=[("1002", "20.00", "乙")], posted=True)
    subjects = {row.code: row.id for row in db.query(Subject).filter_by(book_id=book.id, year=2026).all()}

    merged = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                    subject_ids=[subjects["560207"], subjects["560206"]])
    message = _expect_error(merged)["message"]
    assert "合并" in message, message
    assert "减少科目" in message or "缩小日期范围" in message, message


def test_单个科目就超了时报的是那个科目(db, book, make_voucher, ctx, monkeypatch):
    """单个科目的凭证数就超过合并上限 ⇒ 错误里要点名那个科目。"""
    monkeypatch.setattr(settings, "mcp_max_merge_vouchers", 2)
    make_voucher("2026-10-08", dr=[("560207", "10.00", "甲")], cr=[("1002", "10.00", "甲")], posted=True)
    make_voucher("2026-10-09", dr=[("560207", "20.00", "乙")], cr=[("1002", "20.00", "乙")], posted=True)
    make_voucher("2026-10-10", dr=[("560207", "30.00", "丙")], cr=[("1002", "30.00", "丙")], posted=True)
    subjects = {row.code: row.id for row in db.query(Subject).filter_by(book_id=book.id, year=2026).all()}

    # 至少两个科目才会走「分别查再合并」那条路。
    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id,
                    subject_ids=[subjects["560207"], subjects["560206"]])
    message = _expect_error(result)["message"]
    assert str(subjects["560207"]) in message, message
    assert "缩小" in message, message


# --------------------------------------------------------------------- L-26


def test_限流按用户计数_换一枚令牌不换桶(client, auth, admin_user, monkeypatch):
    """同一用户的第二枚令牌**不换桶** —— 换令牌就换桶等于限流形同虚设。"""
    from app.mcp import auth as auth_mod
    from tests.test_mcp_auth import issue, mcp_post

    auth_mod.reset_rate_limits()
    monkeypatch.setattr(auth_mod.settings, "mcp_max_calls_per_hour", 2)

    busy = issue(client, auth, admin_user.id, name="忙令牌")
    other = issue(client, auth, admin_user.id, name="闲令牌")
    assert busy != other

    for _ in range(2):
        assert mcp_post(client, busy, "tools/list").status_code == 200
    # 忙令牌撞上限。
    assert mcp_post(client, busy, "tools/list").status_code == 429
    # **同一用户的另一枚令牌也该被挡住** —— 桶是按用户开的。
    assert mcp_post(client, other, "tools/list").status_code == 429

    auth_mod.reset_rate_limits()


# --------------------------------------------------------------------- L-27


def test_区间上限只约束跨期查询_按年查询天然更严(db, book, make_voucher, ctx):
    """``search_vouchers`` 按年取数 ⇒ 那条闸不适用，但**要说清楚**。

    这不是「把上限放宽来让它生效」：按年查询的窗口最多 12 个月，本来就比
    60 个月更严。工具的 ``notes`` 里必须写明这一点，免得 agent 以为闸没生效。
    """
    make_voucher("2026-10-08", dr=[("560207", "10.00", "甲")], cr=[("1002", "10.00", "甲")], posted=True)

    result = _call(detail.search_vouchers, ctx=ctx, book_id=book.id, year=2026)
    assert not isinstance(result, CallToolResult), result
    notes = " ".join(result["notes"])
    assert "按年度取数" in notes or "按年查询" in notes, notes
    assert "mcp_max_range_months" in notes or "跨期查询" in notes, notes
