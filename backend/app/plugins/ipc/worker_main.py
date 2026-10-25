"""插件 worker 进程入口。

**插件后端代码在这里、在这个进程里跑。** 宿主的 API 进程不加载它。

用法::

    python -m app.plugins.ipc.worker_main \\
        --socket /run/cwq/plugin-<digest>.sock \\
        --entry  /opt/cwq/data/plugins/<id>/<digest>/backend/main.py \\
        --manifest /run/cwq/plugin-<digest>.manifest.json

**这个进程不许 import 宿主的东西。** 它只 import：
标准库、``app.plugins.ipc.*``（协议与代理）、``app.plugins.sandbox``
（裁剪内建能力的加载器）与插件自己的代码。

尤其**不许** import ``app.core.db`` —— 那会把数据库连接池拖进来，
而 worker 一个数据库连接都不该有（它要的每一样东西都经 IPC 问宿主要）。
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.plugins import API_VERSION
from app.plugins.ipc.channel import Channel
from app.plugins.ipc.declarations import resolve_target
from app.plugins.ipc.limits import apply_worker_limits
from app.plugins.ipc.protocol import (
    DECLARE,
    ERROR,
    INVOKE,
    PING,
    PONG,
    ProtocolError,
    SURFACE,
    declare,
    error,
    error_payload,
    hello,
    result,
)
from app.plugins.ipc.proxy_ctx import CtxTransport, build_proxy_ctx
from app.plugins.ipc.proxy_registrar import ProxyRegistrar
from app.plugins.ipc.values import decode_mapping, decode_value, encode_value

# **沙箱照旧**（第 49 项那一层保留）：进程隔离挡的是「打穿宿主」，
# 沙箱挡的是「在 worker 里读到 worker 自己不该有的东西」。
# 放在模块级 import，是为了握手时 ``sys.modules`` 里已经有它 ——
# 宿主要拿那份名单证明「worker 只加载了 app.plugins.*」。
from app.plugins.sandbox import exec_plugin_module

log = logging.getLogger("app.plugins.ipc.worker")

#: 与宿主的握手超时。宿主起不来时，worker 不该无限期挂着。
HANDSHAKE_TIMEOUT = 30.0

#: 主循环里「没事干」时的等待间隔。
#:
#: 它同时决定**多久检查一次宿主还在不在**（见 :func:`_serve`）—— 所以它要
#: 短一点：宿主崩了之后，孤儿 worker 最多多活这么久。
IDLE_TIMEOUT = 5.0

#: 多久检查一次「宿主还在不在」。
ORPHAN_CHECK_INTERVAL = 15.0


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.plugins.ipc.worker_main")
    parser.add_argument("--socket", required=True, help="宿主监听的 Unix socket 路径")
    parser.add_argument("--entry", required=True, help="插件后端入口 .py 的路径")
    parser.add_argument("--manifest", required=True, help="manifest JSON 的路径")
    return parser.parse_args(argv)


def _install_signal_handlers(stop) -> None:
    """``SIGTERM`` → 优雅退出。

    systemd 停服务时发的是 SIGTERM，不是 SIGKILL —— 收到它就该自己收工，
    而不是等着被强杀（被强杀会留下半个 socket 文件）。
    """

    def handler(signum, frame):  # noqa: ARG001 —— 签名是 signal API 定的
        stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _connect(path: str) -> Channel:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    return Channel(sock)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s worker %(message)s")

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    plugin_id = manifest.get("id", "")
    digest = manifest.get("plugin_digest", "")

    # 脱离终端：worker 不是交互式程序，挂在终端上会挡住 Ctrl-C，
    # 也会在父进程退出时收到 SIGHUP。
    try:
        os.setsid()
    except OSError:
        pass

    channel = _connect(args.socket)
    stopping = {"flag": False}
    _install_signal_handlers(lambda: stopping.__setitem__("flag", True))

    try:
        # **先给自己上限制**（core dump / 文件描述符 / no_new_privs），
        # 再把生效结果随 hello 报给宿主 —— 「限制到底生效了没有」是可观测的。
        limits = apply_worker_limits()
        channel.send(hello(plugin_digest=digest, sdk_version=API_VERSION, pid=os.getpid(),
                           modules=sorted(name for name in sys.modules if name.startswith("app.")),
                           limits=limits))
        first = channel.receive(timeout=HANDSHAKE_TIMEOUT)
        if first.get("type") != SURFACE:
            raise ProtocolError(f"握手后收到的不是 surface：{first.get('type')!r}")
        surface_methods = first.get("surface", [])

        # **沙箱照旧**（第 49 项那一层保留）：进程隔离挡的是「打穿宿主」，
        # 沙箱挡的是「在 worker 里读到 worker 自己不该有的东西」。
        module = exec_plugin_module("cwq_plugin_" + digest, Path(args.entry))
        register = getattr(module, "register", None)
        if not callable(register):
            raise ProtocolError("插件必须提供 register(registrar, ctx)")

        registrar = ProxyRegistrar(plugin_id)
        registrar.bind_manifest(manifest)
        transport = CtxTransport(channel)
        ctx = build_proxy_ctx(surface_methods, transport)
        register(registrar, ctx)
        channel.send(declare(registrar.declarations()))

        _serve(channel, registrar)
        return 0
    except BaseException as exc:  # noqa: BLE001 —— 插件在 register 期间炸了也要报给宿主
        # **要把失败报给宿主**：宿主据此把插件标成 FAILED 并给出原因。
        # 只把 worker 悄悄退掉的话，宿主那边看到的是「连接断了」——
        # 症状一样，但排查时少了「插件自己抛了什么」这条线索。
        try:
            channel.send(error(0, error_payload(exc, error_id=uuid.uuid4().hex[:12])))
        except OSError:
            pass
        log.error("worker 启动失败：%s", exc)
        return 1
    finally:
        channel.close()


def _serve(channel: Channel, registrar: ProxyRegistrar) -> None:
    """worker 的主循环。

    宿主派活过来（``invoke``）就在这里处理；插件要宿主能力（``call``）时
    由 :class:`app.plugins.ipc.proxy_ctx.CtxTransport` 自己收发。

    **孤儿自保**：宿主进程没了的时候，worker 会被 init 收养（``getppid``
    变成 1）。这时 worker 必须自己退出 —— 否则每次宿主重启都留下一堆
    「还在跑但没人管」的插件进程，它们还占着内存与文件描述符。
    """
    parent_pid = os.getppid()
    last_check = time.monotonic()
    while True:
        try:
            message = channel.receive(timeout=IDLE_TIMEOUT)
        except TimeoutError:
            now = time.monotonic()
            if now - last_check >= ORPHAN_CHECK_INTERVAL:
                last_check = now
                if os.getppid() != parent_pid:
                    log.warning("宿主进程已退出，worker 自行退出")
                    return
            continue
        except ConnectionError:
            return
        kind = message.get("type")
        if kind == PING:
            channel.send({"type": PONG})
        elif kind == INVOKE:
            _handle_invoke(channel, message, registrar)
        elif kind in (RESULT, ERROR):
            # 这类消息是宿主对 worker 自己发起的 call 的应答。
            # 走到这里说明已经没有对应的等待者了（register 期间的 call
            # 由 CtxTransport 自己收，不会走到这里）—— 记一条日志而不是崩。
            log.warning("收到无人认领的应答：%s", kind)


def _call_route(endpoint, kwargs: dict):
    """调一次路由处理函数 —— 同步的与异步的都支持。

    进程内模式里 FastAPI 会 await 异步处理函数；隔离模式不能因此变成
    「不支持异步路由」（那会让一份能跑的插件在隔离模式下直接 500）。

    异步的用一个**临时事件循环**跑：worker 的主循环是同步的，
    此刻这个线程上没有正在跑的循环，所以 ``asyncio.run`` 是安全的。
    """
    if inspect.iscoroutinefunction(endpoint):
        return asyncio.run(endpoint(**kwargs))
    return endpoint(**kwargs)


def _handle_invoke(channel: Channel, message: dict, registrar: ProxyRegistrar) -> None:
    """宿主派活过来了：在 worker 里真的调那个函数。"""
    call_id = message.get("id")
    error_id = uuid.uuid4().hex[:12]
    try:
        target = message.get("target") or ""
        declaration = resolve_target(registrar.declarations(), target)
        if declaration is None:
            raise ProtocolError(f"没有这个目标：{target}")
        endpoint = registrar.endpoint(target)
        if endpoint is None:
            raise ProtocolError(f"目标没有对应的处理函数：{target}")
        kind = declaration.get("kind")
        if kind == "route":
            # 路由：关键字调用，与 FastAPI 给宿主的一一对应。
            #
            # **async 处理函数也要支持** —— 进程内模式里 FastAPI 会 await 它，
            # 隔离模式不能因此变成「不支持」：worker 的主循环是同步的，
            # 所以这里自己起一个事件循环跑它。
            value = _call_route(endpoint, decode_mapping(message.get("kwargs") or {}))
        elif kind == "hook":
            # 钩子：位置调用，与进程内的 ``handler(payload)`` 一致。
            value = endpoint(*[decode_value(item) for item in message.get("positional") or []])
        elif kind == "task":
            # 定时任务：位置调用，上下文还原成 ``SimpleNamespace`` ——
            # 与进程内给插件的那个是**同一种东西**，插件里 ``ctx.run_id``
            # 照常能用。
            args = [decode_value(item) for item in message.get("positional") or []]
            context = SimpleNamespace(**(args[0] if args and isinstance(args[0], dict) else {}))
            value = endpoint(context)
        else:
            raise ProtocolError(f"不认识的目标种类：{kind!r}")
        channel.send(result(call_id, encode_value(value)))
    except BaseException as exc:  # noqa: BLE001 —— worker 不能因为插件的错就死掉
        try:
            channel.send(error(call_id, error_payload(exc, error_id=error_id)))
        except OSError:
            return
        log.exception("worker 处理调用失败 error_id=%s", error_id)


if __name__ == "__main__":  # pragma: no cover —— 进程入口
    raise SystemExit(main())
