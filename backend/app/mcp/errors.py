"""MCP 层的错误：分类、脱敏、以及给 agent 的结构化回执。

**为什么 MCP 层要自己写一套脱敏。**
父应用 ``main.py`` 的 ``dbapi_error_handler`` **不覆盖** MCP 子应用 ——
子应用是独立的一层 ASGI 栈，异常处理器是挂在父应用 ``app`` 上的，
父应用的处理器看不到子应用里冒出来的异常。于是 DB 异常原文（表名、列名、
约束名、重复键的值）会一路走到 agent 面前。

这份模块复刻 ``main.py`` 的同一套判据：

* **我们自己写的中文文案** ⇒ 可以回给 agent（那是给人看的提示）；
* **PG 自带的英文违例文案** ⇒ 换成一句脱敏提示（那是库结构）；
* **其它异常** ⇒ 一句「内部错误（请求号 X）」，原文只进服务端日志。

判据仍然是「文案里有没有汉字」，而不是维护一张 sqlstate 白名单 ——
理由与 ``main.py`` 相同：sqlstate 是有限的几个，而**文案来源**才是真正
要管的东西。

**错误的形状。**
工具出错时回 ``CallToolResult(is_error=True)``，``structured_content``
是 ``{"code": ..., "message": ...}``：

* ``code``：机器可读的短标识（``forbidden`` / ``not_found`` / …），
  agent 可以据此分支；
* ``message``：中文、面向人、不含库结构。

这样 agent 拿到的是一个**可自我修正**的结构化回执，而不是一段 500 或
一句堆栈。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException
from mcp.types import CallToolResult, TextContent

log = logging.getLogger("cwq")


class McpError(Exception):
    """MCP 工具错误。``code`` 给机器，``message`` 给人。"""

    code = "error"
    status_code = 400

    def __init__(self, message: str, *, code: Optional[str] = None,
                 status_code: Optional[int] = None, detail: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail or {}

    def to_result(self) -> CallToolResult:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return CallToolResult(
            content=[TextContent(type="text", text=self.message)],
            structured_content=payload,
            is_error=True,
        )


class McpAuthError(McpError):
    """令牌缺失/无效/已吊销/已过宽限期/用户停用。

    **不区分具体原因** —— 「不存在」「已吊销」「已过宽限期」回一样的文案，
    否则一个能猜的接口就成了令牌状态的探测器。
    """

    code = "unauthorized"
    status_code = 401


class McpForbiddenError(McpError):
    """没有该账套的访问权限。文案与 ``core/deps.py`` 逐字一致。"""

    code = "forbidden"
    status_code = 403


class McpNotFoundError(McpError):
    code = "not_found"
    status_code = 404


class McpBadRequest(McpError):
    """入参不合法：期间不存在、年度不存在、区间超限、科目跨年……"""

    code = "bad_request"
    status_code = 400


class McpRateLimited(McpError):
    code = "rate_limited"
    status_code = 429


class McpInternalError(McpError):
    code = "internal_error"
    status_code = 500


# --------------------------------------------------------------------- 脱敏

#: PG 自带约束违例的 sqlstate → 中文脱敏提示。
#:
#: 与 ``main.py`` 的 ``_generic_constraint_detail`` **同一张表、同一批文案** ——
#: 两处各写一份的话，改了一处忘了另一处，泄露就从那一处回来。
_GENERIC_CONSTRAINT_DETAIL = {
    "23505": "数据已存在或被引用（违反唯一性约束），请检查是否重复提交。",
    "23503": "引用了不存在的关联数据，请检查所选科目/账套是否仍然有效。",
    "23502": "必填字段缺失（违反非空约束）。",
    "23514": "数据不满足数据库层的业务约束，请检查输入内容。",
    "22001": "提交的内容超出了字段允许的长度，请缩短后再试。",
}

#: sqlstate → 4xx 的集合。与 ``main.py`` 同一份。
_CONSTRAINT_SQLSTATES = ("23514", "23505", "23503", "23502", "22001")

#: PG 的「语句超时」sqlstate。
#:
#: 工具调用设了 ``statement_timeout``（``mcp_statement_timeout_ms``），慢
#: 查询会在**库层**被掐掉。PG 抛的是 ``57014``（query_canceled），原文是
#: 「canceling statement due to statement timeout」—— 那是 PG 的话，而且
#: 带上了具体时长；这里换成一句中文，让 agent 知道是「太慢了，缩小区间
#: 再试」而不是「系统出错了」。
_QUERY_CANCELED = "57014"


def _is_own_guardrail_text(message: str) -> bool:
    """这句违例文案是不是**我们自己写的中文护栏文案**。

    判据与 ``main.py`` 相同：**里面有没有汉字**。
    ``db_init.sql`` 里的 ``RAISE EXCEPTION '已记账的凭证不允许删除'`` 有汉字
    ⇒ 是写给人看的提示；PG 自带的 ``duplicate key value violates unique
    constraint "uq_..."`` 没有汉字 ⇒ 里面带的是**库结构**，不能回给 agent。
    """
    return any("一" <= ch <= "鿿" for ch in message)


def _generic_constraint_detail(sqlstate: str | None) -> str:
    return _GENERIC_CONSTRAINT_DETAIL.get(
        sqlstate or "", "数据不满足数据库层的约束，请检查输入内容。")


def dbapi_to_mcp_error(exc: Exception, *, request_id: str = "-") -> McpError:
    """把数据库异常翻成一条**不含库结构**的 MCP 错误。

    与 ``main.py`` 的 ``dbapi_error_handler`` 同一个分流：

    * 约束类 sqlstate 且文案是**我们自己写的中文** ⇒ 400 + 原文；
    * 约束类 sqlstate 但文案是 PG 自带的 ⇒ 400 + 脱敏替身；
    * 其它 ⇒ 500 + 请求号，**不回原文**。
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    message = (getattr(diag, "message_primary", None) or str(orig)).strip()

    if sqlstate in _CONSTRAINT_SQLSTATES:
        log.warning("MCP 数据库约束拒绝 rid=%s sqlstate=%s %s",
                    request_id, sqlstate, message[:200])
        detail = (message[:500] if _is_own_guardrail_text(message)
                  else _generic_constraint_detail(sqlstate))
        return McpBadRequest(detail, code="constraint_violation")
    if sqlstate == _QUERY_CANCELED:
        log.warning("MCP 查询超时 rid=%s sqlstate=%s", request_id, sqlstate)
        return McpBadRequest(
            "这次查询太慢，已经被服务端中止；请缩小查询区间或减少返回条数后重试",
            code="query_timeout")
    return internal_error(exc, request_id=request_id)


def internal_error(exc: Exception, *, request_id: str = "-") -> McpError:
    """兜底：一句脱敏提示 + 请求号。**原文只进服务端日志。**"""
    log.exception("MCP 内部异常 rid=%s：%s", request_id, str(exc)[:500])
    return McpInternalError(
        f"服务内部错误（请求号 {request_id}），请联系管理员并提供该请求号")


def to_mcp_error(exc: Exception, *, request_id: str = "-") -> McpError:
    """把**任何**异常映射成一条 MCP 错误。

    映射表：

    ==========================  ==========================================
    异常                        结果
    ==========================  ==========================================
    ``McpError``                原样（我们自己抛的，已经是脱敏过的）
    ``McpPeriodError``          400 + 原文（``caliber`` 抛的「期间/年度层面
                               的输入问题」，文案本身就是给人看的）
    ``HTTPException``           按状态码映射；``detail`` 是我们自己写的中文
    ``ValueError``              400 + 原文（``find_period`` / ``money``
                               / ``merge_by_code`` 抛的都是我们写的中文）
    ``DBAPIError``              走 ``dbapi_to_mcp_error``
    其它                        500 + 请求号
    ==========================  ==========================================

    **``McpPeriodError`` 为什么在这里收口。** ``caliber`` 里
    ``find_period_by_code`` / ``check_range`` / ``parse_date`` 抛的都是它，
    文案是「这个账套从 2026-03 起才有会计期间」这种**对 agent 有用**的话。
    不认它的话，一次「期间不存在」会变成 500 —— agent 拿到的只是一句
    「服务内部错误」，什么也改不了，只能重试或者放弃。
    """
    if isinstance(exc, McpError):
        return exc
    # **延迟导入**：``caliber`` 与本模块互不依赖模块级引用（``caliber`` 只在
    # 方法体里 ``from app.mcp.errors import McpBadRequest``），这里若改成
    # 模块级导入就多出一条环来的可能 —— 而它只在**运行时**需要一次。
    from app.mcp.caliber import McpPeriodError

    if isinstance(exc, McpPeriodError):
        return exc.to_error()
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "请求不被允许"
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            400: "bad_request",
            409: "conflict",
            429: "rate_limited",
        }.get(exc.status_code or 400, "error")
        cls = {
            401: McpAuthError,
            403: McpForbiddenError,
            404: McpNotFoundError,
            429: McpRateLimited,
        }.get(exc.status_code or 400, McpBadRequest)
        return cls(detail, code=code, status_code=exc.status_code or 400)
    if isinstance(exc, ValueError):
        return McpBadRequest(str(exc))
    # SQLAlchemy 的 DBAPIError 家族。
    if exc.__class__.__name__ in ("DBAPIError", "IntegrityError",
                                  "OperationalError", "ProgrammingError",
                                  "DataError", "InterfaceError",
                                  "InternalError", "NotSupportedError",
                                  "StatementError"):
        return dbapi_to_mcp_error(exc, request_id=request_id)
    return internal_error(exc, request_id=request_id)


__all__ = [
    "McpAuthError", "McpBadRequest", "McpError", "McpForbiddenError",
    "McpInternalError", "McpNotFoundError", "McpRateLimited",
    "dbapi_to_mcp_error", "internal_error", "to_mcp_error",
]
