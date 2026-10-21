"""MCP 只读数据服务。

给外部 AI agent（Claude Desktop / Cherry Studio / Dify / 自研 client）一个
查询账套数据的入口。**只读**：工具集里不存在写工具，令牌自身也只有读语义。

设计上三条要紧的：

* **口径不重写**：余额、发生额、损益、现金类判定全部委托既有的账簿引擎与
  报表求值器。MCP 不写第二套聚合（本仓审计第 7.3 节记过「同一口径两份
  实现」的病根）。
* **可见性继承用户**：令牌绑定一个真实用户，账套范围 100% 来自
  ``sys_user_book``。用户侧权限一变，agent 侧立刻同步。
* **令牌永不过期**：所以吊销是唯一安全阀，必须配齐审计与限流。
"""
from __future__ import annotations

from app.mcp.asgi import MCP_PATH, principal_from_request
from app.mcp.auth import McpPrincipal
from app.mcp.errors import (
    McpAuthError,
    McpBadRequest,
    McpError,
    McpForbiddenError,
    McpInternalError,
    McpNotFoundError,
    McpRateLimited,
)
from app.mcp.server import build_app, mcp_server

__all__ = [
    "MCP_PATH", "McpAuthError", "McpBadRequest", "McpError",
    "McpForbiddenError", "McpInternalError", "McpNotFoundError",
    "McpPrincipal", "McpRateLimited", "build_app", "mcp_server",
    "principal_from_request",
]
