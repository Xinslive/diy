"""每个 worker 预加载；只把受限 registrar/context 交给 register。"""
from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

import importlib.util
import inspect as pyinspect
import logging
import re
import sys
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.params import File as FastapiFile, Form as FastapiForm
from starlette.datastructures import UploadFile as StarletteUploadFile
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


_BODY_ANCHOR = "cwq_body"


def _path_param_names(path):
    return set(re.findall(r"\{([a-z][a-z0-9_]*)\}", path or ""))


def _is_dict_annotation(annotation):
    if annotation is dict or annotation == "dict":
        return True
    return getattr(annotation, "__origin__", None) is dict


def _is_upload_param(param):
    """文件/表单参数：multipart 请求体由 FastAPI 自己拆，不能碰。"""
    if isinstance(param.default, (FastapiFile, FastapiForm)):
        return True
    for candidate in (param.annotation, getattr(param.annotation, "__origin__", None)):
        if isinstance(candidate, type) and issubclass(candidate, StarletteUploadFile):
            return True
    return False


def _with_body_payload(endpoint, path=None, methods=None):
    """让插件 POST 处理函数能按 SDK 约定从**请求体**收到前端提交的字段。

    SDK 约定前端用 JSON body 提交（``host.api.post(url, data)``，见
    docs/plugin-guide.md），处理函数按名字收这些字段：既可能写成文档里的
    ``def save_note(payload: dict)`` 从 payload 里取，也可能像两个插件实际那样写成
    ``def save_rule(book_id: int, payload: dict)``。但 FastAPI 只按注解决定参数
    去处——标了 ``dict`` 又没写 ``Body()`` 的按**查询参数**，标量没写 ``Body()`` 的
    更是**必填查询参数**。于是 body 里的一切都收不到，接口一律回 422
    ``Field required``：文档、插件客户端、插件三方的约定其实是一致的，只有路由
    装配这一步漏了。

    这里不改插件的函数对象（它是别人写的，且被 ``_check`` 验过签名），而是套一层
    只改签名的壳：``__signature__`` 优先于 ``__wrapped__``，FastAPI 的
    ``get_typed_signature`` 看到改过的签名，wrapper 再按原签名把 body 里的字段
    分发回去，真正的实现原样保留。

    兼容面必须一起守住，否则修一个坏两个：
    - 路径参数仍由 FastAPI 从路径取（``/rules/{rule_id}/delete`` 里的 ``rule_id``）；
    - 标量参数改成**可省查询参数**，``params={'book_id': ...}`` 这种旧调用照旧；
    - 分发时查询串优先、body 同名字段兜底，所以前端把 book_id 放进 JSON body 也
      照样收得到，同时请求体不能改写授权指向的账套；
    - 文件/表单路由（``UploadFile = File(...)``）整个不包装；
    - GET 路由与同时挂 GET 的路由完全不动。
    """
    if not methods or "POST" not in methods or "GET" in methods:
        return endpoint
    try:
        signature = pyinspect.signature(endpoint)
    except (TypeError, ValueError):
        return endpoint
    params = list(signature.parameters.values())
    if not params or any(p.kind in (pyinspect.Parameter.POSITIONAL_ONLY,
                                    pyinspect.Parameter.VAR_POSITIONAL,
                                    pyinspect.Parameter.VAR_KEYWORD) for p in params):
        return endpoint
    if any(_is_upload_param(p) for p in params):
        return endpoint
    path_params = _path_param_names(path)
    head = [p for p in params if p.name in path_params]
    body_params = [p for p in params if p.name not in path_params]
    if not body_params:
        return endpoint
    for param in head:
        if param.default is not pyinspect.Parameter.empty:
            # 路径参数不该有默认值，有就说明签名写得古怪，别自作主张。
            return endpoint

    dict_params = [p for p in body_params if _is_dict_annotation(p.annotation)]
    new_params = list(head)
    carrier_required = False
    if dict_params:
        # 第一个 ``dict`` 参数就是整个请求体：既是文档里的 ``def save_note(payload:
        # dict)``，也是 ``def save_rule(book_id: int, payload: dict)`` 里 payload 的本意。
        carrier = dict_params[0].name
        # 载体形参自己也是「必填」的：原签名没给默认值，就意味着调用方必须给整个
        # 请求体。少了它时给中文 422，而不是把 None 塞进插件函数让它在半路炸成 500。
        carrier_required = dict_params[0].default is pyinspect.Parameter.empty
        new_params.append(dict_params[0].replace(default=Body(default=None)))
        rest = [p for p in body_params if p.name != carrier]
    else:
        # 全是标量（例如 ``def undo_run(book_id: int, run_id: int)``）：合成一个参数
        # 接住整体 body，标量照旧留在查询串上，两者都能收到。
        carrier = _BODY_ANCHOR
        taken = {p.name for p in params}
        suffix = 0
        while carrier in taken:
            suffix += 1
            carrier = _BODY_ANCHOR + str(suffix)
        first = body_params[0].annotation
        origin = getattr(first, "__origin__", None)
        # body 未必是对象：插件也可以直接收 list 或标量，按原注解照收。
        annotation = List[Any] if (origin is list or first is list) else Dict[str, Any]
        new_params.append(pyinspect.Parameter(
            carrier, pyinspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=Body(default=None), annotation=annotation))
        rest = list(body_params)
    # 其余参数一律变成可省：FastAPI 把「简单类型 + 有默认值」的参数判成**可选**
    # 查询参数，于是既不再 422，旧调用方的查询串也照样有效。
    for param in rest:
        new_params.append(param.replace(
            default=None if param.default is pyinspect.Parameter.empty else param.default))

    # body 里没给、原签名又没默认值的标量：给中文 422，而不是把 None 塞进插件函数
    # 让它在半路炸成 500。
    required = [p.name for p in rest if p.default is pyinspect.Parameter.empty]

    @wraps(endpoint)
    def wrapper(*args, **kwargs):
        raw = kwargs.get(carrier)
        if carrier_required and (not isinstance(raw, dict) or not raw):
            # 载体是必填形参：缺 body（或 body 不是对象）是调用方的错，给 422，
            # 不让插件在函数内部炸成 PLUGIN_EXECUTION_FAILED(500)。
            raise HTTPException(422, "缺少请求体：%s" % carrier)
        body = raw if isinstance(raw, dict) else {}
        call = {param.name: kwargs.get(param.name) for param in head}
        for param in body_params:
            if param.name == carrier:
                call[param.name] = raw
                continue
            if kwargs.get(param.name) is not None:
                # 查询串优先。body 里的同名字段只能当**数据字段**：让它覆盖路由参数
                # 等于让请求体改写授权目标，跨账套一致性校验（data 层比对 values
                # 与 book_id）也就形同虚设了。
                call[param.name] = kwargs[param.name]
            elif isinstance(raw, dict) and param.name in raw:
                call[param.name] = raw[param.name]
            elif param.default is not pyinspect.Parameter.empty:
                call[param.name] = param.default
            else:
                call[param.name] = None
        for name in required:
            if call.get(name) is None:
                raise HTTPException(422, "缺少参数：%s" % name)
        return endpoint(**call)

    wrapper.__signature__ = signature.replace(parameters=new_params)
    return wrapper


class Registrar:
    __slots__ = ("_plugin_id", "_router", "_hooks", "_declared_hooks", "_tasks", "_declared_tasks")

    def __init__(self, manifest):
        self._plugin_id = manifest["id"]
        self._router = APIRouter()
        self._hooks, self._declared_hooks = {}, set(manifest.get("hooks", []))
        self._tasks, self._declared_tasks = {}, {task["name"]: task for task in manifest.get("tasks", [])}

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
        if name not in self._declared_hooks or name in self._hooks or not callable(handler) or pyinspect.iscoroutinefunction(handler):
            raise PluginError("INVALID_PLUGIN_HOOK", "插件 hook 未声明、重复或不在白名单中")
        self._hooks[name] = handler

    def task(self, name, handler):
        if (name not in self._declared_tasks or name in self._tasks or not callable(handler)
                or pyinspect.iscoroutinefunction(handler) or pyinspect.isgeneratorfunction(handler)
                or pyinspect.isasyncgenfunction(handler)):
            raise PluginError("INVALID_PLUGIN_TASK", "插件定时任务未声明、重复或不能是异步函数")
        try:
            params = list(pyinspect.signature(handler).parameters.values())
        except (ValueError, TypeError) as exc:
            raise PluginError("INVALID_PLUGIN_TASK", "定时任务处理函数签名无效") from exc
        if len(params) != 1 or params[0].kind not in (pyinspect.Parameter.POSITIONAL_ONLY, pyinspect.Parameter.POSITIONAL_OR_KEYWORD):
            raise PluginError("INVALID_PLUGIN_TASK", "定时任务处理函数必须只接收一个 TaskContext 参数")
        self._tasks[name] = handler


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
