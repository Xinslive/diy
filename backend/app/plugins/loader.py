"""每个 worker 预加载；只把受限 registrar/context 交给 register。"""
from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

import inspect as pyinspect
import logging
import sys
from fastapi import APIRouter
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from app.core.config import ISOLATION_OFF, ISOLATION_ON, ISOLATION_SHADOW, settings
from app.core.db import SessionLocal, engine
from app.core.deps import get_current_user
from fastapi.security import HTTPAuthorizationCredentials
from app.plugins.access import require_plugin_access
from app.plugins.errors import PluginError, unavailable
from app.plugins.installer import root_path
from app.plugins.migrations import read_head, read_version, validate_tables
from app.plugins.models import PluginRegistry
from app.plugins.public_api import context_for, current_principal, current_request_id
from app.plugins.sandbox import exec_plugin_module
from app.plugins.sdk_rules import (
    BODY_ANCHOR,
    build_body_payload_wrapper,
    validate_endpoint_signature,
    validate_hook_handler,
    validate_route_methods,
    validate_route_path,
    validate_task_handler,
)
from app.plugins.security import verify_directory
from app.plugins.workers import WorkerRuntime

log = logging.getLogger(__name__)


# 下面两个名字是历史别名，保留是为了不打断既有引用（测试与将来 worker 侧的
# 代理都按它们取）。**实现**在 :mod:`app.plugins.sdk_rules` ——
# 那才是「插件 SDK 契约」的唯一一份，宿主与 worker 共用。
_BODY_ANCHOR = BODY_ANCHOR
_with_body_payload = build_body_payload_wrapper


class Registrar:
    __slots__ = ("_plugin_id", "_router", "_hooks", "_declared_hooks", "_tasks", "_declared_tasks", "_declared_routes")

    def __init__(self, manifest):
        self._plugin_id = manifest["id"]
        self._router = APIRouter()
        self._hooks, self._declared_hooks = {}, set(manifest.get("hooks", []))
        self._tasks, self._declared_tasks = {}, {task["name"]: task for task in manifest.get("tasks", [])}
        # 插件**原始**的 (path, methods, endpoint)。
        #
        # 为什么要另存一份：``add`` 挂到 router 上的是**包过 body 载体**的
        # 那个函数，它的 ``__signature__`` 是改写过的。shadow 比对要比的
        # 是插件**原本**的签名（worker 侧看到的也是原本的），
        # 拿改写后的那份去比，每条路由都会报「签名不一样」——假差异。
        self._declared_routes: list[tuple] = []

    def _check(self, path, endpoint, methods):
        # 判据本身在 :mod:`app.plugins.sdk_rules`（SDK 契约的唯一一份）。
        # 这里只补一条**与本 Registrar 绑定**的判据：路由不能重复 ——
        # 那条要看着 ``self._router`` 里已有的路由才判得出来。
        validate_route_path(path, self._plugin_id)
        validate_route_methods(methods)
        validate_endpoint_signature(endpoint)
        for route in self._router.routes:
            if route.path == "/api/plugins/" + self._plugin_id + path and set(route.methods or []) & set(methods):
                raise PluginError("PLUGIN_ROUTE_CONFLICT", "插件路由重复")

    def add(self, path, endpoint, methods):
        self._check(path, endpoint, methods)
        self._declared_routes.append((path, list(methods), endpoint))
        self._router.add_api_route("/api/plugins/" + self._plugin_id + path,
            _with_body_payload(endpoint, path, methods), methods=methods,
            name=f"plugin:{self._plugin_id}:{len(self._router.routes)}")

    def get(self, path, endpoint):
        self.add(path, endpoint, ["GET"])

    def post(self, path, endpoint):
        self.add(path, endpoint, ["POST"])

    def route(self, router, *, namespace=None):
        """把插件自己组装好的 router 挂到宿主命名空间下。

        ``namespace`` 可以省略：Registrar 知道自己的插件 id，默认就用它。
        显式传入且不等于插件 id 时拒绝 —— 插件只能往自己的命名空间里挂路由。
        """
        expected = self._plugin_id if namespace is None else namespace
        if expected != self._plugin_id or not isinstance(router, APIRouter) or router.on_startup or router.on_shutdown or router.dependencies:
            raise PluginError("INVALID_PLUGIN_ROUTER", "插件只能提交无生命周期/依赖的命名空间 router")
        for route in router.routes:
            if not isinstance(route, APIRoute) or route.dependencies or route.callbacks:
                raise PluginError("INVALID_PLUGIN_ROUTER", "插件只允许 HTTP 业务路由")
            self.add(route.path, route.endpoint, route.methods)

    def hook(self, name, handler):
        if name not in self._declared_hooks or name in self._hooks:
            raise PluginError("INVALID_PLUGIN_HOOK", "插件 hook 未声明、重复或不在白名单中")
        validate_hook_handler(handler)
        self._hooks[name] = handler

    def task(self, name, handler):
        if name not in self._declared_tasks or name in self._tasks:
            raise PluginError("INVALID_PLUGIN_TASK", "插件定时任务未声明、重复或不能是异步函数")
        validate_task_handler(handler)
        self._tasks[name] = handler

    def declarations(self) -> list[dict]:
        """插件声明了什么（阶段 1 的 shadow 比对用）。

        **与 worker 侧用的是同一份提取逻辑**
        （:mod:`app.plugins.ipc.declarations`）—— 否则比对出来的差异分不清
        是「插件行为真的不同」还是「两边的提取逻辑写得不一样」，
        而后者占绝大多数。

        注意这里取的是**原始 endpoint**而不是包过 body 载体的那个 ——
        worker 侧看到的也是原始的，比对才对得上。
        """
        from app.plugins.ipc.declarations import describe_hook, describe_route, describe_task

        declared = []
        for path, methods, endpoint in self._declared_routes:
            declared.append(describe_route(path, methods, endpoint))
        for name, handler in self._hooks.items():
            declared.append(describe_hook(name, handler))
        for name, handler in self._tasks.items():
            declared.append(describe_task(name, handler))
        return declared


def load_one(runtime, plugin):
    directory = root_path() / plugin.plugin_id / plugin.plugin_digest
    verified = verify_directory(directory, plugin.plugin_digest)
    with engine.connect() as conn:
        if read_head(directory) != plugin.migration_head or read_version(conn, plugin.plugin_id) != plugin.migration_head:
            raise PluginError("MIGRATION_PENDING", "插件迁移尚未就绪")
        validate_tables(conn, verified.manifest, expected=plugin.tables)
    entry = directory / verified.manifest["contributes"]["backend_entry"]
    name = "cwq_plugin_" + plugin.plugin_digest
    mode = settings.plugin_isolation
    if mode == ISOLATION_ON:
        # **插件代码不进宿主进程** —— 它只在 worker 进程里跑，
        # 宿主这边只有「声明 + 转发函数」。
        _load_isolated(runtime, plugin, entry, verified)
        return
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        # **不走 ``spec.loader.exec_module``**：那条路用解释器的真内建
        # （``__import__`` / ``exec`` / ``open`` 全在），等于把静态扫描
        # 当成唯一防线。审计 §3.1 实测过一条静态扫描看不见的逃逸链
        # （``operator.attrgetter("__globals__")`` → 整份 ``__builtins__``），
        # 所以这里在**运行期**把内建能力裁一遍（见 app.plugins.sandbox）。
        module = exec_plugin_module(name, entry)
    finally:
        sys.dont_write_bytecode = previous
    register = getattr(module, "register", None)
    if not callable(register) or len(pyinspect.signature(register).parameters) != 2:
        raise PluginError("INVALID_PLUGIN_ENTRY", "插件必须提供 register(registrar, ctx)")
    registrar = Registrar(verified.manifest)
    # 插件拿到的是**插件面**的上下文：不含导入批次的「确认」能力 ——
    # 确认必须走宿主路由那一次独立的 HTTP 请求（见 public_api.context_for）。
    result = register(registrar, context_for(verified.manifest, plugin=True))
    if result is not None:
        raise PluginError("INVALID_PLUGIN_ENTRY", "register 不得返回 app 或其他对象")
    from app.plugins.services import router_for
    # 宿主路由先匹配，插件的 /{path} 等通配路由不能遮住宿主服务。
    services = router_for(verified.manifest)
    services.routes.extend(registrar._router.routes)
    if set(registrar._tasks) != set(registrar._declared_tasks):
        raise PluginError("INVALID_PLUGIN_TASK", "每个已声明的定时任务都必须注册一个处理函数")
    runtime.handlers[plugin.plugin_id] = AsyncExitStackMiddleware(services)
    runtime.hooks[plugin.plugin_id] = registrar._hooks
    runtime.tasks[plugin.plugin_id] = {name: (spec["schedule"], registrar._tasks[name], plugin.plugin_digest)
                                       for name, spec in registrar._declared_tasks.items()}
    if mode == ISOLATION_SHADOW:
        # **shadow 不改变任何对外行为**：插件仍然由进程内那份代码处理，
        # 这里额外跑一遍 worker，把两边的声明逐条比对，差异只报告。
        _shadow_compare(runtime, plugin, entry, verified, registrar.declarations())


def _load_isolated(runtime, plugin, entry, verified):
    """``PLUGIN_ISOLATION=on``：插件代码只在 worker 进程里跑。

    宿主这一侧做三件事：

    1. 起 worker，收下它的**声明**（路由 / 钩子 / 任务 + 签名）；
    2. **再校验一遍**声明（worker 侧的校验是插件的自律，宿主这一遍才是
       平台的规矩）；
    3. 按声明**重建** FastAPI 路由与钩子/任务的转发函数。

    插件的模块**不进宿主进程** —— 这是这一模式与进程内模式的全部差别。
    """
    from app.plugins.ipc.host import start_worker
    from app.plugins.ipc.router import build_hooks, build_router, build_tasks, validate_declarations

    manifest = verified.manifest
    ctx = context_for(manifest, plugin=True)
    handle = start_worker(plugin_id=plugin.plugin_id, digest=plugin.plugin_digest,
                          entry=entry, manifest=manifest, ctx=ctx)
    try:
        validate_declarations(plugin.plugin_id, handle.declarations, manifest)
        from app.plugins.services import router_for
        services = router_for(manifest)
        services.routes.extend(build_router(plugin.plugin_id, handle.declarations, handle).routes)
        schedules = {task["name"]: {"schedule": task.get("schedule", ""),
                                    "digest": plugin.plugin_digest}
                     for task in manifest.get("tasks", [])}
        runtime.handlers[plugin.plugin_id] = AsyncExitStackMiddleware(services)
        runtime.hooks[plugin.plugin_id] = build_hooks(handle.declarations, handle)
        runtime.tasks[plugin.plugin_id] = build_tasks(handle.declarations, handle, schedules)
        runtime.workers[plugin.plugin_id] = handle
    except BaseException:
        # 装不上就把 worker 收掉 —— 否则留下一个没人管的进程。
        handle.close()
        raise


def _shadow_compare(runtime, plugin, entry, verified, in_process):
    """``PLUGIN_ISOLATION=shadow``：跑一遍 worker，把声明逐条比对。

    **只报告，不改变行为。** 差异写进日志（ERROR 级，带两边的内容），
    同时记在 ``runtime.isolation_diffs`` 上 —— 供运维与 CI 观察。
    """
    from app.plugins.ipc import shadow

    manifest = dict(verified.manifest)
    manifest["plugin_digest"] = plugin.plugin_digest
    ctx = context_for(verified.manifest, plugin=True)
    run = shadow.run_worker(entry=entry, manifest=manifest, ctx=ctx)
    differences: list = []
    if run.error:
        differences = [{"kind": "worker_error", "detail": run.error}]
        log.error("插件 shadow 比对：worker 没能跑起来 id=%s error=%s stderr=%s",
                  plugin.plugin_id, run.error, run.stderr[-800:])
    else:
        differences = shadow.compare(in_process, run.declarations)
        if differences:
            log.error("插件 shadow 比对发现差异 id=%s differences=%s", plugin.plugin_id, differences)
        else:
            log.info("插件 shadow 比对一致 id=%s declarations=%s", plugin.plugin_id, len(in_process))
    runtime.isolation_diffs[plugin.plugin_id] = differences


def _log_isolation_posture():
    """启动时把「插件代码跑在哪」**说清楚**。

    为什么要有这一段：**隔离档位是一个配置项，而配置项不出声就等于没有**。
    审计报告 §3.1 的根因是「插件后端代码在宿主进程内执行」——
    独立进程那一档（``PLUGIN_ISOLATION=on``）才是那条根因的正面对策，
    沙箱那一层只是**加固**，不是根治。

    默认档是 ``off``（现网行为不变），所以**默认部署仍然是在宿主进程里跑插件**。
    这不是缺陷，但必须是**明示**的：启动日志里说清楚当前档位、它在防什么、
    要换档改哪个环境变量 —— 否则「默认是什么」只有翻代码才知道。

    只说事实，不替运维做决定。
    """
    mode = settings.plugin_isolation
    if mode == ISOLATION_ON:
        log.info(
            "插件隔离：插件后端代码在**独立 worker 进程**里执行，"
            "宿主只经 IPC 派活（审计 §3.1 的正面对策）。")
        return
    if mode == ISOLATION_SHADOW:
        log.info(
            "插件隔离：shadow 档 —— 插件在**宿主进程内**跑一遍（对外行为），"
            "同时在 worker 里再跑一遍并比对声明；有差异会记 ERROR 日志。"
            "这一档是**比对**用的，不改变对外行为。")
        return
    # off：默认档
    log.warning(
        "插件隔离：当前是 **off** 档 —— 插件后端代码在**宿主进程内**执行，"
        "只有静态扫描 + 运行期能力裁剪两层（审计 §3.1 的加固，不是根治）。"
        "要切到独立进程（根治那一面）：设 PLUGIN_ISOLATION=on 并重启；"
        "切换会改变插件的执行位置与并发特性，升级窗口里请自行评估。")


def startup():
    from app.plugins.hooks import set_runtime
    set_runtime(None)
    if not settings.plugin_enabled:
        return None
    _log_isolation_posture()
    from app.plugins.reconcile import reconcile
    try:
        reconcile()
    except PluginError as exc:
        if exc.code != "PLUGIN_BUSY":
            log.exception("plugin reconciliation failed")
    runtime = WorkerRuntime()
    runtime.join()
    with SessionLocal() as db:
        plugins = list(db.scalars(select(PluginRegistry).where(PluginRegistry.deleted_at.is_(None))))
        for plugin in plugins:
            if not plugin.desired_enabled:
                runtime.register(plugin, "DISABLED")
                continue
            runtime.register(plugin, "LOADING")
            try:
                if plugin.runtime_status == "ORPHANED":
                    raise PluginError(plugin.error_code or "PACKAGE_MISSING")
                load_one(runtime, plugin)
                runtime.register(plugin, "LOADED")
            except BaseException as exc:
                # 受信任插件的普通初始化故障隔离；主动 exit/资源耗尽不属于进程隔离。
                code = exc.code if isinstance(exc, PluginError) else "LOAD_FAILED"
                log.exception("plugin preload failed id=%s code=%s", plugin.plugin_id, code)
                runtime.handlers.pop(plugin.plugin_id, None)
                # 隔离模式下可能已经起过 worker —— 装不上就要收掉，
                # 否则留下一个没人管的插件进程。
                worker = runtime.workers.pop(plugin.plugin_id, None)
                if worker is not None:
                    worker.close()
                runtime.register(plugin, "FAILED", code)
    runtime.start_heartbeat()
    set_runtime(runtime)
    return runtime


class PluginGateway:
    """在路由匹配前鉴权及 barrier；加载失败/无路由的 worker 仍返回相同 503。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/plugins/") or path == "/api/plugins/manifest" or path.startswith("/api/plugins/assets/"):
            return await self.app(scope, receive, send)
        pid = path.split("/", 4)[3]
        runtime = getattr(scope["app"].state, "plugin_runtime", None)
        def guard():
            raw = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw[7:]) if raw.lower().startswith("bearer ") else None
            with SessionLocal() as db:
                user = get_current_user(creds, db)
                require_plugin_access(db, pid, user, generation=runtime.generation if runtime else None)
                if runtime is None or pid not in runtime.handlers:
                    raise unavailable()
                return user.id
        try:
            user_id = await run_in_threadpool(guard)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, send)
        except SQLAlchemyError:
            log.exception("plugin access database unavailable id=%s", pid)
            return await JSONResponse({"detail": {"code": "PLUGIN_UNAVAILABLE", "message": "插件暂不可用"}}, status_code=503,
                                      headers={"Cache-Control": "no-store"})(scope, receive, send)
        response_started = False
        async def private_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message = {**message, "headers": [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"] + [(b"cache-control", b"private, no-store")]}
            await send(message)
        token = current_principal.set((pid, user_id, runtime.generation))
        request_token = current_request_id.set(str(scope.get("state", {}).get("request_id", ""))[:128])
        try:
            await runtime.handlers[pid](scope, receive, private_send)
        except HTTPException as exc:
            if response_started:
                raise
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, private_send)
        except RequestValidationError as exc:
            if response_started:
                raise
            errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
            await JSONResponse({"detail": errors}, status_code=422)(scope, receive, private_send)
        except Exception:
            request_id = scope.get("state", {}).get("request_id", "")
            log.exception("plugin request failed id=%s request_id=%s", pid, request_id)
            if response_started:
                raise
            await JSONResponse({"detail": {"code": "PLUGIN_EXECUTION_FAILED", "message": "插件执行失败", "request_id": request_id}}, status_code=500)(scope, receive, private_send)
        except BaseException:
            # **顺序不能反：Exception 在前、BaseException 在后。**
            #
            # 插件抛 SystemExit / KeyboardInterrupt / GeneratorExit 时，
            # ``except Exception`` 接不住 —— 它会穿出宿主，把整个请求变成
            # 连接中断（客户端看到的是「网关挂了」，而不是「插件执行失败」）。
            # 插件的异常**任何形态都不许穿出宿主边界**。
            request_id = scope.get("state", {}).get("request_id", "")
            log.exception("plugin request aborted id=%s request_id=%s", pid, request_id)
            if response_started:
                raise
            await JSONResponse({"detail": {"code": "PLUGIN_EXECUTION_FAILED", "message": "插件执行失败", "request_id": request_id}}, status_code=500)(scope, receive, private_send)
        finally:
            current_principal.reset(token)
            current_request_id.reset(request_token)
