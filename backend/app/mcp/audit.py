"""MCP 读取审计。

**为什么需要它。**
现有 HTTP 读接口**一律不写审计** —— 这在本仓是有意的（审计表存的是被删/
被改凭证的完整快照，读操作不进那里）。但外部 agent 批量拉取账套数据，
在现有审计体系里**完全不可见**：谁在拉、拉了什么、什么时候拉的，一概没有。

令牌**永不过期** ⇒ 泄漏窗口是无限的 ⇒ 吊销是唯一的事后手段，而审计是
唯一能看出「谁在用、查了什么」的线索。所以 MCP 读调用**全部留痕**。

**只记标识与结果，不记数据本体。**
形态参照 ``app/plugins/audit.py``：记 token / 用户 / 账套 / 工具名 / 关键
入参 / 结果行数 / 耗时。**不记返回的金额、科目名、凭证内容** ——
审计表会被备份、被运维看，而 MCP 拉回来的正是账套数据本身。

写审计用独立提交（``app/plugins/audit.py`` 的 ``failure`` 同样如此），
不能被业务操作的 rollback 吞掉。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings

from app.mcp.auth import McpPrincipal

#: 允许记进审计的关键入参。**白名单**，不是黑名单 ——
#: 白名单之外的键一律不记，将来给工具加参数时不会顺手把内容带进审计。
AUDITABLE_ARGS = (
    "book_id", "year", "period_code", "period_id", "from_period_code",
    "to_period_code", "subject_id", "subject_ids", "keyword",
    "subject_keyword", "status", "granularity", "categories",
    "template_code", "template_id", "page", "page_size", "as_of",
    "lookback_months", "subject_scope",
)

#: 入参值的最大长度。超长的一律截断 —— 入参是外部 agent 给的，
#: 不设上限就等于给审计表开了一个「往里塞大字符串」的口子。
MAX_VALUE_LEN = 120


def _safe_args(args: Optional[dict]) -> dict:
    """挑出可以记的关键入参，并截断。"""
    if not args:
        return {}
    out: dict[str, Any] = {}
    for key in AUDITABLE_ARGS:
        if key not in args:
            continue
        value = args[key]
        if value is None or isinstance(value, (int, float, bool)):
            out[key] = value
            continue
        if isinstance(value, (list, tuple)):
            out[key] = [str(v)[:MAX_VALUE_LEN] for v in value][:20]
            continue
        out[key] = str(value)[:MAX_VALUE_LEN]
    return out


def log_read(db: Session, *, principal: McpPrincipal, tool: str,
             book_id: Optional[int] = None, args: Optional[dict] = None,
             result_count: Optional[int] = None,
             elapsed_ms: Optional[int] = None) -> None:
    """记一条 MCP 读取审计。

    ``mcp_audit_reads`` 关掉时**什么都不做**（连事务都不开）。
    """
    if not settings.mcp_audit_reads:
        return

    from app.accounting.audit_helper import log as audit_log

    user = SimpleNamespace(id=principal.user_id, username=principal.username)
    after = {
        "tool": tool,
        "book_id": book_id,
        "result_count": result_count,
        "elapsed_ms": elapsed_ms,
        "args": _safe_args(args),
    }
    audit_log(
        db,
        action="mcp.read",
        entity="mcp_token",
        entity_id=principal.token_id,
        user=user,
        book_id=book_id,
        detail=f"MCP 读取：{tool}",
        after=after,
    )


class Timer:
    """量一次工具调用花了多久。

    用 ``time.perf_counter`` 而不是 ``time.time``：后者会被系统时钟调整
    （NTP 校时、夏令时）带着走，算出来的耗时可能是负的。
    """

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)


__all__ = ["AUDITABLE_ARGS", "Timer", "log_read"]
