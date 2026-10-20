"""外部每分钟调用的应用级调度入口。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from app.core.db import SessionLocal
from app.scheduler import service

router = APIRouter()


def tick(request: Request, background: BackgroundTasks):
    token = getattr(request.state, "scheduler_token", None)
    with SessionLocal() as db:
        expected = service.trigger_secret_digests(db)
    if not any(expected):
        raise HTTPException(503, "调度触发密钥尚未配置")
    if not service.valid_trigger(token, expected):
        raise HTTPException(401, "调度触发密钥无效")

    runtime = getattr(request.app.state, "plugin_runtime", None)
    claims = service.claim_due(runtime)
    if claims:
        background.add_task(service.execute_claims, claims, runtime)
    return JSONResponse(
        status_code=202,
        content={"ok": True, "accepted": len(claims), "run_ids": [item["run_id"] for item in claims]},
        background=background,
    )


# ---------------------------------------------------------------------------
# 一条 FastAPI 路由 + 一个 Starlette 别名（**一个** FastAPI 端点）
# ---------------------------------------------------------------------------
# 以前这里是两个装饰器：
#
#     @router.get("/api/scheduler/tick/", include_in_schema=False)
#     @router.get("/api/scheduler/tick")
#     def tick(...):
#
# 同一个处理函数在**FastAPI 的路由表里出现两次**。看着像「重复注册」，
# 实际是**两个路径别名** —— 外部 cron 提供方有的带尾斜杠、有的不带，
# 两个都得能用；而 ``request_id_middleware`` 读的也是 ``path.rstrip("/")``
# 归一化之后的那个值（两边的密钥校验因此一致）。
#
# **为什么不用 redirect_slashes 统一。** FastAPI/Starlette 的自动跳转是
# **307**，而跳转目标会**丢掉 query string**：``request_id_middleware`` 在
# 进入路由之前就把 ``scope["query_string"]`` 清空了（那正是「密钥不许进
# 访问日志」的实现），于是 307 的 Location 里没有 ``key=`` ——
# 不跟随重定向的 cron 客户端看到的是一个 3xx（**什么都没发生**），
# 跟随的客户端则拿到一次没有密钥的请求（401）。两条路都是坏的。
#
# **为什么别名不是 FastAPI 路由。** FastAPI 0.143 的路由表与 OpenAPI 都只该
# 有一个 tick 端点：路由表里出现两次，遍历路由表的那些护栏
# （tests/ 里好几个 ``_all_routes()``）就会把它算成两个端点。
# FastAPI 的 APIRouter 支持直接挂 Starlette 的 Route —— 匹配与响应都照常，
# 只是它不进 OpenAPI，于是「别名」这件事对 FastAPI 是隐形的。
router.add_api_route("/api/scheduler/tick", tick, methods=["GET"])


async def _tick_with_trailing_slash(request: Request):
    """尾斜杠别名：与上面那条**完全同一个处理函数**。

    BackgroundTasks 在这里手工建一个：FastAPI 平时是把它作为依赖注入的，
    而 Starlette 的 Route 只传 request。响应里显式带着 ``background=``，
    所以后台任务照常在响应发出之后执行（行为与 FastAPI 那条一致）。
    """
    return await run_in_threadpool(tick, request, BackgroundTasks())


router.routes.append(
    Route("/api/scheduler/tick/", endpoint=_tick_with_trailing_slash, methods=["GET"])
)
