"""宿主侧：起一个 worker，收集它的声明，与进程内的声明**逐条比对**。

**这是阶段 1 的全部**
-------------------
阶段 1 **不改变任何对外行为** —— 插件的业务请求仍然由进程内那份代码处理。
阶段 1 做的事只有一件：**让同一份插件代码在独立进程里也跑一遍**，
然后把两边的声明逐条比对。

比的是什么
----------
路径、方法、handler 名、**签名**、任务/钩子名。

**为什么盯签名**：宿主在阶段 2 要按声明**重建** FastAPI 路由，
而 FastAPI 靠 ``__signature__`` 决定怎么解析请求体。
序列化到 JSON 这一趟最容易丢的就是它 —— 丢了的症状是
「路由 422」「参数收不到」，而且只在切过去之后才出现。
shadow 模式就是为了在切过去**之前**把它逼出来。

**总回退开关**：``PLUGIN_ISOLATION=off`` 一行环境变量就完全回到今天的
进程内执行 —— 这就是为什么阶段 1 必须先做 shadow。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.plugins.ipc.channel import Channel
from app.plugins.ipc.ctx_dispatch import dispatch_ctx_call
from app.plugins.ipc.declarations import describe_ctx_surface, diff_declarations, sort_declarations
from app.plugins.ipc.protocol import (
    CALL,
    DECLARE,
    ERROR,
    HELLO,
    PING,
    PONG,
    PROTOCOL_VERSION,
    RESULT,
    SURFACE,
    error,
    error_payload,
    result,
    surface,
)
from app.plugins.ipc.values import decode_mapping, encode_value

#: worker 进程的环境变量白名单。
#:
#: **刻意不给** ``DATABASE_URL`` 之类的宿主机配置：worker 一个数据库连接
#: 都不该有，它要的每一样东西都经 IPC 问宿主要。
WORKER_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "APP_ENV")


@dataclass
class WorkerRun:
    """一次 worker 运行的观察结果。"""

    pid: int | None = None
    declarations: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    error: str | None = None
    returncode: int | None = None
    stderr: str = ""


def _backend_root() -> Path:
    """后端源码根（``app/`` 的上一级）。

    ``backend/app/plugins/ipc/shadow.py`` → parents[0]=ipc、[1]=plugins、
    [2]=app、[3]=backend。**是 [3]** —— [2] 会得到 ``app/`` 目录，
    worker 就会找不到 ``app`` 包（症状是「No module named 'app'」）。
    """
    return Path(__file__).resolve().parents[3]


def _worker_env() -> dict[str, str]:
    """worker 的环境：只留白名单里的，加上 PYTHONPATH。

    刻意**不**继承 ``DATABASE_URL`` —— worker 不该有数据库连接。
    """
    env = {key: os.environ[key] for key in WORKER_ENV_ALLOWLIST if key in os.environ}
    env["PYTHONPATH"] = str(_backend_root())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("APP_ENV", "test")
    return env


def _call_ctx(ctx, surface, method: str, params: dict):
    """替 worker 调一次宿主能力。

    分发走**唯一一份**实现（:mod:`app.plugins.ipc.ctx_dispatch`）——
    白名单与「不许下划线属性」那两道闸在这里，不在各个调用点里。
    """
    return dispatch_ctx_call(ctx, surface, method, decode_mapping(params))


def run_worker(*, entry: Path, manifest: dict, ctx, timeout: float = 30.0) -> WorkerRun:
    """起一个 worker 进程，让它跑插件后端代码，收集它的声明。

    流程：宿主监听 Unix socket → 起 worker → 接受连接 → 发送 ctx 表面说明
    → 应答 worker 的 ``call`` → 收 ``declare`` → 收工退场。
    """
    run = WorkerRun()
    backend_root = _backend_root()
    with tempfile.TemporaryDirectory(prefix="cwq-worker-") as tmp:
        socket_path = Path(tmp) / "worker.sock"
        manifest_path = Path(tmp) / "manifest.json"
        payload = dict(manifest)
        payload.setdefault("plugin_digest", "")
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        server.settimeout(timeout)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.plugins.ipc.worker_main",
                 "--socket", str(socket_path),
                 "--entry", str(entry),
                 "--manifest", str(manifest_path)],
                env=_worker_env(), cwd=str(backend_root),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError) as exc:
                run.error = f"worker 没有连上来：{exc}"
                process.kill()
                _drain(process, run)
                return run

            channel = Channel(conn)
            try:
                ctx_surface = describe_ctx_surface(ctx)
                channel.send(surface(ctx_surface))
                while True:
                    try:
                        message = channel.receive(timeout=timeout)
                    except TimeoutError:
                        run.error = "worker 一直没发声明"
                        break
                    except ConnectionError:
                        break
                    kind = message.get("type")
                    if kind == HELLO:
                        if message.get("protocol_version") != PROTOCOL_VERSION:
                            run.error = "协议版本不一致"
                            break
                        run.pid = message.get("pid")
                        run.modules = list(message.get("modules") or [])
                    elif kind == CALL:
                        run.calls.append({"method": message.get("method"),
                                          "params": message.get("params") or {}})
                        _serve_ctx_call(channel, ctx, ctx_surface, message, run)
                    elif kind == DECLARE:
                        run.declarations = list(message.get("declarations") or [])
                        break
                    elif kind == PING:
                        channel.send({"type": PONG})
                    elif kind == ERROR:
                        run.error = str((message.get("error") or {}).get("detail"))
                        break
            finally:
                channel.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            _drain(process, run)
            run.returncode = process.returncode
            return run
        finally:
            server.close()


def _serve_ctx_call(channel: Channel, ctx, ctx_surface, message: dict, run: WorkerRun) -> None:
    """替 worker 调一次宿主能力并回结果。"""
    call_id = message.get("id")
    try:
        value = _call_ctx(ctx, ctx_surface, message.get("method") or "", message.get("params") or {})
    except BaseException as exc:  # noqa: BLE001 —— 插件的错不能把宿主带崩
        channel.send(error(call_id, error_payload(exc, error_id="host-side")))
        return
    try:
        channel.send(result(call_id, encode_value(value)))
    except Exception as exc:  # noqa: BLE001 —— 返回值不可 JSON 化
        run.error = f"返回值不可序列化：{exc}"
        channel.send(error(call_id, error_payload(exc, error_id="host-side")))


def _drain(process: subprocess.Popen, run: WorkerRun) -> None:
    """把 worker 的 stderr 收进结果里（排查用）。"""
    try:
        _, err = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        _, err = process.communicate()
    except (ValueError, OSError):
        return
    if err:
        run.stderr = err.strip()[-4000:]


def compare(in_process: list[dict], worker: list[dict]) -> list[dict]:
    """比对两份声明，返回差异列表（空列表 = 一致）。"""
    return diff_declarations(sort_declarations(in_process), sort_declarations(worker))
