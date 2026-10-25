"""跨进程传值：**只有一份**编解码实现，两个方向共用。

为什么单独一个文件
------------------
上传文件（``UploadFile``）不是 JSON 能表达的东西，而它在**两个方向**上
都要过 IPC：

* worker → 宿主：插件调 ``ctx.upload_read(file)``，把文件交给宿主读；
* 宿主 → worker：插件的路由声明了 ``file: UploadFile = File(...)``，
  FastAPI 在宿主侧解析出文件，handler 在 worker 里要拿到**同一个**文件。

两个方向的处理是同一件事（字节 → 信封 / 信封 → 字节），所以必须只有
一份实现 —— 两份实现迟早在某一边漏掉「嵌套在 dict 里的文件」。

**类型也要保住**
----------------
这是比「文件」更要紧的一件事。``ctx.data.read`` 返回的是数据库行，
里面的金额是 ``Decimal``、时间是 ``datetime``。如果直接 ``json.dumps``：

* ``Decimal`` → **直接抛异常**（``json`` 不认识它）→ 整个调用失败；
* 就算退而求其次用 ``float`` 顶替 → 插件里 ``Decimal("1.50")`` 变成
  ``1.5`` → ``str()`` 出来的文本从 ``"1.50"`` 变成 ``"1.5"`` →
  **插件写进账里的金额与用户在界面上看到的对不上**。

所以这里用**带类型标记**的编码：``Decimal`` / ``datetime`` / ``date`` /
``UUID`` 各自带上一个标记，对端按标记**还原成原来那个 Python 类型**。
「行为不变」这个承诺，在值这一层也要兑现。

编码的形状
----------
``UploadFile`` →::

    {"__cwq_upload__": True, "filename": ..., "content_type": ...,
     "bytes": {"__cwq_bytes__": {"path": ..., "size": ..., "sha256": ...}}}

带类型的标量 →::

    {"__cwq_typed__": "decimal", "value": "1.50"}

**递归**：文件与带类型的值可能藏在 dict / list 里（``{"rows": [file]}``），
所以编解码都要递归下去。
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.plugins.ipc.protocol import BINARY_KEY, read_binary_file, write_binary_file

#: 上传信封的键名。**刻意起一个不会与业务字段撞名的长键** ——
#: 载荷里的 dict 是插件自己的数据，键名要短、要像业务字段。
UPLOAD_KEY = "__cwq_upload__"

#: 类型标记的键名。同样刻意起长名。
TYPED_KEY = "__cwq_typed__"


def is_upload_envelope(value) -> bool:
    """这个值是不是上传信封。"""
    return isinstance(value, dict) and bool(value.get(UPLOAD_KEY))


def is_binary_envelope(value) -> bool:
    """这个值是不是字节信封。"""
    return isinstance(value, dict) and isinstance(value.get(BINARY_KEY), dict)


def is_typed_envelope(value) -> bool:
    """这个值是不是带类型标记的信封。"""
    return isinstance(value, dict) and TYPED_KEY in value


def encode_value(value):
    """把一个 Python 值变成能过 JSON 的东西。

    * ``UploadFile`` → 文件信封（字节走只读临时文件，socket 上只传路径）；
    * ``Decimal`` / ``datetime`` / ``date`` / ``UUID`` → 带类型标记，
      对端能**还原成同一个类型**；
    * dict / list / tuple → 递归；
    * 其余（``None`` / ``bool`` / ``int`` / ``float`` / ``str``）→ 原样。
    """
    if isinstance(value, StarletteUploadFile):
        raw = value.file.read()
        try:
            value.file.seek(0)
        except (AttributeError, OSError):  # pragma: no cover —— 不可 seek 的流
            pass
        return {
            UPLOAD_KEY: True,
            "filename": value.filename,
            "content_type": value.content_type,
            "bytes": write_binary_file(raw),
        }
    if isinstance(value, Decimal):
        return {TYPED_KEY: "decimal", "value": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        # **字节走文件信封**：JSON 表达不了二进制，而 base64 会让大文件
        # 在内存里翻倍。两侧在同一台机器上，socket 上只传路径。
        #
        # ``write_binary_file`` 返回的**已经是**信封本体，不要再包一层。
        return write_binary_file(bytes(value))
    if isinstance(value, datetime):
        return {TYPED_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {TYPED_KEY: "date", "value": value.isoformat()}
    if isinstance(value, UUID):
        return {TYPED_KEY: "uuid", "value": str(value)}
    if isinstance(value, dict):
        return {key: encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    return value


def decode_value(value):
    """把编码后的值还原成原来那个 Python 值。"""
    if is_upload_envelope(value):
        raw = read_binary_file(value["bytes"])
        headers = Headers({"content-type": value.get("content_type") or "application/octet-stream"})
        return StarletteUploadFile(
            file=io.BytesIO(raw),
            filename=value.get("filename") or "upload.bin",
            headers=headers,
        )
    if is_binary_envelope(value):
        return read_binary_file(value)
    if is_typed_envelope(value):
        kind = value.get(TYPED_KEY)
        raw = value.get("value")
        try:
            if kind == "decimal":
                return Decimal(raw)
            if kind == "datetime":
                return datetime.fromisoformat(raw)
            if kind == "date":
                return date.fromisoformat(raw)
            if kind == "uuid":
                return UUID(raw)
        except (TypeError, ValueError, ArithmeticError):
            # 还原不了就退回原始文本 —— 宁可给一个字符串，也不要在这里把
            # 整个调用炸掉（那会让「对端格式不对」变成「插件 500」）。
            return raw
        return raw
    if isinstance(value, dict):
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def decode_mapping(params: dict) -> dict:
    """把一整个参数字典解码（上传信封 + 类型标记）。"""
    return {key: decode_value(item) for key, item in (params or {}).items()}


def encode_mapping(params: dict) -> dict:
    """把一整个参数字典编码。"""
    return {key: encode_value(item) for key, item in (params or {}).items()}
