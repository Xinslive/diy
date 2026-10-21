"""MCP 端点的 ASGI 接线：路径改写 + 令牌鉴权中间件。

**为什么不用 ``app.mount("/api/mcp", ...)``。**
Starlette 的 ``Mount`` 要求路径带尾斜杠：``Mount("/api/mcp")`` 匹配的是
``/api/mcp/``，而 ``/api/mcp`` 本身**不匹配** —— 落到 ``redirect_slashes``
上变成 307。对 MCP 客户端来说，一次 POST 的 307 跳转是「能不能跟上取决于
这个客户端的实现」的赌注：有的客户端跟，有的不跟；跟的那些还可能把
``Authorization`` 头在跳转时丢掉（跨源时按规范要丢，同源通常保留，但
取决于实现）。

所以这里**不用 Mount**，而是自己写一个 ASGI 适配器：

* 父应用用一条 ``Route("/api/mcp", ...)`` 精确匹配（**无尾斜杠歧义**）；
* 适配器把 scope 里的 ``path`` 保持成 ``/api/mcp``（绝对 URL 生成要它），
  同时把 ``root_path`` 设成 ``/api`` —— 于是 Starlette 的
  ``get_route_path()`` 剥掉前缀后得到 ``/mcp``，正好命中 MCP 子应用里那条
  ``/mcp`` 路由。

**鉴权放在中间件层，而不是每个工具里。**
理由有三：

1. ``tools/list`` 也要挡在门外 —— 否则任何人都能拉到工具清单与参数 schema，
   那是一份「这个系统有哪些入口、各自要什么」的地图（与本仓关闭生产
   ``/openapi.json`` 的同一个理由）。
2. **一次 HTTP 请求 = 一次令牌校验**（stateless 模式下一次请求就是一个
   MCP 消息），不会因为一次会话里连着调十个工具而查十遍库。
3. 中间件拿到的 ``scope["state"]`` 与工具里拿到的 ``request.state`` 是同一
   个字典 —— 校验结果只传一次，不用来回解析。

**令牌校验要查库**，而中间件是 async 的、数据库是同步的。校验放进
``anyio.to_thread.run_sync`` —— 在工作线程里跑同步的 SQLAlchemy，
不占事件循环。
"""
from __future__ import annotations

import logging
from typing import Optional

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.db import SessionLocal

from app.mcp.auth import (McpPrincipal, check_rate_limit, extract_bearer,
                          verify_token)
from app.mcp.errors import McpAuthError, McpRateLimited

log = logging.getLogger("cwq")

#: MCP 端点在父应用里的路径。
MCP_PATH = "/api/mcp"

#: MCP 子应用内部那条路由的路径。
MCP_INNER_PATH = "/mcp"

#: scope["state"] 里放主体的键名。
PRINCIPAL_KEY = "mcp_principal"


class McpPathAdapter:
    """把父应用的请求改写成 MCP 子应用期望的形状。

    只改一处：``root_path``。父应用的 ``Route("/api/mcp", ...)`` 精确匹配
    之后，``scope["path"]`` 已经是 ``/api/mcp`` —— 而 MCP 子应用里那条
    路由是 ``/mcp``。Starlette 的 ``get_route_path()`` 会用 ``root_path``
    剥前缀，所以把 ``root_path`` 设成 ``/api``，剥完正好得到 ``/mcp``。

    ``path`` 与 ``raw_path`` **原样不动** —— Starlette 的
    ``URL(scope=...)`` 只看 ``scope["path"]``，动了它，绝对 URL 就错了
    （MCP 传输层在报错与日志里都会用到它）。

    若部署在某个前缀之下（``root_path`` 非空），前缀**接在前面**而不是被
    覆盖：``/prefix/api/mcp`` 剥掉 ``/prefix/api`` 之后同样是 ``/mcp``。
    """

    def __init__(self, inner: ASGIApp, *, mount_path: str = MCP_PATH,
                 inner_path: str = MCP_INNER_PATH):
        self.inner = inner
        self.mount_path = mount_path
        self.inner_path = inner_path
        # 剥掉的那一段：``/api/mcp`` → ``/api``。
        self.strip_prefix = (mount_path[: -len(inner_path)]
                             if mount_path.endswith(inner_path) else "")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.inner(scope, receive, send)
            return
        scope = dict(scope)
        if self.strip_prefix:
            scope["root_path"] = scope.get("root_path", "") + self.strip_prefix
        await self.inner(scope, receive, send)


def _principal_of(scope: Scope) -> Optional[McpPrincipal]:
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    principal = state.get(PRINCIPAL_KEY)
    return principal if isinstance(principal, McpPrincipal) else None


def principal_from_request(request) -> McpPrincipal:
    """从工具拿当前主体。

    工具跑在 HTTP 请求的上下文里（``ctx.request_context.request``），而
    鉴权中间件已经把主体挂在了同一个 ``scope["state"]`` 上。拿不到 ⇒
    中间件没跑（不该发生）或者中间件放行了未鉴权的请求 —— 一律当 401。
    """
    principal = _principal_of(request.scope)
    if principal is None:
        raise McpAuthError("MCP 令牌无效或已失效")
    return principal


class McpAuthMiddleware:
    """令牌鉴权中间件。

    * **没有令牌 / 令牌不对 / 用户停用** ⇒ 401，回一个**不含库结构**的
      JSON；
    * 通过 ⇒ 把主体挂进 ``scope["state"]``，放行。

    **每次都查库**，不做跨请求缓存：吊销、停用、改角色必须**立刻**生效
    （与 ``core/deps.py`` 的 ``_link_or_403`` 同一原则）。
    """

    def __init__(self, inner: ASGIApp):
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        raw = extract_bearer(_headers_of(scope))
        if not raw:
            await self._reject(scope, receive, send, "缺少 MCP 令牌")
            return

        try:
            principal = await anyio.to_thread.run_sync(self._verify, raw)
        except McpRateLimited as exc:
            await self._reject(scope, receive, send, exc.message, status_code=429)
            return
        except McpAuthError as exc:
            await self._reject(scope, receive, send, exc.message)
            return
        except Exception:  # pragma: no cover - 兜底：库里出问题也不该泄栈
            log.exception("MCP 令牌校验异常")
            await self._reject(scope, receive, send, "MCP 令牌无效或已失效")
            return

        state = scope.setdefault("state", {})
        state[PRINCIPAL_KEY] = principal
        await self.inner(scope, receive, send)

    @staticmethod
    def _verify(raw: str) -> McpPrincipal:
        with SessionLocal() as db:
            principal = verify_token(db, raw)
            # 限流：**令牌永不过期 ⇒ 泄漏窗口是无限的** ⇒ 吊销是唯一的事后
            # 手段，限流是唯一能压住「被拿去批量导出」的**事前**手段。
            # 放在令牌校验之后 —— 先确认这个令牌是真的，再去数它用了几次。
            check_rate_limit(principal.token_id)
            # ``last_used_at`` 让管理员能看出哪些令牌是僵尸、哪些还在被用。
            # 每次调用都更新它 —— 这是「永不过期」这条产品决定的主要观测手段。
            db.commit()
            return principal

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, message: str,
                      status_code: int = 401) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"code": "rate_limited" if status_code == 429 else "unauthorized",
                     "message": message},
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
        )
        await response(scope, receive, send)


def _headers_of(scope: Scope) -> dict:
    """把 scope 的原始 header 列表转成小写键的字典。"""
    out = {}
    for key, value in scope.get("headers") or ():
        try:
            out[key.decode("latin-1").lower()] = value.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):  # pragma: no cover
            continue
    return out


def build_mcp_asgi(inner: ASGIApp) -> ASGIApp:
    """把 MCP 子应用包成父应用可以直接挂的一条路由。

    顺序：**先鉴权，后改路径**。鉴权中间件读的是原始请求头，与路径无关；
    但把路径改写在里层，可以让鉴权失败时回的是父应用语境下的 401。
    """
    return McpAuthMiddleware(McpPathAdapter(inner))


class McpEndpoint:
    """可换代的 MCP 端点。

    **为什么需要它。**
    MCP 的 session manager（``StreamableHTTPSessionManager``）**一个实例只能
    ``run()`` 一次** —— 第二次进入会抛
    「can only be called once per instance」。而 lifespan 在进程里可能跑多次
    （测试里每个 ``TestClient`` 一次；生产里 uvicorn 的 reload 与
    ``--workers`` 是**新进程**，不受影响）。

    所以父应用挂的是**这个可换代的壳**，lifespan 每次启动时重新建一套
    ``MCPServer`` + 子应用 + session manager，再塞进这个壳里。
    壳的接口不变，路由不用重挂。

    壳在还没有内容时（lifespan 尚未跑完）回 503 而不是 404/500 ——
    「服务没起来」与「路径不存在」要能分开。
    """

    def __init__(self) -> None:
        self._app: Optional[ASGIApp] = None

    def set(self, app: ASGIApp) -> None:
        self._app = app

    def clear(self) -> None:
        self._app = None

    @property
    def ready(self) -> bool:
        return self._app is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if self._app is None:
                await JSONResponse(status_code=503, content={
                    "code": "unavailable", "message": "MCP 服务尚未就绪",
                })(scope, receive, send)
                return
            await self._app(scope, receive, send)
            return
        if self._app is None:
            await JSONResponse(status_code=503, content={
                "code": "unavailable", "message": "MCP 服务尚未就绪",
            })(scope, receive, send)
            return
        await self._app(scope, receive, send)


__all__ = [
    "MCP_INNER_PATH", "MCP_PATH", "McpAuthMiddleware", "McpEndpoint",
    "McpPathAdapter", "PRINCIPAL_KEY", "build_mcp_asgi",
    "principal_from_request",
]
