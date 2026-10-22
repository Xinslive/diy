"""**真实 MCP 客户端**的端到端联调。

**为什么这一组与其它 MCP 用例不同。**
别的用例要么直接调工具函数（绕过了传输层），要么用裸 JSON-RPC 打端点
（绕过了 SDK 客户端）。这一组走的是**第三方 agent 客户端真正会走的那条路**：

    MCP SDK 客户端 → Streamable HTTP → /api/mcp → 鉴权中间件 → 工具

它守的是**协议层**的事：``initialize`` 握手能不能成、``tools/list`` 能不能拿到
完整的清单与参数 schema、``tools/call`` 的回包结构客户端解析得了、SSE 回包
（而不是 JSON 回包）客户端读得懂。

**这些是「端点能通」与「端点真的能用」之间的差别。** 一个自造的 JSON-RPC
请求能成功，不代表真实客户端能连上 —— 握手顺序、协议版本、Accept 头、回包
的 content-type，任一处对不上，客户端报出来的都是「连接失败」这种没法定位的
话。

**为什么用 ASGITransport 而不是起一个真的 uvicorn。** 起真服务器要占端口、
要等启动、还会和别的用例抢端口；这里走 ASGI 直连，跑得快且不 flaky ——
**协议层的东西（握手、schema、回包格式）与传输是不是 TCP 无关**。
生命周期由 ``client`` 夹具（``TestClient``）带着跑，MCP 的 session manager
因此已经启动（没启动的话首个请求会报「Task group is not initialized」）。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def token(client, auth, admin_user) -> str:
    """签一个真令牌（走管理面，拿到的是明文）。"""
    from tests.test_mcp_auth import issue

    return issue(client, auth, admin_user.id)


def _run_client(url: str, token: str, tool_args: dict):
    """用 SDK 客户端连一次端点，跑完 initialize → tools/list → tools/call。"""
    import anyio
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from app.main import app

    async def _main():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (
                read_stream, write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    call = await session.call_tool("list_periods", tool_args)
                    return init, tools, call

    return anyio.run(_main)


def test_真实客户端能握手并拿到完整工具清单(token, book):
    """initialize → tools/list → tools/call 一步都不能断。"""
    init, tools, call = _run_client(
        "http://testserver/api/mcp", token, {"book_id": book.id})

    # 握手结果：客户端至少要能知道对端是什么
    assert init.server_info.name
    assert init.protocol_version

    # 工具清单：10 个，一个不多一个不少，每个都有参数 schema
    names = {t.name for t in tools.tools}
    assert names == {
        "list_books", "list_periods", "find_subjects", "search_vouchers",
        "latest_payment", "check_period_payments", "ledger_balance",
        "income_expense_trend", "render_report", "accounting_context",
    }
    for tool in tools.tools:
        assert tool.description, f"工具 {tool.name} 没有说明"
        assert tool.input_schema, f"工具 {tool.name} 没有参数 schema"

    # 工具调用：结构化内容客户端能解析
    assert not call.is_error
    payload = call.structured_content
    assert payload["caliber"] == "period"
    assert payload["book_id"] == book.id


def test_真实客户端拿到的是结构化错误而不是异常(client, auth, token, book, db):
    """工具报错时，客户端拿到的是 ``is_error`` + 结构化 code/message。

    **这条最容易被忽略**：服务端抛异常的话，SDK 客户端收到的是一句
    「Error executing tool X: 」，agent 拿不到「是 403 还是期间不存在」，
    于是只能瞎猜或者干脆放弃。
    """
    from app.models import SysUser, UserBook

    outsider = SysUser(username="mcp_e2e_outsider", password_hash="x",
                       real_name="没有授权的用户", active=True, is_admin=False)
    db.add(outsider)
    db.flush()
    db.commit()

    # 这个用户没有这个账套的授权
    assert db.query(UserBook).filter_by(
        user_id=outsider.id, book_id=book.id).count() == 0

    # 给外人签一个令牌：它是真令牌，但看不到这本账
    from tests.test_mcp_auth import issue

    outsider_token = issue(client, auth, outsider.id)

    init, tools, call = _run_client(
        "http://testserver/api/mcp", outsider_token, {"book_id": book.id})
    assert call.is_error, "没授权的调用应当回结构化错误"
    payload = call.structured_content
    # **错误码在 ``error`` 里**（``{code, message, detail}``）。顶层没有
    # ``code``/``message`` —— 那是旧形状，出错时回的结构对不上工具自己声明的
    # outputSchema，客户端会抛协议错 -32602（评估报告 P0-2）。
    error = payload["error"]
    assert error["code"] == "forbidden", payload
    assert "没有该账套的访问权限" in error["message"]


def _flatten(exc: BaseException) -> str:
    """把异常组摊平成一段文本。

    SDK 客户端的错误是包在 ``ExceptionGroup`` 里的（anyio 的任务组会把子异常
    收进一个组），只看最外层那句「unhandled errors in a TaskGroup」什么也
    断言不了。
    """
    parts = [f"{type(exc).__name__}: {exc}"]
    for sub in getattr(exc, "exceptions", ()) or ():
        parts.append(_flatten(sub))
    return " | ".join(parts)


def test_真实客户端在没有令牌时连不上(token, book):
    """没有令牌 ⇒ 401。**握手之前**就被挡下 —— ``tools/list`` 也要挡。

    **为什么要自己记状态码。** SDK 客户端把任何非 2xx 都翻成一句
    「Server returned an error response」，状态码在它那一层已经丢了 ——
    所以这里用 httpx 的响应钩子把服务端**真实回的**状态码记下来再断言。
    免得「401」与「404」「500」在客户端这一侧长得一模一样。
    """
    import anyio
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from app.main import app

    seen: list[int] = []

    async def _record(response):
        seen.append(response.status_code)

    async def _main():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver",
            event_hooks={"response": [_record]},
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/api/mcp", http_client=http_client,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

    with pytest.raises(BaseException) as exc:
        anyio.run(_main)
    assert seen and all(code == 401 for code in seen), (
        f"应当每个请求都是 401，实际是 {seen}；异常：{_flatten(exc.value)}")
