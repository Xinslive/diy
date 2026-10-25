"""宿主侧执行 ``ctx.*`` 调用的**唯一一份**实现。

为什么这层要单独存在
--------------------
进程隔离之后，「插件能让宿主做什么」不再由「插件拿到的是哪个 ctx 对象」
决定了 —— 而是由**插件发过来的方法名**决定。插件可以写任意字符串：

    {"type": "call", "id": 3, "method": "book_list", "params": {}}
    {"type": "call", "id": 4, "method": "__globals__", "params": {}}

于是「按名字分发」这一步**本身就是新的攻击面**。这里用两道闸挡住：

1. **白名单**：方法名必须在 ctx 表面说明里（那份说明是**从真实 ctx 推导**
   出来的，不是手写的），也就是「宿主真的有这么一个能力」。
2. **不许下划线开头**：任何一段以 ``_`` 开头就拒。白名单其实已经挡掉了
   ``__globals__``（真实 ctx 上没有这个属性），但白名单是**数据**，
   将来有人往 ctx 上加了个 ``_internal`` 就会漏；这一道是**结构性**的，
   不依赖白名单的内容。

**为什么白名单从 ctx 表面说明来而不是另写一张**：手写的表迟早和真实 ctx
分叉，而分叉的那一处恰好是「插件调了一个宿主以为不存在的方法」——
症状是插件在隔离模式下莫名其妙 ``AttributeError``，极难查。

**调用形状**：参数按**关键字**传（``ctx.upload_read(file, limit=...)``），
与进程内的调用一致。
"""
from __future__ import annotations

from app.plugins.ipc.protocol import ProtocolError


def allowed_methods(surface: list[dict]) -> set[str]:
    """ctx 表面说明里允许的方法名集合。"""
    return {str(item.get("method")) for item in (surface or []) if item.get("method")}


def check_method(method: str, surface: list[dict]) -> None:
    """校验一次 ctx 调用的方法名。

    不合规就抛 :class:`ProtocolError` —— 它是**编码/协议层**的错误，
    宿主侧一律按内部错误处理，不把细节回传给插件。
    """
    if not isinstance(method, str) or not method:
        raise ProtocolError("ctx 调用缺少方法名")
    for part in method.split("."):
        if not part:
            raise ProtocolError("ctx 方法名里有空段")
        if part.startswith("_"):
            # 结构性闸门：下划线开头的属性一律不许点名。
            raise ProtocolError("ctx 方法名不许包含下划线开头的属性")
    if method not in allowed_methods(surface):
        raise ProtocolError("ctx 方法不在白名单里")


def check_params(method: str, params: dict, surface: list[dict]) -> None:
    """校验参数名：只能是该方法真的有的那些。"""
    if not isinstance(params, dict):
        raise ProtocolError("ctx 调用参数必须是对象")
    expected = None
    for item in surface or []:
        if item.get("method") == method:
            expected = set(item.get("params") or [])
            break
    if expected is None:
        raise ProtocolError("ctx 方法不在白名单里")
    unknown = set(params) - expected
    if unknown:
        raise ProtocolError("ctx 调用带了该方法没有的参数：" + ",".join(sorted(unknown)))


def resolve(ctx, method: str):
    """按点号路径在 ctx 上找到那个真的方法。"""
    node = ctx
    for part in method.split("."):
        node = getattr(node, part)
    return node


def dispatch_ctx_call(ctx, surface: list[dict], method: str, params: dict):
    """执行一次 ctx 调用并返回结果。"""
    check_method(method, surface)
    check_params(method, params, surface)
    return resolve(ctx, method)(**params)
