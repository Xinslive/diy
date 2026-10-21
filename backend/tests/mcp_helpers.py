"""MCP 工具的测试助手。

**为什么要有这一层。**
MCP 工具是**同步函数**，由 SDK 在工作线程里调用。测试要直接调它们，就得
自己拼出 SDK 注入的那两样东西：

1. 一个真的 ``Context``（SDK 按**类型标注**找它，不是按参数名）；
2. 挂在请求 scope 上的**主体**（鉴权中间件放进去的）。

拼错了会得到一个很有迷惑性的失败：``ctx`` 是 ``None``、或主体取不到，
于是每个用例都报「令牌无效」—— 而真正的问题在测试夹具里。

所以这里把拼装过程收成一处，测试只管调。
"""
from __future__ import annotations

from typing import Any, Optional

from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult
from pydantic import BaseModel
from starlette.requests import Request

from app.mcp.asgi import PRINCIPAL_KEY
from app.mcp.auth import McpPrincipal


def make_principal(user_id: int = 1, username: str = "mcp_tester",
                   token_id: int = 1, token_name: str = "测试 agent") -> McpPrincipal:
    """造一个主体。"""
    return McpPrincipal(user_id=user_id, username=username,
                        token_id=token_id, token_name=token_name)


def make_ctx(principal: Optional[McpPrincipal] = None, *,
             request_id: str = "mcp-test-req") -> Context:
    """造一个真的 MCP ``Context``，其请求 scope 上挂着主体。"""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/mcp",
        "raw_path": b"/api/mcp",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "headers": [],
        "state": {},
    }
    scope["state"][PRINCIPAL_KEY] = principal
    scope["state"]["request_id"] = request_id
    request = Request(scope)
    return Context(request_context=ServerRequestContext(
        session=None,           # type: ignore[arg-type]
        lifespan_context=None,  # type: ignore[arg-type]
        protocol_version="2025-06-18",
        method="tools/call",
        request=request,
    ))


def tool_fn(name: str):
    """按名字取到已注册的工具函数。

    工具是模块级函数（``@mcp_tool`` 装饰过），装饰器保留了原函数的名字，
    所以可以直接按名字找。
    """
    from app.mcp.tools import analysis, detail, navigation

    for module in (navigation, detail, analysis):
        fn = getattr(module, name, None)
        if fn is not None:
            return fn
    raise KeyError(f"没有名为 {name!r} 的工具")


def call_tool(name: str, principal: Optional[McpPrincipal] = None, **args):
    """调用一个工具，返回它的出参。

    **出参现在是一个 Pydantic 模型**（``app/mcp/schemas.py``）—— 助手在这里
    把它摊成字典再返回，这样用例读起来还是 ``result["caliber"]``，与模型化
    之前一模一样（模型化是为了给 agent 发布 JSON Schema 与结构化输出，
    不是为了让用例改写法）。

    出错时（工具返回了 ``is_error`` 的 ``CallToolResult``）**抛**
    ``McpToolFailure``，让用例里的断言读起来像普通异常而不是一堆字典取值。
    """
    fn = tool_fn(name)
    ctx = make_ctx(principal)
    result = fn(ctx=ctx, **args)
    # **顺序要紧**：``CallToolResult`` 自己也是 Pydantic 模型，先判它。
    if isinstance(result, CallToolResult):
        if result.is_error:
            raise McpToolFailure(name, result)
        return result
    if isinstance(result, BaseModel):
        return result.model_dump()
    return result


class McpToolFailure(AssertionError):
    """工具返回了结构化错误。"""

    def __init__(self, tool: str, result: CallToolResult):
        payload = result.structured_content or {}
        self.tool = tool
        self.code = payload.get("code", "")
        self.message = payload.get("message", "") or (result.content[0].text
                                                     if result.content else "")
        super().__init__(f"工具 {tool} 返回错误：[{self.code}] {self.message}")


def call_tool_expect_error(name: str, principal: Optional[McpPrincipal] = None,
                            **args) -> McpToolFailure:
    """调用一个工具并**期望**它返回结构化错误。"""
    fn = tool_fn(name)
    ctx = make_ctx(principal)
    result = fn(ctx=ctx, **args)
    assert isinstance(result, CallToolResult) and result.is_error, (
        f"工具 {name} 本应报错，却返回了 {result!r}")
    return McpToolFailure(name, result)


__all__ = [
    "McpToolFailure", "call_tool", "call_tool_expect_error", "make_ctx",
    "make_principal", "tool_fn",
]
