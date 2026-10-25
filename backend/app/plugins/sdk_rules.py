"""插件 SDK 契约的**唯一**实现。

这里放的是「**什么样的插件代码算合法**」的全部判据：
路由路径的形状、允许的 HTTP 方法、处理函数的签名约束、
定时任务与钩子的处理函数约束，以及「POST 的 body 怎么分发」的那套包装。

为什么单独一个文件
------------------
这些判据**语义上属于 SDK 契约**，不是「加载器顺手做的事」。
现在它们住在 :mod:`app.plugins.loader` 的私有方法里，于是：

* 只有一个地方能用它们 —— 别的层要判断「这个插件写法合不合法」，
  就只能复制一份，而复制的那份必然慢慢和原件分叉；
* §7 第 6 步第 48 项（插件后端代码搬到独立进程）一旦实施，
  **worker 侧也要执行同一套判据** —— 否则等于把校验挪到了不可信的一侧：
  插件自己声明的路由形状、签名，都由插件自己说了算，那这道闸就是装饰。

所以这里只依赖 ``fastapi`` / ``starlette`` / 标准库，**不碰** ``app.core.*``
（worker 进程不需要数据库，也不需要读宿主配置）。

**行为不变**：这只是把既有判据搬到一处，宿主侧的调用点全部改成调这里。
"""
from __future__ import annotations

import inspect as pyinspect
import re
from functools import wraps
from typing import Any, Dict, List

from fastapi import Body, HTTPException
from fastapi.params import File as FastapiFile, Form as FastapiForm
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.plugins.errors import PluginError

#: 合成 body 载体时用的形参名前缀。
#:
#: 插件的 POST 处理函数可能全是标量（``def undo_run(book_id: int, run_id: int)``），
#: 没有一个 ``dict`` 形参可接整个 body —— 这时合成一个**占位**形参把 body 接住，
#: 标量参数照旧留在查询串上。这个名字会出现在改写后的签名里，
#: 所以它是**公开**的（测试要断言它）。
BODY_ANCHOR = "cwq_body"

#: 插件路由允许的 HTTP 方法。
ALLOWED_ROUTE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

#: 插件处理函数**不许**接收的参数类型。
#:
#: 这些都是宿主的对象：``Request`` 能读原始请求与鉴权头，``Session``
#: 能直接开数据库事务，``Response`` 能绕过宿主的响应封装。
#: 插件要的是「受限 API」，不是「宿主对象」。
FORBIDDEN_PARAMETER_TOKENS = ("Request", "Response", "Session", "WebSocket", "BackgroundTasks")


def path_param_names(path: str | None) -> set[str]:
    """路径里的 ``{name}`` 占位符名字集合。"""
    return set(re.findall(r"\{([a-z][a-z0-9_]*)\}", path or ""))


def is_dict_annotation(annotation) -> bool:
    """注解是不是 ``dict``（含 ``dict[str, Any]`` 这种带泛参的写法）。"""
    if annotation is dict or annotation == "dict":
        return True
    return getattr(annotation, "__origin__", None) is dict


def is_upload_param(param) -> bool:
    """文件/表单参数：multipart 请求体由 FastAPI 自己拆，不能碰。"""
    if isinstance(param.default, (FastapiFile, FastapiForm)):
        return True
    for candidate in (param.annotation, getattr(param.annotation, "__origin__", None)):
        if isinstance(candidate, type) and issubclass(candidate, StarletteUploadFile):
            return True
    return False


# ---------------------------------------------------------------------------
# 一、路由判据
# ---------------------------------------------------------------------------


def validate_route_path(path, plugin_id: str) -> None:
    """路由路径必须是**插件自己命名空间内**的相对路径。

    两条独立的要求：

    1. 形状受限（只允许小写字母/数字/下划线/连字符，以及 ``{param}`` 占位）——
       防止插件把路由挂到宿主的前缀上，或用 ``..`` 之类的形状往外爬；
    2. ``/_host`` 是**宿主服务保留**前缀 —— 插件挂上去就能遮住宿主的接口。
    """
    if not isinstance(path, str) or not re.fullmatch(
        r"/(?:[a-z0-9_-]+|\{[a-z][a-z0-9_]*\})"
        r"(?:/(?:[a-z0-9_-]+|\{[a-z][a-z0-9_]*\}))*|/",
        path,
    ):
        raise PluginError("INVALID_PLUGIN_ROUTE", "插件路由必须是命名空间内的相对路径")
    if path == "/_host" or path.startswith("/_host/"):
        raise PluginError("INVALID_PLUGIN_ROUTE", "_host 是宿主服务保留路由")
    if not plugin_id:
        raise PluginError("INVALID_PLUGIN_ROUTE", "插件路由缺少插件标识")


def validate_route_methods(methods) -> None:
    """只允许那五个 HTTP 方法。"""
    if not set(methods) <= ALLOWED_ROUTE_METHODS:
        raise PluginError("INVALID_PLUGIN_ROUTE", "插件路由方法不允许")


def validate_endpoint_signature(endpoint) -> None:
    """处理函数不许接收宿主对象或自定义依赖。"""
    for param in pyinspect.signature(endpoint).parameters.values():
        annotation = str(param.annotation)
        if (any(token in annotation for token in FORBIDDEN_PARAMETER_TOKENS)
                or hasattr(param.default, "dependency")):
            raise PluginError(
                "INVALID_PLUGIN_ROUTE",
                "插件公开接口不能接收宿主 request/session 或自定义依赖",
            )


def validate_task_handler(handler) -> None:
    """定时任务处理函数：必须是**同步**函数，且只收一个 ``TaskContext``。"""
    if (not callable(handler) or pyinspect.iscoroutinefunction(handler)
            or pyinspect.isgeneratorfunction(handler)
            or pyinspect.isasyncgenfunction(handler)):
        raise PluginError(
            "INVALID_PLUGIN_TASK",
            "插件定时任务未声明、重复或不能是异步函数",
        )
    try:
        params = list(pyinspect.signature(handler).parameters.values())
    except (ValueError, TypeError) as exc:
        raise PluginError("INVALID_PLUGIN_TASK", "定时任务处理函数签名无效") from exc
    if len(params) != 1 or params[0].kind not in (
        pyinspect.Parameter.POSITIONAL_ONLY,
        pyinspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise PluginError(
            "INVALID_PLUGIN_TASK",
            "定时任务处理函数必须只接收一个 TaskContext 参数",
        )


def validate_hook_handler(handler) -> None:
    """钩子处理函数：必须是可调用的**同步**函数。"""
    if (not callable(handler) or pyinspect.iscoroutinefunction(handler)):
        raise PluginError(
            "INVALID_PLUGIN_HOOK",
            "插件 hook 未声明、重复或不在白名单中",
        )


# ---------------------------------------------------------------------------
# 二、POST 的 body 分发
# ---------------------------------------------------------------------------


def build_body_payload_wrapper(endpoint, path=None, methods=None):
    """让插件 POST 处理函数能按 SDK 约定从**请求体**收到前端提交的字段。

    SDK 约定前端用 JSON body 提交（``host.api.post(url, data)``，见
    docs/plugin-guide.md），处理函数按名字收这些字段：既可能写成文档里的
    ``def save_note(payload: dict)`` 从 payload 里取，也可能像两个插件实际那样写成
    ``def save_rule(book_id: int, payload: dict)``。但 FastAPI 只按注解决定参数
    去处——标了 ``dict`` 又没写 ``Body()`` 的按**查询参数**，标量没写 ``Body()`` 的
    更是**必填查询参数**。于是 body 里的一切都收不到，接口一律回 422
    ``Field required``：文档、插件客户端、插件三方的约定其实是一致的，只有路由
    装配这一步漏了。

    这里不改插件的函数对象（它是别人写的，且签名已被验过），而是套一层
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
    if any(is_upload_param(p) for p in params):
        return endpoint
    path_params = path_param_names(path)
    head = [p for p in params if p.name in path_params]
    body_params = [p for p in params if p.name not in path_params]
    if not body_params:
        return endpoint
    for param in head:
        if param.default is not pyinspect.Parameter.empty:
            # 路径参数不该有默认值，有就说明签名写得古怪，别自作主张。
            return endpoint

    dict_params = [p for p in body_params if is_dict_annotation(p.annotation)]
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
        carrier = BODY_ANCHOR
        taken = {p.name for p in params}
        suffix = 0
        while carrier in taken:
            suffix += 1
            carrier = BODY_ANCHOR + str(suffix)
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
