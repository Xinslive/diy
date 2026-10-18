"""每个 worker 预加载；只把受限 registrar/context 交给 register。"""
from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

import importlib.util
import inspect as pyinspect
import logging
import re
import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.deps import get_current_user
from fastapi.security import HTTPAuthorizationCredentials
from app.plugins.access import require_plugin_access
from app.plugins.errors import PluginError, unavailable
from app.plugins.installer import root_path
from app.plugins.migrations import read_head, read_version, validate_tables
from app.plugins.models import PluginRegistry
from app.plugins.public_api import context_for, current_principal, current_request_id
from app.plugins.security import verify_directory
from app.plugins.workers import WorkerRuntime

log = logging.getLogger(__name__)


class Registrar:
    __slots__ = ("_plugin_id", "_router", "_hooks", "_declared_hooks")

    def __init__(self, manifest):
        self._plugin_id = manifest["id"]
        self._router = APIRouter()
        self._hooks, self._declared_hooks = {}, set(manifest.get("hooks", []))

    def _check(self, path, endpoint, methods):
        if not isinstance(path, str) or not re.fullmatch(r"/(?:[a-z0-9_-]+|\{[a-z][a-z0-9_]*\})(?:/(?:[a-z0-9_-]+|\{[a-z][a-z0-9_]*\}))*|/", path):
            raise PluginError("INVALID_PLUGIN_ROUTE", "插件路由必须是命名空间内的相对路径")
        if path == "/_host" or path.startswith("/_host/"):
            raise PluginError("INVALID_PLUGIN_ROUTE", "_host 是宿主服务保留路由")
        if not set(methods) <= {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise PluginError("INVALID_PLUGIN_ROUTE", "插件路由方法不允许")
        for param in pyinspect.signature(endpoint).parameters.values():
            annotation = str(param.annotation)
            if any(token in annotation for token in ("Request", "Response", "Session", "WebSocket", "BackgroundTasks")) or hasattr(param.default, "dependency"):
                raise PluginError("INVALID_PLUGIN_ROUTE", "插件公开接口不能接收宿主 request/session 或自定义依赖")
        for route in self._router.routes:
            if route.path == "/api/plugins/" + self._plugin_id + path and set(route.methods or []) & set(methods):
                raise PluginError("PLUGIN_ROUTE_CONFLICT", "插件路由重复")

    def add(self, path, endpoint, methods):
        self._check(path, endpoint, methods)
        self._router.add_api_route("/api/plugins/" + self._plugin_id + path, endpoint,
            methods=methods, name=f"plugin:{self._plugin_id}:{len(self._router.routes)}")

    def get(self, path, endpoint):
        self.add(path, endpoint, ["GET"])

    def post(self, path, endpoint):
        self.add(path, endpoint, ["POST"])

    def route(self, router, *, namespace):
        if namespace != self._plugin_id or not isinstance(router, APIRouter) or router.on_startup or router.on_shutdown or router.dependencies:
            raise PluginError("INVALID_PLUGIN_ROUTER", "插件只能提交无生命周期/依赖的命名空间 router")
        for route in router.routes:
            if not isinstance(route, APIRoute) or route.dependencies or route.callbacks:
                raise PluginError("INVALID_PLUGIN_ROUTER", "插件只允许 HTTP 业务路由")
            self.add(route.path, route.endpoint, route.methods)

    def hook(self, name, handler):
        if name not in self._declared_hooks or name in self._hooks or not callable(handler) or pyinspect.iscoroutinefunction(handler):
            raise PluginError("INVALID_PLUGIN_HOOK", "插件 hook 未声明、重复或不在白名单中")
        self._hooks[name] = handler


def load_one(runtime, plugin):
    directory = root_path() / plugin.plugin_id / plugin.plugin_digest
    verified = verify_directory(directory, plugin.plugin_digest)
    with engine.connect() as conn:
        if read_head(directory) != plugin.migration_head or read_version(conn, plugin.plugin_id) != plugin.migration_head:
            raise PluginError("MIGRATION_PENDING", "插件迁移尚未就绪")
        validate_tables(conn, verified.manifest, expected=plugin.tables)
    entry = directory / verified.manifest["contributes"]["backend_entry"]
    name = "cwq_plugin_" + plugin.plugin_digest
    spec = importlib.util.spec_from_file_location(name, entry)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    register = getattr(module, "register", None)
    if not callable(register) or len(pyinspect.signature(register).parameters) != 2:
        raise PluginError("INVALID_PLUGIN_ENTRY", "插件必须提供 register(registrar, ctx)")
    registrar = Registrar(verified.manifest)
    result = register(registrar, context_for(verified.manifest))
    if result is not None:
        raise PluginError("INVALID_PLUGIN_ENTRY", "register 不得返回 app 或其他对象")
    from app.plugins.services import router_for
    # 宿主路由先匹配，插件的 /{path} 等通配路由不能遮住宿主服务。
    services = router_for(verified.manifest)
    services.routes.extend(registrar._router.routes)
    runtime.handlers[plugin.plugin_id] = AsyncExitStackMiddleware(services)
    runtime.hooks[plugin.plugin_id] = registrar._hooks


def startup():
    from app.plugins.hooks import set_runtime
    set_runtime(None)
    if not settings.plugin_enabled:
        return None
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
        finally:
            current_principal.reset(token)
            current_request_id.reset(request_token)
