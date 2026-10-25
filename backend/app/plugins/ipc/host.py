"""宿主侧：持有一个 worker 进程，经 Unix socket 派活。

**这一层是阶段 2 的宿主一半**
--------------------------
插件的 handler 在 worker 进程里跑；宿主这边只有一个**声明**（路径、方法、
签名）和一个**转发函数**。业务请求进来时，宿主把参数打包成 ``invoke``
消息发过去，worker 调真函数，把结果发回来。

**为什么宿主要「重建」签名**
--------------------------
FastAPI 靠 ``__signature__`` 决定「哪些是路径参数、哪些是 body 字段」。
宿主手里没有插件的函数对象，只有一份签名描述 —— 所以要把它**重建**成
一个真签名挂到转发函数上（:func:`app.plugins.ipc.signatures.rebuild_signature`）。
重建错了，插件的 POST 路由会一律 422 —— 所以重建是**严格**的：
重建不了就拒绝加载这个插件（而不是塞个 ``Any`` 上去让它行为悄悄变掉）。

**嵌套：worker 会反过来要宿主能力**
----------------------------------
插件的 handler 调 ``ctx.book_list()`` 时，worker 发一条 ``call`` 给宿主，
宿主执行真的能力再回结果。这发生在宿主**正在等 invoke 结果**的时候 ——
两个方向的「未完成调用」各有一个，正好装得下（见协议层的说明）。

**串行化**
---------
一条连接在一个方向上只允许一个未完成的调用，所以同一插件的并发请求在
宿主这边被**串行化**（一把锁）。这是隔离模型的已知代价：进程内模式下
同步 handler 是跑在线程池里并发的。插件 handler 本身是同步的，绝大多数
插件的请求量也远达不到并发成为瓶颈的程度 —— 但这个代价**写在明处**，
不装作没有。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from app.plugins.ipc.channel import Channel
from app.plugins.ipc.ctx_dispatch import dispatch_ctx_call
from app.plugins.ipc.declarations import describe_ctx_surface
from app.plugins.ipc.protocol import (
    CALL,
    DECLARE,
    ERROR,
    HELLO,
    INVOKE,
    PING,
    PONG,
    PROTOCOL_VERSION,
    RESULT,
    SURFACE,
    ProtocolError,
    error,
    error_payload,
    invoke as invoke_message,
    protocol_error_to_exception,
    result as result_message,
    surface as surface_message,
)
from app.plugins.ipc.values import decode_mapping, decode_value, encode_value

log = logging.getLogger(__name__)

#: 一次调用的超时（秒）。
#:
#: 插件 handler 可能很慢（导入批次的解析就是），但**不能无限等** ——
#: 一个卡死的插件会把宿主的请求线程一起拖住。
DEFAULT_TIMEOUT = 60.0

#: 握手超时（秒）：worker 起来要加载插件代码，慢一点是正常的，但不该无底洞。
HANDSHAKE_TIMEOUT = 60.0


class WorkerUnavailable(Exception):
    """worker 没了（进程死了、连接断了、超时了）。

    宿主把它翻译成 503 —— **不是 500**：插件不可用与「插件自己出错」是
    两件事，前者该让调用方重试，后者不该重试。
    """


def _backend_root() -> Path:
    """后端源码根（``app/`` 的上一级）。

    ``backend/app/plugins/ipc/host.py`` → parents[0]=ipc、[1]=plugins、
    [2]=app、[3]=backend。**是 [3]**。
    """
    return Path(__file__).resolve().parents[3]


def _worker_env() -> dict[str, str]:
    """worker 的环境：只留白名单里的，加上 PYTHONPATH。

    刻意**不**继承 ``DATABASE_URL`` —— worker 不该有数据库连接。
    """
    allowlist = ("PATH", "LANG", "LC_ALL", "TZ", "APP_ENV")
    env = {key: os.environ[key] for key in allowlist if key in os.environ}
    env["PYTHONPATH"] = str(_backend_root())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("APP_ENV", "test")
    return env


class WorkerHandle:
    """宿主侧持有的一个 worker 进程。"""

    def __init__(self, *, plugin_id: str, digest: str, process, channel: Channel,
                 declarations: list[dict], surface: list[dict], ctx,
                 timeout: float = DEFAULT_TIMEOUT, worker_pid: int | None = None,
                 limits: dict | None = None):
        self.plugin_id = plugin_id
        self.digest = digest
        self.declarations = list(declarations)
        self.surface = list(surface)
        self._ctx = ctx
        self._process = process
        self._channel = channel
        self._timeout = timeout
        self._next_id = 0
        self._lock = threading.Lock()
        self._closed = False
        #: worker 自己报出来的进程号 —— 证明「插件代码真的在另一个进程里」
        #: 的**唯一**凭据（进程号只能由那个进程自己报）。
        self.worker_pid = worker_pid
        #: worker **实际生效**的资源/权限限制（core dump、文件描述符、
        #: no_new_privs）。worker 自己报，可观测。
        self.limits = dict(limits or {})

    # -- 状态 --------------------------------------------------------------

    @property
    def alive(self) -> bool:
        """worker 进程还在吗。"""
        return not self._closed and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid

    # -- 派活 --------------------------------------------------------------

    def invoke(self, target: str, kwargs: dict | None = None, positional: list | None = None):
        """让 worker 调一个 handler，等它的结果。

        ``kwargs`` 与 ``positional`` 只用其中一个（见协议层的说明）：
        路由用关键字，钩子与定时任务用位置。
        """
        with self._lock:
            if not self.alive:
                raise WorkerUnavailable(f"插件 worker 已退出：{self.plugin_id}")
            self._next_id += 1
            call_id = self._next_id
            try:
                self._channel.send(invoke_message(call_id, target, kwargs, positional))
            except OSError as exc:
                raise WorkerUnavailable(f"插件 worker 不可写：{self.plugin_id}") from exc
            while True:
                try:
                    message = self._channel.receive(timeout=self._timeout)
                except TimeoutError as exc:
                    raise WorkerUnavailable(f"插件 worker 超时：{self.plugin_id}") from exc
                except ConnectionError as exc:
                    raise WorkerUnavailable(f"插件 worker 连接断开：{self.plugin_id}") from exc
                kind = message.get("type")
                if kind == RESULT and message.get("id") == call_id:
                    # **要解码**：worker 发回来的返回值可能带类型标记或字节信封。
                    return decode_value(message.get("value"))
                if kind == ERROR and message.get("id") == call_id:
                    raise protocol_error_to_exception(message.get("error") or {})
                if kind == CALL:
                    # worker 反过来要一个宿主能力 —— **嵌套**，不是并发。
                    self._serve_ctx_call(message)
                    continue
                if kind == PING:
                    self._channel.send({"type": PONG})
                    continue
                raise ProtocolError(f"worker 发来不该发的消息：{kind!r}")

    # -- 应答 ctx 调用 ------------------------------------------------------

    def _serve_ctx_call(self, message: dict) -> None:
        """执行 worker 要的宿主能力并回结果。"""
        call_id = message.get("id")
        method = message.get("method") or ""
        try:
            value = dispatch_ctx_call(self._ctx, self.surface,
                                      method,
                                      decode_mapping(message.get("params") or {}))
        except BaseException as exc:  # noqa: BLE001 —— 插件的错不能把宿主带崩
            # **要记日志**：宿主侧执行 ctx 能力失败时，堆栈只在这里。
            # 不记的话，宿主只知道「插件那边报了个内部错误」，无从查起。
            log.exception("插件 worker 的 ctx 调用失败 id=%s method=%s", self.plugin_id, method)
            self._channel.send(error(call_id, error_payload(exc, error_id="host-side")))
            return
        try:
            # **返回值要编码**（Decimal / datetime / 上传文件都在这一步变成
            # 能过 JSON 的形状），对端再还原成原来的类型。
            self._channel.send(result_message(call_id, encode_value(value)))
        except Exception as exc:  # noqa: BLE001 —— 返回值不可 JSON 化
            self._channel.send(error(call_id, error_payload(exc, error_id="host-side")))

    # -- 收尾 --------------------------------------------------------------

    def close(self) -> None:
        """关掉 worker（终止进程、关连接）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.close()
        except Exception:  # noqa: BLE001 —— 收尾不该抛
            pass
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover —— kill 之后还不退
                    pass


def start_worker(*, plugin_id: str, digest: str, entry: Path, manifest: dict, ctx,
                 timeout: float = DEFAULT_TIMEOUT,
                 handshake_timeout: float = HANDSHAKE_TIMEOUT) -> WorkerHandle:
    """起一个 worker 进程，把它跑起来，收下它的声明。

    流程：宿主监听 Unix socket → 起 worker → 接受连接 → 收 ``hello``
    → 发 ctx 表面说明 → 收 ``declare`` → 把连接交给 :class:`WorkerHandle`。

    **ctx 表面说明是宿主从真实 ctx 推导出来的**（不是手写的），worker 据此
    造出形状一样的 ctx 代理。
    """
    backend_root = _backend_root()
    workdir = tempfile.mkdtemp(prefix="cwq-worker-")
    socket_path = Path(workdir) / "worker.sock"
    manifest_path = Path(workdir) / "manifest.json"
    payload = dict(manifest)
    payload["plugin_digest"] = digest
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    server.settimeout(handshake_timeout)
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.plugins.ipc.worker_main",
             "--socket", str(socket_path),
             "--entry", str(entry),
             "--manifest", str(manifest_path)],
            env=_worker_env(), cwd=str(backend_root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _drain_streams(process, plugin_id)
        try:
            conn, _ = server.accept()
        except (socket.timeout, OSError) as exc:
            raise WorkerUnavailable(f"插件 worker 没有连上来：{plugin_id}") from exc

        channel = Channel(conn)
        ctx_surface = describe_ctx_surface(ctx)
        try:
            hello_message = channel.receive(timeout=handshake_timeout)
        except (TimeoutError, ConnectionError) as exc:
            channel.close()
            raise WorkerUnavailable(f"插件 worker 没有完成握手：{plugin_id}") from exc
        if hello_message.get("type") != HELLO:
            channel.close()
            raise ProtocolError("worker 的第一条消息不是 hello")
        if hello_message.get("protocol_version") != PROTOCOL_VERSION:
            channel.close()
            raise ProtocolError("协议版本不一致")
        channel.send(surface_message(ctx_surface))
        handle = WorkerHandle(plugin_id=plugin_id, digest=digest, process=process,
                              channel=channel, declarations=[], surface=ctx_surface,
                              ctx=ctx, timeout=timeout,
                              worker_pid=hello_message.get("pid"),
                              limits=hello_message.get("limits") or {})
        declarations: list[dict] = []
        try:
            while True:
                message = channel.receive(timeout=handshake_timeout)
                kind = message.get("type")
                if kind == DECLARE:
                    declarations = list(message.get("declarations") or [])
                    break
                if kind == CALL:
                    # 插件在 register 期间就要宿主能力 —— 正常（进程内也一样）。
                    handle._serve_ctx_call(message)
                    continue
                if kind == PING:
                    channel.send({"type": PONG})
                    continue
                if kind == ERROR:
                    channel.close()
                    # 还原成**真的异常**再抛 —— 与进程内模式一致：
                    # 插件在 register 期间调 ctx 被拒（403）时，两边报的是
                    # 同一种错，宿主日志与插件状态才读得懂。
                    raise protocol_error_to_exception(message.get("error") or {})
        except (TimeoutError, ConnectionError) as exc:
            channel.close()
            raise WorkerUnavailable(f"插件 worker 没有报出声明：{plugin_id}") from exc
        handle.declarations = declarations
        return handle
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        raise
    finally:
        server.close()
        _remove_tree(workdir)


def _remove_tree(path: str) -> None:
    """删掉 worker 的工作目录（socket 文件与 manifest）。"""
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _drain_streams(process, plugin_id: str) -> None:
    """**持续**读掉 worker 的 stdout/stderr。

    不读会出事：管道的缓冲区满了之后，worker 写日志就会**阻塞** ——
    一个只在出问题时才写日志的插件，会把 worker 卡死在一个写日志的动作上，
    症状是「插件莫名卡住」，极难查。

    所以这里起两个守护线程，一个读 stdout，一个读 stderr。
    """
    def pump(stream, level):
        if stream is None:
            return
        for line in stream:
            text = line.rstrip("\n")
            if text:
                log.log(level, "插件 worker %s：%s", plugin_id, text)

    threading.Thread(target=pump, args=(process.stderr, logging.WARNING),
                     name="plugin-worker-stderr", daemon=True).start()
    threading.Thread(target=pump, args=(process.stdout, logging.INFO),
                     name="plugin-worker-stdout", daemon=True).start()


__all__ = [
    "DEFAULT_TIMEOUT",
    "HANDSHAKE_TIMEOUT",
    "WorkerHandle",
    "WorkerUnavailable",
    "start_worker",
]
