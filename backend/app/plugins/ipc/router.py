"""宿主侧：按 worker 报上来的声明，**重建**路由、钩子与定时任务。

**为什么要「重建」**
------------------
隔离之后，宿主手里**没有插件的函数对象**了 —— 只有一份声明（路径、方法、
签名）。而 FastAPI 靠 ``__signature__`` 决定「哪些是路径参数、哪些是 body
字段」，所以宿主必须把描述里的签名**还原**成一个真签名，挂到一个转发函数上。

**重建是严格的**
----------------
还原不出来就**拒绝加载这个插件**（:func:`app.plugins.ipc.signatures.rebuild_signature`
的 ``strict=True``），而不是塞一个 ``Any`` 上去。理由很实在：``Any`` 与原注解
对 FastAPI 的意义**不同** —— ``Any`` 让它少校验一点，而一个错的注解会让它
**按错的方式**解析请求体。与其让插件「看起来能跑、行为却变了」，不如
明确拒绝并说清楚是哪个参数。

**宿主侧要再校验一遍声明**
--------------------------
worker 侧也校验（同一份 :mod:`app.plugins.sdk_rules`），但那一遍是**插件的
自律**；宿主这一遍才是**平台的规矩**。两边都做，代价只是一次遍历。

**钩子与定时任务的调用形状**
---------------------------
进程内是 ``handler(payload)`` / ``handler(context)`` —— **一个位置参数**。
所以转发函数也用**位置参数**派活，worker 那边再 ``endpoint(*positional)``
调回去：这样插件那边收到的调用形态与进程内**逐字一致**（定时任务收到的还是
一个 ``SimpleNamespace``，插件里 ``ctx.run_id`` 照常能用）。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.plugins.errors import PluginError, unavailable
from app.plugins.ipc.host import WorkerUnavailable
from app.plugins.ipc.signatures import UnrebuildableSignature, rebuild_signature, signature_problems
from app.plugins.ipc.values import encode_mapping, encode_value
from app.plugins.sdk_rules import (
    FORBIDDEN_PARAMETER_TOKENS,
    build_body_payload_wrapper,
    validate_route_methods,
    validate_route_path,
)


def _forbidden_parameter_problems(signature) -> list[str]:
    """签名里有没有宿主对象（``Request`` / ``Session`` / ……）。

    worker 侧是对**真的函数对象**做这个检查（``validate_endpoint_signature``）；
    宿主这边只有描述，所以按注解里的**类型名**再查一遍 —— 判据用的是
    **同一份** token 表（``sdk_rules.FORBIDDEN_PARAMETER_TOKENS``）。
    """
    problems = []
    for item in signature or []:
        annotation = item.get("annotation") or {}
        names = {str(annotation.get("name") or ""), str(annotation.get("text") or "")}
        for token in FORBIDDEN_PARAMETER_TOKENS:
            if any(token in name for name in names if name):
                problems.append(f"参数 {item.get('name')} 的注解含有宿主对象 {token}")
    return problems


def validate_declarations(plugin_id: str, declarations: list[dict], manifest: dict) -> None:
    """宿主侧对声明**再校验一遍**。

    任何一条不合规都抛 :class:`PluginError` —— 插件**装不上**，而不是
    「装上了但少一条路由」。后者更难查：用户看到的是「某个接口 404」。
    """
    seen_routes: dict[tuple[str, frozenset], str] = {}
    declared_hooks = set(manifest.get("hooks", []))
    declared_tasks = {task["name"] for task in manifest.get("tasks", [])}
    got_hooks: set[str] = set()
    got_tasks: set[str] = set()

    for item in declarations:
        kind = item.get("kind")
        if kind == "route":
            path = item.get("path") or ""
            methods = item.get("methods") or []
            validate_route_path(path, plugin_id)
            validate_route_methods(methods)
            for other_path, other_methods in seen_routes:
                if other_path == path and other_methods & {str(m).upper() for m in methods}:
                    raise PluginError("PLUGIN_ROUTE_CONFLICT", "插件路由重复")
            seen_routes[(path, frozenset(str(m).upper() for m in methods))] = path
            problems = _forbidden_parameter_problems(item.get("signature"))
            problems += signature_problems(item.get("signature"))
            if problems:
                raise PluginError("PLUGIN_ISOLATION_UNSUPPORTED",
                                  "插件路由的签名宿主重建不了：" + "；".join(problems))
            continue
        if kind == "hook":
            name = item.get("name") or ""
            if name not in declared_hooks:
                raise PluginError("INVALID_PLUGIN_HOOK", "插件 hook 未声明或不在白名单中")
            got_hooks.add(name)
            continue
        if kind == "task":
            name = item.get("name") or ""
            if name not in declared_tasks:
                raise PluginError("INVALID_PLUGIN_TASK", "插件定时任务未声明或不在白名单中")
            got_tasks.add(name)
            continue
        raise PluginError("INVALID_PLUGIN_ENTRY", "插件声明了不认识的东西")

    missing_hooks = declared_hooks - got_hooks
    if missing_hooks:
        raise PluginError("INVALID_PLUGIN_HOOK",
                          "每个已声明的 hook 都必须注册一个处理函数：" + "、".join(sorted(missing_hooks)))
    missing_tasks = declared_tasks - got_tasks
    if missing_tasks:
        raise PluginError("INVALID_PLUGIN_TASK",
                          "每个已声明的定时任务都必须注册一个处理函数：" + "、".join(sorted(missing_tasks)))


def _route_forwarder(handle, declaration: dict):
    """一条路由的转发函数。

    ``__signature__`` 是**重建**出来的 —— FastAPI 靠它解析请求。
    """
    try:
        signature = rebuild_signature(declaration.get("signature"), strict=True)
    except UnrebuildableSignature as exc:
        # 理论上 :func:`validate_declarations` 已经拦过了；这里是兜底，
        # 免得「校验漏了 → 装上一个签名不对的插件」。
        raise PluginError("PLUGIN_ISOLATION_UNSUPPORTED",
                          "插件路由的签名宿主重建不了：" + "；".join(exc.problems)) from exc
    target = declaration["target"]

    def forward(**kwargs):
        try:
            return handle.invoke(target, kwargs=encode_mapping(kwargs))
        except WorkerUnavailable:
            # worker 没了 → **503**，不是 500：插件不可用与「插件自己出错」
            # 是两件事，前者该让调用方重试。
            raise unavailable()

    forward.__signature__ = signature
    forward.__name__ = declaration.get("handler") or "plugin_route"
    forward.__qualname__ = forward.__name__
    forward.__doc__ = "插件路由（由声明重建；真实处理函数在 worker 进程里）"
    return forward


def _hook_forwarder(handle, declaration: dict):
    """一个钩子的转发函数（位置参数，与进程内的 ``handler(payload)`` 一致）。"""
    target = declaration["target"]

    def forward(payload):
        try:
            return handle.invoke(target, positional=[encode_value(payload)])
        except WorkerUnavailable:
            raise unavailable()

    forward.__name__ = declaration.get("handler") or "plugin_hook"
    forward.__qualname__ = forward.__name__
    forward.__doc__ = "插件钩子（真实处理函数在 worker 进程里）"
    return forward


def _namespace_to_dict(value):
    """把 ``SimpleNamespace`` 变成 dict（定时任务上下文用）。"""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": value}


def _task_forwarder(handle, declaration: dict):
    """一个定时任务的转发函数（位置参数，与进程内的 ``handler(context)`` 一致）。"""
    target = declaration["target"]

    def forward(context):
        try:
            return handle.invoke(target, positional=[encode_value(_namespace_to_dict(context))])
        except WorkerUnavailable:
            raise unavailable()

    forward.__name__ = declaration.get("handler") or "plugin_task"
    forward.__qualname__ = forward.__name__
    forward.__doc__ = "插件定时任务（真实处理函数在 worker 进程里）"
    return forward


def build_router(plugin_id: str, declarations: list[dict], handle) -> APIRouter:
    """按声明建出插件的 FastAPI 路由。

    **body 分发那层用的是同一份实现**（:func:`app.plugins.sdk_rules.build_body_payload_wrapper`）
    —— 进程内模式与隔离模式对 POST body 的处理必须一致，否则同一份插件
    在两种模式下行为不同，而那正是「行为不变」要守住的地方。
    """
    router = APIRouter()
    index = 0
    for item in declarations:
        if item.get("kind") != "route":
            continue
        path = item.get("path") or ""
        methods = item.get("methods") or []
        endpoint = _route_forwarder(handle, item)
        router.add_api_route("/api/plugins/" + plugin_id + path,
                             build_body_payload_wrapper(endpoint, path, methods),
                             methods=methods,
                             name=f"plugin:{plugin_id}:{index}")
        index += 1
    return router


def build_hooks(declarations: list[dict], handle) -> dict:
    """按声明建出钩子的转发函数表（``{名字: 可调用}``）。

    形状与进程内**一样** —— ``hooks.py`` 那一侧不用改。
    """
    return {item["name"]: _hook_forwarder(handle, item)
            for item in declarations if item.get("kind") == "hook"}


def build_tasks(declarations: list[dict], handle, schedules: dict) -> dict:
    """按声明建出定时任务的转发函数表（``{名字: (schedule, 可调用, digest)}``）。

    形状与进程内**一样** —— 调度器那一侧不用改。
    """
    built = {}
    for item in declarations:
        if item.get("kind") != "task":
            continue
        name = item["name"]
        spec = schedules.get(name) or {}
        built[name] = (spec.get("schedule", ""), _task_forwarder(handle, item), spec.get("digest", ""))
    return built


__all__ = [
    "build_hooks",
    "build_router",
    "build_tasks",
    "validate_declarations",
]
