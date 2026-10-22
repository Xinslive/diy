"""工具层的公共骨架：会话、主体、审计、错误。

**为什么要有这一层。**
十个工具都要做同一串事：拿令牌主体、开数据库会话、判账套权限、翻异常成
结构化错误、写一条读取审计。散在十个文件里各写一遍，十处就会漂成十种
写法 —— 而其中任何一处漏掉「脱敏」或「鉴权」，就是一个**数据出口**。

所以这些事收在这里，工具本体只写业务。

**线程模型。**
MCP 的工具函数是**同步**函数，SDK 会在工作线程里跑它们
（``anyio.to_thread.run_sync``）。这正好：数据库是同步的 SQLAlchemy，
在工作线程里跑不占事件循环。

同步也意味着工具函数里可以直接用普通 ``with``，不需要 async/await 的
那一套。上下文用 ``threading.local`` 传 —— 一次工具调用的全部过程都在
同一个工作线程里，线程局部变量是这里最省事也最不会串的方式。

**异常怎么出去。**
工具函数抛出的异常会被 SDK 包成 ``UnexpectedToolError("Error executing tool
X")`` —— **原因不会给模型**。这对「不泄栈」是对的，但对「agent 能自我
修正」不够：agent 拿不到「期间不存在」还是「没有权限」的区别。

所以这里**不抛**：工具函数体里的异常被这一层接住，翻成
``CallToolResult(is_error=True)``，``structured_content`` 是
``{"code": ..., "message": ...}``。于是 agent 拿到的是一条**可自我修正的
结构化回执**，而不是一句「执行工具失败」。
"""
from __future__ import annotations

import logging
import threading
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, TypeVar

from mcp.server.mcpserver import Context
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal

from app.mcp.audit import Timer, log_read
from app.mcp.auth import McpPrincipal, require_book_access
from app.mcp.errors import McpAuthError, McpNotFoundError, to_mcp_error

log = logging.getLogger("cwq")

#: 出参模型的类型变量。``envelope`` 用它把「传入的模型类」与「返回的实例」
#: 的类型对上 —— 类型检查器能看出来调用处传错模型。
ModelT = TypeVar("ModelT", bound=BaseModel)

#: 一次工具调用期间的上下文。线程局部 —— SDK 在工作线程里跑同步工具，
#: 整个调用过程都在同一个线程内。
_state = threading.local()


@dataclass
class ToolContext:
    """一次工具调用的运行环境。"""

    db: Session
    principal: McpPrincipal
    request_id: str
    tool_name: str
    args: dict = field(default_factory=dict)
    result_count: Optional[int] = None
    elapsed_ms: Optional[int] = None

    def count(self, n: Optional[int]) -> None:
        """记下这次调用返回了多少行（写进审计）。"""
        self.result_count = n


def current_tool_context(ctx: Optional[Context] = None) -> ToolContext:
    """取当前这次工具调用的上下文。

    拿不到 ⇒ 中间件没跑（不该发生）⇒ 当 401 处理。
    """
    tc = getattr(_state, "tc", None)
    if tc is None:
        raise McpAuthError("MCP 令牌无效或已失效")
    return tc


def request_id_of(ctx: Optional[Context]) -> str:
    """这次调用的请求号（与父应用 ``X-Request-ID`` 同一个）。

    父应用的 ``request_id_middleware`` 把号挂在了 ``request.state`` 上，
    而那与 MCP 子应用是**同一个 scope** —— 所以这里读得到。
    """
    if ctx is None:
        return "-"
    try:
        request = ctx.request_context.request
    except Exception:  # pragma: no cover - 防御
        return "-"
    if request is None:
        return "-"
    return getattr(request.state, "request_id", "-") or "-"


def _request_of(ctx: Optional[Context]):
    """取底层 HTTP 请求（工具靠它拿主体）。"""
    if ctx is None:
        return None
    try:
        return ctx.request_context.request
    except Exception:  # pragma: no cover
        return None


def principal_of(ctx: Optional[Context]) -> McpPrincipal:
    """取当前主体（由鉴权中间件挂在 scope 上）。"""
    from app.mcp.asgi import principal_from_request

    request = _request_of(ctx)
    if request is None:
        raise McpAuthError("MCP 令牌无效或已失效")
    return principal_from_request(request)


@contextmanager
def tool_context(ctx: Optional[Context], *, tool_name: str,
                 args: Optional[dict] = None) -> Iterator[ToolContext]:
    """开一个数据库会话，跑完写审计，最后关会话。

    * 主体从请求的 scope 上取（鉴权中间件已经校验过）；
    * 审计在**会话还没关**的时候写 —— 写审计不需要另一条连接；
    * 审计失败**不能**让整次调用失败：审计是可观测性，不是业务。
    """
    principal = principal_of(ctx)
    request_id = request_id_of(ctx)

    db = SessionLocal()
    tc = ToolContext(db=db, principal=principal, request_id=request_id,
                     tool_name=tool_name, args=dict(args or {}))
    try:
        _apply_statement_timeout(db)
        yield tc
    finally:
        try:
            log_read(
                db,
                principal=principal,
                tool=tool_name,
                book_id=tc.args.get("book_id"),
                args=tc.args,
                result_count=tc.result_count,
                elapsed_ms=tc.elapsed_ms,
            )
            db.commit()
        except Exception:  # pragma: no cover - 审计不该拖垮业务
            db.rollback()
            log.exception("MCP 读取审计写入失败 rid=%s tool=%s",
                          request_id, tool_name)
        finally:
            db.close()


def require_book(tc: ToolContext, book_id: int):
    """账套授权 + 账套存在。**每个带 book_id 的工具都要过这一道。**"""
    require_book_access(tc.db, tc.principal, book_id)
    from app.models import Book

    book = tc.db.get(Book, book_id)
    if book is None:
        raise McpNotFoundError("账套不存在")
    return book


def _apply_statement_timeout(db: Session) -> None:
    """给这次工具调用设一条**库层**的语句超时。

    为什么要有：agent 可能发出「跨 5 年 + 全科目 + 每页 200 条」这种把库
    跑满的请求。与其让它占住一个 worker 直到 nginx 超时（那是一次 180s/360s
    的连接占用），不如让**库**在服务端就把它掐掉。

    ``SET LOCAL`` 只在当前事务内有效 —— 事务一提交就没了，所以不会污染
    连接池里的下一条语句（连接归还后时区之类的会话状态仍然是我们自己钉的
    那一套，见 ``app/core/db.py``）。

    超时抛的是 PG 的 ``57014``（query_canceled），由
    ``app/mcp/errors.py`` 翻成一句可读的中文，而不是把 PG 原文回给 agent。
    """
    timeout_ms = int(getattr(settings, "mcp_statement_timeout_ms", 0) or 0)
    if timeout_ms <= 0:
        return
    try:
        db.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
    except Exception:  # pragma: no cover - 兜底：设不上不该让整次调用失败
        log.warning("设置 statement_timeout 失败（不影响本次调用）",
                    exc_info=True)


def envelope(model: type[ModelT], *, caliber: str,
             notes: Optional[list[str]] = None, **fields) -> ModelT:
    """统一的出参外壳，**并按模型校验形状**。

    每个工具的返回体都带 ``caliber``（口径标识）与 ``notes[]``（人话解释），
    让 agent 知道自己手里的数是在哪个口径下取出来的 —— 这两个字段是
    「agent 说错话」与「agent 说对话」的分界线。

    **为什么在这里就校验，而不是等 SDK 回包时再校验。**
    出参模型（``app/mcp/schemas.py``）是这份契约的**唯一**定义：它同时给出
    发布给 agent 的 JSON Schema、返回时的校验、以及 ``structuredContent``。
    校验放在工具内部（本函数），出参形状对不上时抛的是普通异常 ——
    于是被 ``mcp_tool`` 接住、翻成一条结构化错误给 agent；若等 SDK 回包
    那一层才校验，异常会绕过 ``mcp_tool``，变成客户端眼里的「工具崩了」。

    ``currency`` 由模型的默认值给出（``schemas.CURRENCY``），不在这里拼 ——
    「金额单位是什么」是出参契约的一部分，不该散在两个地方各写一遍。
    """
    payload: dict[str, Any] = {"caliber": caliber, "notes": list(notes or [])}
    payload.update(fields)
    return model(**payload)


def _count_of(result: Any) -> Optional[int]:
    """从一个出参里猜出「多少行」。

    只认三个约定的键（``items`` / ``rows`` / ``records``）—— 猜不出来就
    返回 ``None``，不硬凑。

    出参现在是 **Pydantic 模型**（``app/mcp/schemas.py``），所以先摊成字典
    再找那几个键；模型实例摊出来的键与模型字段一致。
    """
    if isinstance(result, BaseModel):
        result = result.model_dump()
    if not isinstance(result, dict):
        return None
    for key in ("items", "rows", "records"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _output_model_of(fn: Callable[..., Any]) -> Optional[type[BaseModel]]:
    """工具的**出参模型**（函数的返回标注）。

    **为什么工具模块不许写 ``from __future__ import annotations``** ——
    有了那条，返回标注会变成字符串，这里就认不出来（`isinstance(ann, type)`
    为假）。认出来是**必须的**：出错时我们要按这个模型拼一个**符合
    outputSchema** 的结构体回给客户端，否则严格的客户端会抛协议错
    ``-32602``，把真正的异常吞掉（见 ``errors.McpError.to_result``）。

    字符串标注仍然尽量还原（``get_type_hints``），但还原不了就返回
    ``None`` —— 那时错误回执退回**基类契约** ``McpResult`` 拼（仍是一份
    合规结构体，不是旧的 ``{code,message}``）。
    """
    ann = getattr(fn, "__annotations__", {}).get("return")
    if isinstance(ann, str):
        try:
            ann = typing.get_type_hints(fn).get("return")
        except Exception:  # pragma: no cover - 还原不了就按「没有出参模型」处理
            return None
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    return None


def mcp_tool(*, name: str, description: str = ""):
    """把一个工具函数注册成 MCP 工具。

    **被装饰的函数必须满足两条**：

    1. 有一个**类型标注为 ``Context``** 的参数 —— SDK 靠这个标注把 MCP
       上下文注进来（它不按参数名猜）；
    2. **不要在模块里写 ``from __future__ import annotations``** ——
       那会让注解变成字符串，而 SDK 解析注解时用的是**装饰器所在模块**的
       全局命名空间（``base.py``），不是工具模块的。注解是对象时没有
       这个问题。

    装饰器做四件事：

    * 兜住异常 ⇒ 翻成 ``CallToolResult(is_error=True)`` + 结构化 payload；
    * **按出参模型拼错误回执**，让 ``structuredContent`` 仍然符合
      ``outputSchema``（不然客户端只报 ``-32602``，真正的异常看不见）；
    * 开/关数据库会话、写读取审计；
    * 保留原函数的签名与注解（``functools.wraps``），让 SDK 生成出正确的
      JSON Schema。
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        import functools

        output_model = _output_model_of(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = _pick_context(args, kwargs)
            request_id = request_id_of(ctx)
            timer = Timer()
            try:
                with tool_context(ctx, tool_name=name, args=_call_args(kwargs)) as tc:
                    _state.tc = tc
                    try:
                        result = fn(*args, **kwargs)
                    finally:
                        _state.tc = None
                    if tc.result_count is None:
                        tc.result_count = _count_of(result)
                    tc.elapsed_ms = timer.elapsed_ms
                    return result
            except Exception as exc:
                error = to_mcp_error(exc, request_id=request_id)
                log.info("MCP 工具失败 rid=%s tool=%s code=%s",
                         request_id, name, error.code)
                return error.to_result(output_model)

        wrapper.__name__ = name
        wrapper.__qualname__ = name
        wrapper.__doc__ = description or fn.__doc__ or ""
        return wrapper

    return decorator


def _pick_context(args: tuple, kwargs: dict) -> Optional[Context]:
    """从调用参数里找出 MCP 的 ``Context``。"""
    candidate = kwargs.get("ctx")
    if isinstance(candidate, Context):
        return candidate
    for value in args:
        if isinstance(value, Context):
            return value
    return None


def _call_args(kwargs: dict) -> dict:
    """工具入参（去掉 SDK 注入的 context）。"""
    return {k: v for k, v in kwargs.items() if k != "ctx"}


__all__ = [
    "ModelT", "ToolContext", "current_tool_context", "envelope", "mcp_tool",
    "principal_of", "request_id_of", "require_book", "tool_context",
]
