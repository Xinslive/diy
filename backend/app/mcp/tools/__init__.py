"""MCP 工具集。

分成三个模块，按业务语义切：

* ``navigation`` —— 账套、期间、科目、上下文（**去哪儿看**）
* ``detail`` —— 凭证明细、最近付款、期间支付核对、科目余额（**看到了什么**）
* ``analysis`` —— 收支趋势、内置报表（**算出了什么**）

分开的理由不是行数好看，而是它们**动的库不一样**：导航类几乎不查业务表，
明细类要碰凭证与分录，分析类要跑账簿引擎与报表求值器。放在一个文件里
会让「哪些工具会跑聚合」这件事看不出来。

三个模块各自导出 ``register(server)``，由 ``app/mcp/server.py`` 统一调用。
"""
from __future__ import annotations

from app.mcp.tools import analysis, detail, navigation

__all__ = ["analysis", "detail", "navigation"]
