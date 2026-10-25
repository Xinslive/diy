"""插件 IPC 协议（``app.plugins.ipc.protocol``）的帧与消息契约。

对应 §7 第 6 步第 48 项的**阶段 0**：协议层先落地，两侧共用同一份实现。

这个文件盯的是**协议本身**的边界：帧编解码往返、四类坏帧、调用与响应的
配对、错误映射表的逐条、字节信封。

**为什么协议层要单独测**：它两侧都要用，而两侧都用它意味着
「一处错、两处错」。而协议层的错有个讨厌的性质 —— 它往往**不立刻报错**：
长度算错会让连接挂在半截帧上，错误码拼错会让异常变成 500，
字节没走分块会让大返回值**静默地**变成另一个值。
所以这里逐条钉死。
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi import HTTPException

from app.plugins.errors import PluginError
from app.plugins.ipc import protocol
from app.plugins.ipc.protocol import (
    CALL,
    DECLARE,
    ERROR,
    HELLO,
    INVOKE,
    PING,
    PONG,
    RESULT,
    ProtocolError,
    call,
    check_single_outstanding,
    declare,
    decode_frame,
    encode_frame,
    error,
    error_payload,
    error_to_http,
    find_bytes,
    hello,
    invoke,
    ping,
    pong,
    result,
)


# ---------------------------------------------------------------------------
# 一、帧编解码往返
# ---------------------------------------------------------------------------


def test_帧往返():
    message = hello(plugin_digest="a" * 64, sdk_version="1.2.0")
    frame = encode_frame(message)
    assert decode_frame(frame) == message


def test_帧是四字节大端长度加utf8负载():
    frame = encode_frame({"type": PING})
    assert len(frame) == 4 + len(json.dumps({"type": PING}, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8"))
    assert int.from_bytes(frame[:4], "big") == len(frame) - 4


def test_非ascii负载按utf8编码():
    message = {"type": RESULT, "id": 1, "value": {"备注": "中文"}}
    frame = encode_frame(message)
    assert decode_frame(frame) == message


def test_协议版本有明确常量():
    assert protocol.PROTOCOL_VERSION == 1
    assert protocol.MAX_FRAME == 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# 二、坏帧：四类都要挡住
# ---------------------------------------------------------------------------


def test_坏帧_长度字段与实际长度不符():
    frame = bytearray(encode_frame({"type": PING}))
    # 长度头在 [0:4]；把它改小一字节，于是「声明」与「实际」对不上。
    # （改 [4:8] 是不行的 —— 那是载荷的前 4 字节，会变成「JSON 坏了」。）
    (declared,) = (int.from_bytes(frame[:4], "big"),)
    frame[0:4] = (declared - 1).to_bytes(4, "big")
    with pytest.raises(ProtocolError, match="长度不符"):
        decode_frame(bytes(frame))


def test_坏帧_不是合法utf8():
    body = b"\xff\xfe\x00\x01"
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="UTF-8"):
        decode_frame(frame)


def test_坏帧_不是合法json():
    body = b"{not json"
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="JSON"):
        decode_frame(frame)


def test_坏帧_负载不是对象():
    body = b"[1,2,3]"
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="必须是对象"):
        decode_frame(frame)


def test_坏帧_比长度头还短():
    with pytest.raises(ProtocolError, match="太短"):
        decode_frame(b"\x00\x01")


def test_坏帧_声明超长():
    frame = (protocol.MAX_FRAME + 1).to_bytes(4, "big") + b"{}"
    with pytest.raises(ProtocolError, match="超长"):
        decode_frame(frame)


def test_坏帧_未知消息类型():
    body = json.dumps({"type": "nope"}).encode("utf-8")
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="未知消息类型"):
        decode_frame(frame)


def test_坏帧_编码时负载不可json化会指出是哪个字段():
    with pytest.raises(ProtocolError) as exc:
        encode_frame({"type": RESULT, "id": 1, "value": {"content": b"raw"}})
    assert "content" in str(exc.value)


def test_帧超长在编码这一侧就被拒():
    with pytest.raises(ProtocolError, match="超长"):
        encode_frame({"type": RESULT, "id": 1, "value": "x" * (protocol.MAX_FRAME + 10)})


# ---------------------------------------------------------------------------
# 三、流式解帧
# ---------------------------------------------------------------------------


def test_流式解帧_半条消息不产出结果():
    decoder = protocol.FrameDecoder()
    frame = encode_frame({"type": PING})
    assert decoder.feed(frame[:2]) == []
    assert decoder.feed(frame[2:]) == [{"type": PING}]
    assert decoder.pending == 0


def test_流式解帧_一次喂多条():
    decoder = protocol.FrameDecoder()
    data = encode_frame({"type": PING}) + encode_frame({"type": PONG})
    assert decoder.feed(data) == [{"type": PING}, {"type": PONG}]


def test_流式解帧_粘包时按帧边界切开():
    decoder = protocol.FrameDecoder()
    frames = [encode_frame({"type": PING}), encode_frame({"type": PONG}),
              encode_frame({"type": PING})]
    out = []
    for chunk in b"".join(frames):
        out.extend(decoder.feed(bytes([chunk])))
    assert out == [{"type": PING}, {"type": PONG}, {"type": PING}]


def test_流式解帧_超长声明直接拒():
    decoder = protocol.FrameDecoder()
    with pytest.raises(ProtocolError, match="超长"):
        decoder.feed((protocol.MAX_FRAME + 1).to_bytes(4, "big"))


# ---------------------------------------------------------------------------
# 四、调用与响应的配对
# ---------------------------------------------------------------------------


def test_call与result配对():
    request = call(7, "book_list")
    response = result(7, [{"id": 1, "name": "账套"}])
    assert request["id"] == response["id"]
    assert request["type"] == CALL and response["type"] == RESULT


def test_invoke带目标标识():
    """invoke 用**目标标识**派活（不是「路由 + 方法」两段）。

    目标标识是声明的唯一标识，worker 拿它在自己的声明表里找处理函数。
    """
    message = invoke(3, "route:/rules:GET", {"book_id": 1})
    assert message["type"] == INVOKE
    assert message["target"] == "route:/rules:GET"
    assert message["kwargs"] == {"book_id": 1}
    assert "positional" not in message


def test_invoke的两种传参形态():
    """路由用关键字，钩子与定时任务用位置 —— 两种形态不许混。"""
    keyword = invoke(4, "route:/rules:GET", {"book_id": 1})
    positional = invoke(5, "hook:on_period_close", positional=[{"book_id": 1}])
    assert keyword["kwargs"] == {"book_id": 1} and "positional" not in keyword
    assert positional["positional"] == [{"book_id": 1}] and "kwargs" not in positional


def test_带id的消息缺id会被拒():
    body = json.dumps({"type": CALL, "method": "book_list"}).encode("utf-8")
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="id"):
        decode_frame(frame)


def test_call缺method会被拒():
    body = json.dumps({"type": CALL, "id": 1}).encode("utf-8")
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="method"):
        decode_frame(frame)


def test_declare必须带声明列表():
    body = json.dumps({"type": DECLARE}).encode("utf-8")
    frame = len(body).to_bytes(4, "big") + body
    with pytest.raises(ProtocolError, match="declarations"):
        decode_frame(frame)


def test_一条连接上只允许一个未完成的调用():
    """「不做多路复用」是可执行的约束，不是口头约定。"""
    first = call(1, "book_list")
    check_single_outstanding(None, first)
    with pytest.raises(ProtocolError, match="未完成的调用"):
        check_single_outstanding(1, call(2, "book_read"))
    # 响应不占用名额
    check_single_outstanding(1, result(1, None))
    check_single_outstanding(1, error(1, {"status": 500, "detail": None}))


def test_心跳不占用未完成名额():
    check_single_outstanding(1, ping())
    check_single_outstanding(1, pong())


# ---------------------------------------------------------------------------
# 五、错误映射表：逐条
# ---------------------------------------------------------------------------


def test_错误映射_http异常原样转出():
    payload = error_payload(HTTPException(404, "账套不存在"), error_id="e1")
    assert payload["kind"] == "http"
    assert payload["status"] == 404
    assert payload["detail"] == "账套不存在"
    assert error_to_http(payload) == (404, "账套不存在")


def test_错误映射_http异常的detail结构不变():
    detail = {"code": "IMPORT_DUPLICATE", "message": "该账套已存在相同文件的导入批次"}
    payload = error_payload(HTTPException(409, detail), error_id="e1")
    assert payload["detail"] == detail
    assert error_to_http(payload) == (409, detail)


def test_错误映射_插件错误把code放进响应体():
    payload = error_payload(PluginError("PLUGIN_ROUTE_CONFLICT", "插件路由重复"), error_id="e1")
    assert payload["kind"] == "plugin"
    assert payload["status"] == 400
    assert payload["detail"] == {"code": "PLUGIN_ROUTE_CONFLICT", "message": "插件路由重复"}


def test_错误映射_其它异常只回固定文案加error_id():
    payload = error_payload(RuntimeError("数据库口令是 hunter2"), error_id="e-42")
    assert payload["kind"] == "internal"
    assert payload["status"] == 500
    assert payload["detail"] == {"detail": "插件内部错误", "error_id": "e-42"}
    # 异常消息一个字都不许漏出去
    assert "hunter2" not in json.dumps(payload, ensure_ascii=False)


def test_错误映射_堆栈不进响应体():
    payload = error_payload(ValueError("boom"), error_id="e1")
    assert "Traceback" not in json.dumps(payload, ensure_ascii=False)
    assert "boom" not in json.dumps(payload, ensure_ascii=False)


def test_错误消息本身是合法帧():
    payload = error_payload(PluginError("X", "y"), error_id="e1")
    frame = encode_frame(error(1, payload))
    decoded = decode_frame(frame)
    assert decoded["type"] == ERROR
    assert decoded["id"] == 1


# ---------------------------------------------------------------------------
# 六、字节信封：走文件，不走帧
# ---------------------------------------------------------------------------


def test_字节信封往返(tmp_path):
    envelope = protocol.write_binary_file(b"hello", directory=str(tmp_path))
    assert protocol.read_binary_file(envelope) == b"hello"
    assert envelope[protocol.BINARY_KEY]["size"] == 5


def test_字节信封文件是只读的(tmp_path):
    envelope = protocol.write_binary_file(b"hello", directory=str(tmp_path))
    path = envelope[protocol.BINARY_KEY]["path"]
    assert oct(os.stat(path).st_mode & 0o777) == "0o400"


def test_字节信封大小对不上就拒(tmp_path):
    envelope = protocol.write_binary_file(b"hello", directory=str(tmp_path))
    envelope[protocol.BINARY_KEY]["size"] = 99
    with pytest.raises(ProtocolError, match="大小不符"):
        protocol.read_binary_file(envelope)


def test_字节信封摘要对不上就拒(tmp_path):
    envelope = protocol.write_binary_file(b"hello", directory=str(tmp_path))
    envelope[protocol.BINARY_KEY]["sha256"] = "0" * 64
    with pytest.raises(ProtocolError, match="摘要不符"):
        protocol.read_binary_file(envelope)


def test_字节信封文件被换过就拒(tmp_path):
    envelope = protocol.write_binary_file(b"hello", directory=str(tmp_path))
    path = envelope[protocol.BINARY_KEY]["path"]
    os.chmod(path, 0o600)
    with open(path, "wb") as handle:
        handle.write(b"evil!")
    with pytest.raises(ProtocolError, match="大小不符|摘要不符"):
        protocol.read_binary_file(envelope)


def test_不是字节信封就拒():
    with pytest.raises(ProtocolError, match="字节信封"):
        protocol.read_binary_file({"x": 1})


def test_字节信封可以被递归找出来():
    payload = {"files": [{"name": "a", "content": protocol.write_binary_file(b"x")}]}
    found = protocol.collect_binary(payload)
    assert len(found) == 1
    assert found[0][0] == "files[0].content"


def test_找字节会指出具体位置():
    assert find_bytes({"a": {"b": [1, b"raw"]}}) == ["a.b[1]"]


def test_普通负载里没有字节():
    assert find_bytes({"a": 1, "b": ["x", None, 1.5]}) == []
