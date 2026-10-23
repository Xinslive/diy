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

判据是「**有汉字、且不是 PG 的固定话术**」，而不是维护一张 sqlstate 白名单 ——
理由与 ``main.py`` 相同：sqlstate 是有限的几个，而**文案来源**才是真正
要管的东西。

**为什么"只看有没有汉字"不够**（审查补漏）：PG 的违例原文里会夹带中文
**数据**（重复键的值可能是凭证号「记0001」）。只判汉字的话，带着中文数据的
23505 原文（表名、约束名、**重复键的值**）会被当成自家提示原样回给 agent。
所以除了汉字，还要排除 PG 那几套固定英文模板（``_PG_VIOLATION_MARKERS``）。

**错误的形状。**
工具出错时回 ``CallToolResult(is_error=True)``，``structured_content``
是**一份符合该工具 ``outputSchema`` 的结构体**：

* ``error.code``：机器可读的短标识（``forbidden`` / ``not_found`` / …），
  agent 可以据此分支；
* ``error.message``：中文、面向人、不含库结构；
* ``error.detail``：原始异常的摘要（类型名 + 消息 + 上下文），让「为什么
  出错」在回执里就看得到；
* ``caliber="error"`` + ``notes=[message]``：让回执**同时**满足
  「机器读 ``error``」与「人读 ``notes``」。

**为什么不能回 ``{"code","message"}``**（2026-10-22 评估报告 P0-2 的根因）：
客户端会拿 ``tools/list`` 里声明的 ``outputSchema`` 去校验
``structuredContent``，少一个必填字段就直接抛协议错 ``-32602`` ——
**真正的错误原因被整个吞掉**，调用方只看到「结构不匹配」。

这样 agent 拿到的是一个**可自我修正**的结构化回执，而不是一段 500 或
一句堆栈。
"""
from __future__ import annotations

import logging
import os
import traceback
from typing import Any, Optional, Union, get_args, get_origin

from fastapi import HTTPException
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

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

    def to_result(self, output_model: Optional[type[BaseModel]] = None) -> CallToolResult:
        """出错时的回执。

        **``structured_content`` 必须符合这个工具声明的 ``outputSchema``。**

        客户端会拿 ``tools/list`` 里声明的 ``outputSchema`` 去校验
        ``structuredContent``。出错时若回的是 ``{"code","message"}`` 这种
        「另一个形状」，严格的客户端会直接抛协议错 ``-32602``
        （"missing required property 'caliber'"）—— **真正的错误原因被
        吞掉，调用方只看到「结构不匹配」**，排查无从下手。

        所以这里按工具的出参模型拼一个**形状对得上**的结构体：
        必填字段补上类型正确的占位值，错误本身放在 ``error`` 字段里。
        ``notes[]`` 同时写一句人话，``content`` 文本段写原始异常。

        **连出参模型都认不出来时**（理论上不会：工具模块刻意不写
        ``from __future__ import annotations``），退回**基类契约**
        ``McpResult`` 也要拼一份合规结构体 —— 退回旧的 ``{code,message}``
        就等于把 ``-32602`` 又放回来了，而那正是这一段要根治的东西。
        """
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        # 函数内导入：``schemas`` 不依赖本模块，但工具层两边都引，
        # 模块加载期就把依赖关系定死没有好处。
        from app.mcp.schemas import McpResult

        structured = error_payload(output_model or McpResult, self.code,
                                   self.message, self.detail)
        return CallToolResult(
            content=[TextContent(type="text", text=self.content_text())],
            structured_content=structured,
            is_error=True,
        )

    def content_text(self) -> str:
        """``content`` 文本段：人话 + 原始异常（可读的部分）。"""
        text = self.message
        detail = self.detail or {}
        raw = detail.get("exception")
        if raw:
            text = f"{text}\n\n原始异常：{raw}"
        return text


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


# --------------------------------------------------------------------- 出参形状
#
# 下面这一段解决的是「出错时回的结构体不符合 outputSchema」这件事。
# 详见 ``McpError.to_result`` 的说明：客户端拿工具声明的 outputSchema 校验
# structuredContent，形状对不上就是协议错 -32602，真正的异常反而看不见。

#: 出错时 ``caliber`` 填什么。
#:
#: 口径标识的语义是「这个数是在哪套口径下取出来的」。出错时根本没有数，
#: 所以填 ``error`` —— 这是一个**诚实**的值，而不是把某套口径的名字
#: 挂在一个没有数据的回执上。
ERROR_CALIBER = "error"


def _placeholder(annotation: Any) -> Any:
    """给某个类型标注造一个「类型对得上、值无害」的占位值。

    **只造得出「无害的空值」**：空串、0、False、空列表。
    这些值在错误回执里不会被当成业务数据读 —— 回执本身带 ``is_error``
    与 ``error`` 字段，而那些必填字段（模板 id、期间 id……）在错误回执里
    本来就无从得知。
    """
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if not args:
            return None
        return _placeholder(args[0])
    if origin in (list, set, tuple, frozenset):
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _model_placeholder(annotation)
    if annotation is str:
        return ""
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    return None


def _model_placeholder(model_cls: type[BaseModel]) -> dict[str, Any]:
    """模型里**必填**字段的占位值。

    只补必填的：有默认值的字段本来就合法，补了反而覆盖掉真实默认值。
    """
    out: dict[str, Any] = {}
    for name, field in model_cls.model_fields.items():
        if not field.is_required():
            continue
        out[name] = _placeholder(field.annotation)
    return out


def error_payload(model_cls: type[BaseModel], code: str, message: str,
                  detail: Optional[dict] = None) -> dict[str, Any]:
    """按出参模型拼一个**符合 outputSchema** 的错误结构体。

    形状：``caliber="error"`` + ``notes=[message]`` + ``error={code,message,detail}``，
    外加必填字段的类型正确占位值。

    兜底：模型将来加了本模块认不出的类型时，占位值可能填不上 ——
    那时退回「尽力而为」的字典，而不是把协议层搞坏（协议层一坏，
    调用方连这条错误都读不到）。
    """
    payload: dict[str, Any] = {"code": code, "message": message}
    if detail:
        payload["detail"] = detail
    base = _model_placeholder(model_cls)
    base["caliber"] = ERROR_CALIBER
    base["notes"] = [message]
    base["error"] = payload
    try:
        return model_cls(**base).model_dump()
    except Exception:  # pragma: no cover - 占位值填不上时的兜底
        log.warning("错误回执无法按出参模型拼装（模型=%s）", model_cls.__name__,
                    exc_info=True)
        return {**base, **payload}


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


#: PG 自己的违例话术。**只要命中一条，就不是「我们自己写的中文护栏」** ——
#: 哪怕它夹带了中文**数据**（凭证号「记0001」、摘要都会是中文）。
#:
#: 这些是 PG 固定的几套英文模板（唯一/外键/非空/CHECK/超长），不是我们写的
#: 散文，所以判据是稳定的。
#:
#: **为什么不能只看「有没有汉字」**（审查补漏）：PG 的违例原文里会夹带中文
#: **数据**，于是「含中文 ⇒ 是我们写的」这个判据会被骗过 —— 带着中文数据的
#: 23505 原文（表名、约束名、**重复键的值**）会被当成自家提示原样回给 agent。
#: 与 ``main.py`` 的同一套判据保持一致。
_PG_VIOLATION_MARKERS = (
    "violates",                 # violates unique/foreign key/check/not-null constraint
    "duplicate key value",      # 23505
    "detail:",                  # PG 的 DETAIL 行
    "key (",                    # DETAIL 里的 Key (col)=(val)
    "is not present in table",  # 23503
    "null value in column",     # 23502
    "value too long for type",  # 22001
    "row for relation",         # 23514
)


def _is_own_guardrail_text(message: str) -> bool:
    """这句违例文案是不是**我们自己写的中文护栏文案**。

    判据：**有汉字**（我们自己写的提示都是中文）**且不是 PG 的固定话术**。

    ``db_init.sql`` 里的 ``RAISE EXCEPTION '已记账的凭证不允许删除'`` 有汉字
    ⇒ 是写给人看的提示；PG 自带的 ``duplicate key value violates unique
    constraint "uq_..."`` 没有汉字 ⇒ 里面带的是**库结构**，不能回给 agent。

    **但 PG 的原文里也会夹带中文数据**（重复键的值可能是凭证号「记0001」），
    所以光看"有没有汉字"不够 —— 还要排除 PG 那几套**固定英文模板**。
    """
    if not any("一" <= ch <= "鿿" for ch in message):
        return False
    lowered = message.lower()
    return not any(marker in lowered for marker in _PG_VIOLATION_MARKERS)


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


#: SQLAlchemy 的 DBAPIError 家族。判据用**类名**而不是 ``isinstance`` ——
#: 这一层刻意不 import sqlalchemy（工具模块要在没有 DB 依赖的环境里也能 import）。
_DBAPI_ERROR_NAMES = frozenset({
    "DBAPIError", "IntegrityError", "OperationalError", "ProgrammingError",
    "DataError", "InterfaceError", "InternalError", "NotSupportedError",
    "StatementError",
})


def _is_dbapi_error(exc: Exception) -> bool:
    return exc.__class__.__name__ in _DBAPI_ERROR_NAMES


def _exception_summary(exc: Exception) -> str:
    """一句「原始异常」摘要：类型 + 消息 + 末尾几帧栈。

    **给的是能定位问题的信息，不是库结构。** 具体说：

    * **类型名**永远给（``ValidationError`` / ``KeyError`` / …）——它就是
      「哪一坏」的答案；
    * **消息**只在**不是数据库异常**时给 —— DB 异常的消息里含表名、列名、
      约束名，那是要脱敏的（见本模块 docstring）；
    * **栈**给末尾三帧，且**只给文件名**（不带绝对路径）—— 路径是部署
      细节，不该回给外部调用方，而「哪个文件第几行」已经足够定位。
    """
    parts = [type(exc).__name__]
    if _is_dbapi_error(exc):
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        parts.append(f"sqlstate={sqlstate}" if sqlstate else "数据库异常")
    else:
        message = str(exc).strip()
        if message:
            parts.append(message[:500])
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        parts.append("at " + " ← ".join(
            f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
            for f in frames[-3:]))
    return " | ".join(parts)


def internal_error(exc: Exception, *, request_id: str = "-") -> McpError:
    """兜底：一句脱敏提示 + 请求号。

    **服务端日志留全文；回给调用方的是「可定位」的摘要** ——
    异常类型、消息（非数据库异常时）与末尾几帧栈。

    为什么不给全文：数据库异常的原文含库结构，不能出站（见本模块
    docstring）。为什么不能什么都不给：只回一句「服务内部错误」的话，
    调用方（以及接这个接口的 agent）连「哪一坏」都不知道 —— 那正是
    ``-32602`` 吞掉原始异常之后最难受的地方。
    """
    log.exception("MCP 内部异常 rid=%s：%s", request_id, str(exc)[:500])
    return McpInternalError(
        f"服务内部错误（请求号 {request_id}），请联系管理员并提供该请求号",
        detail={"exception": _exception_summary(exc), "request_id": request_id},
    )


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
        # **带上原始异常**：``McpPeriodError`` 自己只存了文案，异常类型与
        # 抛出位置就丢了。调用方（尤其是 agent）看到「期间不存在」时需要能
        # 知道是哪个口径层的校验拦下来的 —— 那在 ``content`` 文本段里。
        err = exc.to_error()
        err.detail = {**(err.detail or {}),
                      "exception": _exception_summary(exc)}
        return err
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
        # **ValueError 的消息是我们自己写的中文**（``find_period_by_code`` /
        # ``merge_by_code`` / 出参模型的 Pydantic 校验错都在这一类），
        # 可以原样回给调用方。
        #
        # 但出参模型校验错（``ValidationError`` 是 ``ValueError`` 的子类）
        # **必须带上原始异常**：它的消息里是「哪个字段、哪一格、期望什么类型」，
        # 那正是「报表渲染不出来」这类问题的答案。
        detail = {"exception": _exception_summary(exc)}
        return McpBadRequest(str(exc), detail=detail)
    # SQLAlchemy 的 DBAPIError 家族。
    if _is_dbapi_error(exc):
        return dbapi_to_mcp_error(exc, request_id=request_id)
    return internal_error(exc, request_id=request_id)


__all__ = [
    "ERROR_CALIBER", "McpAuthError", "McpBadRequest", "McpError",
    "McpForbiddenError", "McpInternalError", "McpNotFoundError",
    "McpRateLimited", "dbapi_to_mcp_error", "error_payload",
    "internal_error", "to_mcp_error",
]
