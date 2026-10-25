"""插件声明的形状 —— worker 与宿主共用的唯一一份。

**为什么必须共用一份**
----------------------
阶段 1 的 shadow 比对是「同一份插件代码，在两种执行方式下**各自声明了什么**，
逐条比对」。如果两边的声明是**各写各的**，那比对出来的差异分不清是
「插件行为真的不同」还是「两边的提取逻辑写得不一样」—— 后者占绝大多数，
于是比对结果毫无意义。

所以：**声明的形状与提取方式只有一份**（这个文件），
两边的代理（宿主的 :class:`app.plugins.loader.Registrar` 与
worker 的 :class:`app.plugins.ipc.proxy_registrar.ProxyRegistrar`）
都调用它。

声明的形状
----------
路由::

    {"kind": "route", "target": "route:/rules:GET", "path": "/rules",
     "methods": ["GET"], "handler": "chart", "signature": [...]}

钩子与任务::

    {"kind": "hook", "target": "hook:on_period_close", "name": "on_period_close",
     "handler": "close", "signature": [...]}
    {"kind": "task", "target": "task:monthly", "name": "monthly",
     "handler": "run", "signature": [...]}

``target`` 是这条声明的**唯一标识**：宿主拿它派活，worker 拿它找处理函数。
它必须唯一 —— 早先用「handler 名字」当标识是有洞的：两个路由都注册了
``lambda`` 时它们的名字一样（``"<lambda>"``），后者会把前者顶掉，
于是**第一条路由会调到第二个函数**。所以标识由「种类 + 路径/名字 + 方法」拼成，
与 handler 名字无关。

``signature`` 是**必需的**：宿主侧要用它给转发函数伪造 ``__signature__``，
FastAPI 才能按插件原本的签名解析请求体（否则 §5.1 那个
``_with_body_payload`` 的技巧就无从谈起）。**这一条是整个兼容性里最容易
丢信息的地方** —— shadow 模式就是专门盯它的。
"""
from __future__ import annotations

import inspect

from app.plugins.ipc.signatures import describe_signature

KIND_ROUTE = "route"
KIND_HOOK = "hook"
KIND_TASK = "task"


def _handler_name(handler) -> str:
    """处理函数的名字。

    取不到就返回空串 —— **不抛异常**：插件可以传一个可调用对象（不是函数），
    那时没有 ``__name__``。这里要的是「可比对的标识」，不是「拒绝加载」。
    """
    return str(getattr(handler, "__name__", "") or "")


def route_target(path: str, methods) -> str:
    """一条路由声明的目标标识。"""
    return "route:" + path + ":" + ",".join(sorted(str(m).upper() for m in methods))


def hook_target(name: str) -> str:
    """一个钩子声明的目标标识。"""
    return "hook:" + name


def task_target(name: str) -> str:
    """一个定时任务声明的目标标识。"""
    return "task:" + name


def target_for(declaration: dict) -> str:
    """从声明算出它的目标标识。"""
    kind = declaration.get("kind")
    if kind == KIND_ROUTE:
        return route_target(declaration.get("path") or "", declaration.get("methods") or [])
    if kind == KIND_HOOK:
        return hook_target(declaration.get("name") or "")
    if kind == KIND_TASK:
        return task_target(declaration.get("name") or "")
    return ""


def resolve_target(declarations: list[dict], target: str) -> dict | None:
    """按目标标识找到那条声明（找不到就是 ``None``）。

    **不解析 target 字符串** —— 拿它去和声明里算出来的标识**逐条比对**。
    解析字符串等于把「标识的格式」变成第二份约定，而格式一变，
    两边就得分叉；比对则只有一个事实来源：声明本身。
    """
    for item in declarations:
        if item.get("target") == target:
            return item
    return None



def describe_route(path: str, methods, endpoint) -> dict:
    """一条路由的声明。"""
    normalized = sorted(str(method).upper() for method in methods)
    return {
        "kind": KIND_ROUTE,
        "target": route_target(path, normalized),
        "path": path,
        "methods": normalized,
        "handler": _handler_name(endpoint),
        "signature": describe_signature(endpoint),
    }


def describe_hook(name: str, handler) -> dict:
    """一个钩子的声明。"""
    return {
        "kind": KIND_HOOK,
        "target": hook_target(name),
        "name": name,
        "handler": _handler_name(handler),
        "signature": describe_signature(handler),
    }


def describe_task(name: str, handler) -> dict:
    """一个定时任务的声明。"""
    return {
        "kind": KIND_TASK,
        "target": task_target(name),
        "name": name,
        "handler": _handler_name(handler),
        "signature": describe_signature(handler),
    }


def sort_declarations(declarations: list[dict]) -> list[dict]:
    """排序，让两份声明**逐条可比**。

    顺序不同不算差异 —— 插件注册路由的顺序本来就与「声明的顺序」无关。
    排序键刻意用 ``(kind, path/name, methods)``，不含 ``handler``：
    handler 名字不同**是**差异，要留在比对里，不能被排序吃掉。
    """
    def key(item: dict):
        return (item.get("kind", ""), item.get("path") or item.get("name") or "",
                ",".join(item.get("methods", [])))

    return sorted(declarations, key=key)


def diff_declarations(left: list[dict], right: list[dict]) -> list[dict]:
    """比对两份声明，返回差异列表。

    比对是**逐条**的：路径、方法、handler 名、签名、目标标识、任务/钩子名。
    签名是整体比较 —— 序列化后的签名是一个列表，逐字段比容易在
    「描述里没写的字段」上出假差异，而**整体比较**的差异一眼能看懂。
    """
    def index(declarations):
        out = {}
        for item in declarations:
            key = (item.get("kind"), item.get("path") or item.get("name") or "",
                   ",".join(item.get("methods", [])))
            out.setdefault(key, []).append(item)
        return out

    left_index, right_index = index(left), index(right)
    differences: list[dict] = []

    for key in sorted(set(left_index) | set(right_index), key=lambda k: tuple(str(x) for x in k)):
        in_left, in_right = left_index.get(key), right_index.get(key)
        if in_left is None:
            differences.append({"key": list(key), "kind": "only_in_right",
                                "right": in_right[0] if in_right else None})
            continue
        if in_right is None:
            differences.append({"key": list(key), "kind": "only_in_left",
                                "left": in_left[0] if in_left else None})
            continue
        if len(in_left) != len(in_right):
            differences.append({"key": list(key), "kind": "count",
                                "left_count": len(in_left), "right_count": len(in_right)})
        first_left, first_right = in_left[0], in_right[0]
        for field in ("target", "handler", "signature"):
            if first_left.get(field) != first_right.get(field):
                differences.append({"key": list(key), "kind": "field", "field": field,
                                    "left": first_left.get(field), "right": first_right.get(field)})
    return differences


def describe_ctx_surface(ctx) -> list[dict]:
    """把**真实 ctx** 的可调用叶子路径与参数描述出来。

    宿主侧用它生成 worker 需要的 ctx 代理说明。**从真实 ctx 推导**，
    而不是另维护一张手写表 —— 手写表和真实 ctx 迟早分叉，而分叉的那一处
    恰好是「插件调了一个宿主以为不存在的方法」。

    ``plugin_id`` 是字符串属性，不是方法，不在此列。
    """
    described: list[dict] = []

    def walk(node, prefix):
        for name, value in sorted(vars(node).items()):
            if not prefix and name == "plugin_id":
                continue
            path = f"{prefix}{name}"
            if callable(value):
                described.append({
                    "method": path,
                    "params": [p.name for p in inspect.signature(value).parameters.values()],
                })
            elif hasattr(value, "__dict__"):
                walk(value, path + ".")

    walk(ctx, "")
    return described
