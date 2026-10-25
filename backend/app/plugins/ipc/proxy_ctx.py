"""worker 侧的 ctx 代理：每个方法 → 一条 ``call``。

插件拿到的 ``ctx`` 是一棵命名空间树（``ctx.data.insert`` / ``ctx.imports.read``
……）。代理要**造出同样形状**的一棵树，但叶子不是真能力，而是
「发一条 call、等一个 result」。

**为什么形状必须一样**：插件的代码是照着宿主的 ctx 写的 ——
``ctx.data.insert("表名", {...}, book_id=1)``。代理要是形状不一样
（少一层、参数名不同），插件一跑就 ``AttributeError``，
而这跟「隔离模型对不对」毫无关系。

所以代理的形状**由宿主给的 ctx 表面说明生成**（:func:`app.plugins.ipc.declarations.describe_ctx_surface`
从**真实 ctx** 推导出来的），不是手写的 —— 手写的迟早和真实 ctx 分叉。

**文件怎么办**：``ctx.upload_read`` 收的是 ``UploadFile``，不是 JSON 能表达的。
worker 把它读成字节、落成一个只读临时文件，把**信封**（路径 + 字节数 + 摘要）
发过去；宿主再把它还原成 ``UploadFile`` 调真正的能力。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.plugins.ipc.protocol import (
    ERROR,
    PING,
    PONG,
    RESULT,
    call as call_message,
    protocol_error_to_exception,
)
from app.plugins.ipc.values import decode_value, encode_value


class CtxTransport:
    """worker 侧发 ``call``、收 ``result`` 的那一半。"""

    def __init__(self, channel, *, timeout: float | None = 30.0):
        self._channel = channel
        self._timeout = timeout
        self._next_id = 0

    def call(self, method: str, params: dict):
        """调一个宿主能力，等它回来。

        一次只允许一个未完成的调用（协议层的约束），所以这里不需要
        多路复用 —— 也正因为没有多路复用，收到「不属于本次调用」的消息时
        只能原样抛出去，而不是默默丢掉。
        """
        self._next_id += 1
        call_id = self._next_id
        self._channel.send(call_message(call_id, method, params))
        while True:
            message = self._channel.receive(timeout=self._timeout)
            kind = message.get("type")
            if kind == RESULT and message.get("id") == call_id:
                # **要解码**：宿主发回来的值可能带类型标记（Decimal / datetime）
                # 或字节信封 —— 不解码的话，插件拿到的就是信封字典本身
                # （症状：``content.decode`` 报「dict 没有 decode」）。
                return decode_value(message.get("value"))
            if kind == ERROR and message.get("id") == call_id:
                raise protocol_error_to_exception(message.get("error") or {})
            if kind == PING:
                self._channel.send({"type": PONG})
                continue
            # 不是本次调用的响应：往上抛。
            # 默默丢掉会让宿主「以为 worker 应答了」，而它其实没应答 ——
            # 这种错最后会以「插件莫名其妙卡住」的形式出现，极难查。
            raise RuntimeError(f"收到不属于本次调用的消息：{kind!r}")


def _pack_value(value):
    """把一个参数值变成能过 JSON 的东西。

    真正的实现只有一份，在 :mod:`app.plugins.ipc.values` —— 宿主→worker
    方向要用的是同一套（上传文件与 Decimal/datetime 在两个方向上都要过 IPC）。
    """
    return encode_value(value)


def build_proxy_ctx(surface: list[dict], transport) -> SimpleNamespace:
    """按 ctx 表面说明造出代理。

    ``surface`` 是 :func:`describe_ctx_surface` 的输出，
    ``transport`` 是 :class:`CtxTransport`。
    """
    root = SimpleNamespace()
    for item in surface:
        method = item["method"]
        params = list(item.get("params", []))
        parts = method.split(".")
        node = root
        for part in parts[:-1]:
            child = getattr(node, part, None)
            if child is None:
                child = SimpleNamespace()
                setattr(node, part, child)
            node = child

        def make(method=method, params=params):
            def call(*args, **kwargs):
                if len(args) > len(params):
                    raise TypeError(f"{method} 最多接受 {len(params)} 个位置参数")
                payload = {}
                for name, value in zip(params, args):
                    payload[name] = value
                for name, value in kwargs.items():
                    if name not in params:
                        raise TypeError(f"{method} 没有名为 {name!r} 的参数")
                    payload[name] = value
                return transport.call(method, {k: _pack_value(v) for k, v in payload.items()})

            call.__name__ = parts[-1]
            return call

        setattr(node, parts[-1], make())
    return root


def unpack_upload(value):
    """宿主侧用：把 worker 发来的上传信封还原成 ``UploadFile``。"""
    from app.plugins.ipc.values import decode_value

    return decode_value(value)


__all__ = [
    "CtxTransport",
    "build_proxy_ctx",
    "unpack_upload",
]
