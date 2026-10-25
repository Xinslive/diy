"""插件 IPC 协议 —— 宿主与 worker 两侧共用的**唯一一份**实现。

为什么单独一个文件
------------------
§7 第 6 步第 48 项要把插件后端代码搬进独立进程，两侧就得用同一种语言
说同一种话。这份协议**必须**只有一份：宿主与 worker 各写一份，迟早分叉，
而分叉的那一处恰好是「线上才发现」的地方 —— 错误码拼错、帧长度算错、
字节没有走分块，都是这种。

所以这里**只放纯数据与纯函数**，不依赖任何运行时设施：不碰 ``app.core.*``、
不碰数据库、不碰配置，**两侧可以原样 import 同一份**（§4.5：同一个量只允许
一个权威实现）。

帧格式
------
``4 字节大端长度 + UTF-8 JSON 负载``。

消息类型
--------
======== ========= ==========================================
type     方向       用途
======== ========= ==========================================
hello    worker→宿主 协议版本、插件 digest、SDK 版本
declare  worker→宿主 路由 / 任务 / 钩子的声明（仅装载阶段）
call     worker→宿主 插件调 ``ctx.*``
invoke   宿主→worker 宿主调插件的 handler
result   双向       正常返回
error    双向       异常
ping     双向       心跳
pong     双向       心跳应答
======== ========= ==========================================

调用与响应的配对
----------------
``call`` / ``invoke`` 带自增 ``id``，响应（``result`` / ``error``）带同一个
``id``。**每个方向**上同时只允许一个未完成的调用 —— 插件的 handler 本来就是
同步的，串行足够；多路复用留到有实测需求时再加（:func:`check_single_outstanding`
把这条约束变成可执行的检查，而不是一句口头约定）。

「按方向」而不是「按连接」是因为它们会**嵌套**：宿主派活时，worker 可能反过来
要一个宿主能力（``ctx.*``），那是 worker 在等宿主，宿主在等 worker ——
一个方向一个未完成，正好装得下。

错误映射
--------
================================= ==============================
插件侧                             宿主侧
================================= ==============================
``HTTPException(status, detail)``  原样转成 HTTP 响应（结构不变）
``PluginError(code, message, status)`` 同上（``code`` 放进响应体）
其它异常                           **500**，响应体只有
                                   ``{"detail": "插件内部错误"}``
                                   加一个 ``error_id``；**堆栈不进响应**，
                                   只进宿主日志
================================= ==============================

不可序列化的返回值
------------------
``ctx.*`` 的返回值必须能 JSON 化。宿主侧哪些方法会返回二进制，登记在
:data:`app.plugins.public_api.CTX_METHOD_SERIALIZATION`（**28 项**：26 项
``json``、2 项 ``bytes``）。字节走**文件分块**：写入一个只读临时文件，
把路径与字节数作为信封回传 —— 两侧在同一台机器上，Unix socket 只传路径
不传字节，比 base64 少一次编解码。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path

from fastapi import HTTPException

from app.plugins.errors import PluginError

#: 协议版本。两侧版本不一致时**拒绝握手**，而不是「尽力而为地通信」——
#: 版本不同意味着消息形状可能不同，硬着头皮走下去的结果是更难查的错。
PROTOCOL_VERSION = 1

#: 单帧上限（8 MB）。超出的走文件分块，不走帧。
MAX_FRAME = 8 * 1024 * 1024

# ---- 消息类型 ------------------------------------------------------------
HELLO = "hello"
DECLARE = "declare"
CALL = "call"
INVOKE = "invoke"
RESULT = "result"
ERROR = "error"
PING = "ping"
PONG = "pong"
#: 宿主 → worker：ctx 表面说明（worker 据此造出 ctx 代理）。
SURFACE = "surface"

MESSAGE_TYPES = frozenset({HELLO, DECLARE, CALL, INVOKE, RESULT, ERROR, PING, PONG, SURFACE})

#: 每个消息类型的方向。``both`` 表示双向。
MESSAGE_DIRECTIONS = {
    HELLO: "worker_to_host",
    DECLARE: "worker_to_host",
    CALL: "worker_to_host",
    INVOKE: "host_to_worker",
    SURFACE: "host_to_worker",
    RESULT: "both",
    ERROR: "both",
    PING: "both",
    PONG: "both",
}

#: 需要 ``id`` 的消息类型（请求与它的响应靠 ``id`` 配对）。
_ID_REQUIRED = frozenset({CALL, INVOKE, RESULT, ERROR})

#: 字节信封的键名。**刻意起一个不会与业务字段撞名的长键** ——
#: 载荷里的 dict 是插件自己的数据，键名要短、要像业务字段。
BINARY_KEY = "__cwq_bytes__"

#: 内部错误响应体里给运维看的固定文案。
#: **不包含任何异常细节**：堆栈与异常消息只进宿主日志。
INTERNAL_ERROR_DETAIL = "插件内部错误"


class ProtocolError(Exception):
    """协议层错误：坏帧、未知消息类型、版本不符、负载不可序列化。

    这些都是**编码错误**（两侧实现对不上），不是运行期业务错误，
    所以宿主侧一律按「内部错误」处理并记日志，不回传给插件。
    """


# ---------------------------------------------------------------------------
# 帧编解码
# ---------------------------------------------------------------------------


def encode_frame(message: dict) -> bytes:
    """把一条消息编成一帧。

    载荷里出现**不可 JSON 化**的值（典型是 ``bytes``）时抛
    :class:`ProtocolError` —— 并把**位置**说出来。裸的 ``TypeError``
    只会说「Object of type bytes is not JSON serializable」，不说是哪个字段。
    """
    if not isinstance(message, dict):
        raise ProtocolError("消息必须是对象")
    kind = message.get("type")
    if kind not in MESSAGE_TYPES:
        raise ProtocolError(f"未知消息类型：{kind!r}")
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        offenders = find_bytes(message)
        detail = f"（含不可 JSON 化的字段：{offenders}）" if offenders else ""
        raise ProtocolError(f"消息负载不可 JSON 化：{exc}{detail}") from exc
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"帧超长：{len(payload)} > {MAX_FRAME} 字节")
    return struct.pack(">I", len(payload)) + payload


def decode_frame(frame: bytes) -> dict:
    """解一帧并校验。

    四道坏帧都要挡住：**长度字段与实际长度不符**、**不是合法 UTF-8**、
    **不是合法 JSON**、**载荷不是对象**。少挡一道，坏帧就会被当成
    「一条看不懂的消息」一路传下去，最后报在一个跟根因无关的地方。
    """
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise ProtocolError("帧必须是字节")
    frame = bytes(frame)
    if len(frame) < 4:
        raise ProtocolError("帧太短：连长度头都不够")
    (length,) = struct.unpack(">I", frame[:4])
    if length > MAX_FRAME:
        raise ProtocolError(f"帧超长：声明 {length} > {MAX_FRAME} 字节")
    body = frame[4:]
    if len(body) != length:
        raise ProtocolError(f"帧长度不符：声明 {length}，实际 {len(body)}")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("帧负载不是合法 UTF-8") from exc
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("帧负载不是合法 JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("帧负载必须是对象")
    validate_message(message)
    return message


class FrameDecoder:
    """流式解帧：喂字节，拿**完整**消息。

    socket 给的是字节流，帧边界要自己找。攒够一帧才解一条 ——
    半条消息先解出来，报的错会指向「JSON 坏了」而不是「还没收完」。
    """

    def __init__(self, *, max_frame: int = MAX_FRAME):
        self._buffer = bytearray()
        self._max_frame = max_frame

    def feed(self, data: bytes) -> list[dict]:
        """喂一段字节，返回其中**完整**的消息列表。"""
        self._buffer.extend(data)
        out: list[dict] = []
        while True:
            if len(self._buffer) < 4:
                break
            (length,) = struct.unpack(">I", self._buffer[:4])
            if length > self._max_frame:
                raise ProtocolError(f"帧超长：声明 {length} > {self._max_frame} 字节")
            if len(self._buffer) < 4 + length:
                break
            frame = bytes(self._buffer[:4 + length])
            del self._buffer[:4 + length]
            out.append(decode_frame(frame))
        return out

    @property
    def pending(self) -> int:
        """还没凑齐一帧的字节数。"""
        return len(self._buffer)


# ---------------------------------------------------------------------------
# 消息构造与校验
# ---------------------------------------------------------------------------


def validate_message(message: dict) -> None:
    """校验一条消息的形状。"""
    kind = message.get("type")
    if kind not in MESSAGE_TYPES:
        raise ProtocolError(f"未知消息类型：{kind!r}")
    if kind in _ID_REQUIRED and not isinstance(message.get("id"), int):
        raise ProtocolError(f"{kind} 消息缺少整数 id")
    if kind == CALL and not isinstance(message.get("method"), str):
        raise ProtocolError("call 消息缺少 method")
    if kind == INVOKE and not isinstance(message.get("target"), str):
        raise ProtocolError("invoke 消息缺少 target")
    if kind == DECLARE and not isinstance(message.get("declarations"), list):
        raise ProtocolError("declare 消息缺少 declarations 列表")
    if kind == SURFACE and not isinstance(message.get("surface"), list):
        raise ProtocolError("surface 消息缺少 surface 列表")


def hello(*, plugin_digest: str, sdk_version: str, protocol_version: int = PROTOCOL_VERSION,
          pid: int | None = None, modules: list[str] | None = None,
          limits: dict | None = None) -> dict:
    """worker → 宿主：握手。

    ``pid`` 是**worker 自己的进程号** —— 宿主要拿它证明「插件代码真的
    跑在另一个进程里」（阶段 1 的第一条验收），而进程号只能由那个
    进程自己报。

    ``modules`` 是 worker 已经加载的**宿主侧模块名**（``app.*``）。
    宿主拿它证明「worker 没有把 ``app.core.db`` 拖进来」——
    这条不能靠「插件导不进来」证明：插件导不进来是**沙箱**的功劳，
    而「worker 自己没加载它」是**进程边界**的功劳，两件事要分开验。

    ``limits`` 是 worker **实际生效**的资源/权限限制（core dump、
    文件描述符、no_new_privs）。带上它是为了让「限制到底生效了没有」
    可观测 —— 否则「代码里写了就当它生效了」。
    """
    message = {"type": HELLO, "protocol_version": protocol_version,
               "plugin_digest": plugin_digest, "sdk_version": sdk_version}
    if pid is not None:
        message["pid"] = pid
    if modules is not None:
        message["modules"] = list(modules)
    if limits is not None:
        message["limits"] = dict(limits)
    return message


def surface(methods: list[dict]) -> dict:
    """宿主 → worker：ctx 表面说明（worker 据此造 ctx 代理）。"""
    return {"type": SURFACE, "surface": methods}


def declare(declarations: list[dict]) -> dict:
    """worker → 宿主：路由 / 任务 / 钩子的声明。"""
    return {"type": DECLARE, "declarations": declarations}


def call(call_id: int, method: str, params: dict | None = None) -> dict:
    """worker → 宿主：调一个 ``ctx.*`` 能力。"""
    return {"type": CALL, "id": call_id, "method": method, "params": params or {}}


def invoke(call_id: int, target: str, kwargs: dict | None = None,
           positional: list | None = None) -> dict:
    """宿主 → worker：调插件的 handler。

    ``target`` 是声明的**唯一标识**（见 :mod:`app.plugins.ipc.declarations`），
    worker 拿它在自己的声明表里找处理函数。

    ``kwargs`` 与 ``positional`` **只用其中一个**：

    * 路由用 ``kwargs`` —— FastAPI 给的是关键字参数，参数名就是插件自己
      写的那些（原样重建，不改名）。
    * 钩子与定时任务用 ``positional`` —— 它们在宿主里是「一个位置参数」
      的调用形态（``handler(payload)`` / ``handler(context)``），
      位置传参才能和进程内的调用**逐字对齐**。
    """
    message = {"type": INVOKE, "id": call_id, "target": target}
    if kwargs is not None:
        message["kwargs"] = kwargs
    if positional is not None:
        message["positional"] = list(positional)
    return message


def result(call_id: int, value) -> dict:
    """响应：正常返回。"""
    return {"type": RESULT, "id": call_id, "value": value}


def error(call_id: int, payload: dict) -> dict:
    """响应：异常。``payload`` 由 :func:`error_payload` 生成。"""
    return {"type": ERROR, "id": call_id, "error": payload}


def ping() -> dict:
    return {"type": PING}


def pong() -> dict:
    return {"type": PONG}


def check_single_outstanding(outstanding: int | None, message: dict) -> None:
    """**每个方向**同时只允许一个未完成的调用。

    这条约束是「不做多路复用」的具体形态：它把「串行」从口头约定变成
    可执行的检查。少了它，两侧一旦并发发起调用，配对就会错位 ——
    而且错位之后每一层的报错都指向别处。

    **为什么按方向而不是按连接**：宿主派活（``invoke``）时，worker 可能反过来
    要一个宿主能力（``call``）—— 那是**嵌套**，不是并发。所以两个方向各自
    允许一个未完成，而一个方向上不允许第二个。

    ``outstanding`` 是**该方向**当前未完成的调用号（没有就是 ``None``）。
    """
    if message.get("type") not in (CALL, INVOKE):
        return
    if outstanding is not None:
        raise ProtocolError(
            f"连接上已有未完成的调用（id={outstanding}），不允许再发起 id={message.get('id')}")


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------


def error_payload(exc: BaseException, *, error_id: str) -> dict:
    """把插件侧异常映射成协议里的 error 负载。

    三档（见模块 docstring）。**内部错误这一档不泄露任何异常细节** ——
    异常消息与堆栈只进宿主日志，响应体里只有一个 ``error_id`` 供对账。
    """
    if isinstance(exc, HTTPException):
        return {"kind": "http", "status": exc.status_code, "detail": exc.detail}
    if isinstance(exc, PluginError):
        return {"kind": "plugin", "status": exc.status,
                "detail": {"code": exc.code, "message": exc.message}}
    return {"kind": "internal", "status": 500,
            "detail": {"detail": INTERNAL_ERROR_DETAIL, "error_id": error_id}}


def error_to_http(payload: dict) -> tuple[int, object]:
    """把 error 负载还原成「状态码 + 响应体」。

    内部错误这一档**只**返回固定文案与 error_id —— 堆栈永远不进响应。
    """
    status = payload.get("status", 500)
    if not isinstance(status, int):
        status = 500
    return status, payload.get("detail")


def protocol_error_to_exception(payload: dict) -> Exception:
    """把 error 负载还原成异常（**worker 侧**用）。

    是 :func:`error_payload` 的逆运算：插件代码里 ``except HTTPException``
    那种写法，在 worker 里仍然要接得住 —— 否则插件的正常错误处理
    （「账套不存在就提示用户」）会在隔离模型下变成「未捕获异常」。
    """
    kind = payload.get("kind")
    status = payload.get("status", 500)
    detail = payload.get("detail")
    if kind == "http":
        return HTTPException(status if isinstance(status, int) else 500, detail)
    if kind == "plugin":
        code = detail.get("code", "PLUGIN_ERROR") if isinstance(detail, dict) else "PLUGIN_ERROR"
        message = detail.get("message", "插件操作失败") if isinstance(detail, dict) else "插件操作失败"
        return PluginError(code, message, status if isinstance(status, int) else 400)
    # 内部错误：细节只进宿主日志，worker 侧只知道「出错了」。
    return RuntimeError(INTERNAL_ERROR_DETAIL)


# ---------------------------------------------------------------------------
# 字节：走文件，不走帧
# ---------------------------------------------------------------------------


def find_bytes(value, path: str = "") -> list[str]:
    """找出负载里所有**不可 JSON 化**的字节值的位置。

    用来在编码失败时直接说清楚「是哪个字段」——裸的 ``TypeError`` 不会说。
    """
    found: list[str] = []
    if isinstance(value, (bytes, bytearray, memoryview)):
        found.append(path or "<root>")
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(find_bytes(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(find_bytes(item, f"{path}[{index}]"))
    return found


def write_binary_file(data: bytes, *, directory: str | None = None) -> dict:
    """把字节写进一个**只读**临时文件，返回 JSON 可表达的信封。

    两侧在同一台机器上（Unix socket），所以传**路径**而不是传字节：
    少一次编解码，也不用给大文件准备分帧逻辑。

    文件权限是 ``0400``（只读）：它是一次性传值的中转站，不是插件的
    工作目录，插件拿到的也只是一条路径。
    """
    payload = bytes(data)
    fd, path = tempfile.mkstemp(prefix="cwq-ipc-", suffix=".bin", dir=directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    os.chmod(path, 0o400)
    return {BINARY_KEY: {"path": path, "size": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest()}}


def read_binary_file(envelope: dict) -> bytes:
    """把信封还原成字节，并**核对**大小与摘要。

    核对不是多余的：信封是跨进程传来的，路径指向的文件若被换过，
    宿主会拿完全不同的内容去调真正的 ``ctx`` 方法。摘要对不上就必须拒。
    """
    meta = envelope.get(BINARY_KEY) if isinstance(envelope, dict) else None
    if not isinstance(meta, dict):
        raise ProtocolError("不是字节信封")
    path = meta.get("path")
    if not isinstance(path, str) or not path:
        raise ProtocolError("字节信封缺少路径")
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise ProtocolError(f"字节信封的文件读不出来：{path}") from exc
    if len(payload) != meta.get("size"):
        raise ProtocolError(f"字节信封大小不符：声明 {meta.get('size')}，实际 {len(payload)}")
    if hashlib.sha256(payload).hexdigest() != meta.get("sha256"):
        raise ProtocolError("字节信封摘要不符")
    return payload


def collect_binary(value, path: str = "") -> list[tuple[str, dict]]:
    """递归找出负载里所有字节信封，返回 ``(位置, 信封)`` 列表。"""
    found: list[tuple[str, dict]] = []
    if isinstance(value, dict):
        if BINARY_KEY in value and isinstance(value[BINARY_KEY], dict):
            found.append((path or "<root>", value))
            return found
        for key, item in value.items():
            found.extend(collect_binary(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(collect_binary(item, f"{path}[{index}]"))
    return found
