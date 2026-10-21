"""MCP 服务本体：工具注册表、子应用工厂。

**这一层只做三件事**：建 ``MCPServer``、把工具注册进去、把子应用包成父应用
能挂的一条路由。工具的实现分散在 ``app/mcp/tools/`` 下三个模块里
（导航 / 明细 / 分析），各自导出 ``register(server)`` —— 分开是因为它们
是三块独立的业务语义，合在一个文件里会有六百行以上的单文件。

**传输方式：Streamable HTTP + stateless。**
生产是 2 worker 多进程（``scripts/start_server.py``）。有状态的 MCP session
会把 ``initialize`` 与后续调用分散到不同 worker 上，于是「刚初始化完就
报 session not found」。``stateless_http=True`` 每次请求独立建传输，规避
这个问题。

**DNS 重绑定防护。**
传输层自带这层防护（校验 ``Host`` / ``Origin`` 头，挡的是「恶意网页把
浏览器指向本机服务」）。配置了 ``MCP_ALLOWED_HOSTS`` 就开，没配就关。

为什么没配时是**关**而不是「只放行 localhost」：这个端点的第一道闸是
**每次请求都校验的 bearer 令牌**，而生产没有 CORS 中间件（跨源读不到
响应、也带不上 ``Authorization`` 头）—— 浏览器侧的 DNS 重绑定拿不到令牌，
读不到任何数据。而 fail-closed 在这里有个更糟的表现：服务看起来装好了，
却把每一个外部 agent 都挡在外面，运维看到的是一个不透明的 421 而不是
一句配置提示。所以选了「没配就关 + 启动时明确告警」，把决定权交回给
运维 —— 要这层防护，配一行环境变量即可。
"""
from __future__ import annotations

import logging
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.core.config import mcp_allowed_host_list, settings

from app.mcp.asgi import MCP_INNER_PATH, MCP_PATH, build_mcp_asgi
from app.mcp.tools import analysis, detail, navigation

log = logging.getLogger("cwq")

#: 连上时 agent 看到的说明。**这是 agent 理解口径的第一入口** ——
#: 它拿到的每个数都带着 ``caliber`` 与 ``notes``，而这里解释这两个字段
#: 是什么意思、为什么两次调用同一个问题的答案可能不同。
INSTRUCTIONS = """\
本服务是「财务记账系统」的**只读**账套数据接口。外部 AI agent 通过它查询
账套，回答「最近一笔租金是什么时候付出去的」「这个月电费付了吗」「今年
收入费用怎么变」这类会计问题。

## 读什么

* 账套、期间、科目的导航信息；
* 凭证明细与科目余额；
* 「钱到底付出去没有」的判定（区分**已付** / **仅计提未付** / **有凭证但
  未记账** / **本月无记录**四种状态）；
* 收支趋势与内置报表（资产负债表 / 利润表）。

## 不读什么

* **没有任何写能力**：不能建凭证、记账、结账、改科目、导入。
* **不提供任意 SQL 或原始表达式入口**。

## 口径（每个返回体都带 `caliber` 与 `notes[]`，请一并读）

* 金额单位是**元**，两位小数，以**字符串**承载（避免浮点误差），并带
  `currency: "CNY"`。
* 负数是**红字**（冲销），不是负数金额。
* 默认只读**已记账**（`status="POSTED"`）的凭证；要包含未记账凭证必须
  显式传 `include_unposted=true`。
* 损益类取数用**报表口径**（排除结转凭证），余额类用**账簿口径**（含结转）。
  两个口径的差额恰是结转凭证金额，返回体的 `notes[]` 会写明差多少。
* 「今年」指**这个账套当前在记的那一个会计年度**，不是服务器日历年。
* 科目表每个会计年度各有一份：同一编码在不同年度是**两行、两个 id**。
  跨年请按 `year` 显式指定，不要把**上一年**的科目 id 传进来。

## 账号可见性

你看到的账套范围＝令牌绑定那个用户被授权的账套范围。用户的权限变了
（撤授权、停用、换角色），你这边**下一次调用就变**，不需要重启也不需要
换令牌。
"""

def transport_security() -> Optional[TransportSecuritySettings]:
    """按配置给出传输层的安全设置。

    配了 ``MCP_ALLOWED_HOSTS`` ⇒ 开启 DNS 重绑定防护，只放行这些 Host；
    没配 ⇒ 关闭（理由见模块 docstring）。
    """
    hosts = mcp_allowed_host_list(settings.mcp_allowed_hosts)
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = []
    for host in hosts:
        base = host[:-2] if host.endswith(":*") else host
        origins.append(f"http://{base}")
        origins.append(f"https://{base}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def register_tools(server: MCPServer) -> None:
    """把三组工具注册到服务上。

    **幂等**：同一个 server 重复注册不会炸（``add_tool`` 对重名只告警，
    但告警是噪音）。用 server 上的一个标记记住「已经注册过」。
    """
    if getattr(server, "_cwq_tools_registered", False):
        return
    navigation.register(server)
    detail.register(server)
    analysis.register(server)
    try:
        server._cwq_tools_registered = True
    except AttributeError:  # pragma: no cover - 防御
        pass


#: 一次服务实例的构造。**每次调用都新建一个。**
#:
#: 为什么不是全局单例：MCP 的 session manager
#: （``StreamableHTTPSessionManager``）**一个实例只能 ``run()`` 一次** ——
#: 第二次进入会抛「can only be called once per instance」。而 lifespan 在
#: 进程里可能跑多次（测试里每个 ``TestClient`` 一次）。
#:
#: 生产不受影响（uvicorn 的 reload 与 ``--workers`` 都是**新进程**，各自
#: 导入一次模块、各自一份实例），但把这件事做成「每次建一套新的」，
#: 测试与生产就是同一条路径 —— 而不是两条路径里只有一条被测过。
def _new_server() -> MCPServer:
    server = MCPServer(
        name="cwq-accounting",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )
    register_tools(server)
    return server


#: 供内省与测试用的实例（``list_tools()`` 之类）。**业务请求不走它** ——
#: 业务走的是 lifespan 里现建的那一套。
mcp_server: MCPServer = _new_server()


def build_inner_app(server: MCPServer) -> Starlette:
    """MCP 的**子应用**（Starlette）。

    ``stateless_http=True``：每次请求独立建传输，规避多 worker 下会话
    错乱（生产是 2 worker，见 ``scripts/start_server.py``）。

    ``json_response=False``（默认，SSE 回包）：这是 Streamable HTTP 的
    规范主路径，第三方客户端（Claude Desktop / Cherry Studio / Dify）都是
    按它实现的。代价是 nginx 那边要 ``proxy_buffering off``，否则回包被
    缓冲住 —— ``deploy/nginx/cwq.conf`` 已加专用 location。
    """
    return server.streamable_http_app(
        streamable_http_path=MCP_INNER_PATH,
        stateless_http=True,
        json_response=False,
        transport_security=transport_security(),
    )


def build_app(server: Optional[MCPServer] = None) -> tuple[MCPServer, object]:
    """返回 ``(服务实例, 可挂载的 ASGI 应用)``。

    服务实例要留着 —— 它的 ``session_manager.run()`` 必须在父应用的
    lifespan 里进入（Starlette **不会**为被挂载的子应用跑 lifespan，
    少了这一步第一个请求就会报「Task group is not initialized」）。

    **每次调用都新建一套**（见 ``_new_server`` 的说明）：session manager
    一个实例只能 ``run()`` 一次。
    """
    server = server or _new_server()
    inner = build_inner_app(server)
    return server, build_mcp_asgi(inner)


__all__ = [
    "INSTRUCTIONS", "MCP_INNER_PATH", "MCP_PATH", "build_app",
    "build_inner_app", "mcp_server", "register_tools", "transport_security",
]
