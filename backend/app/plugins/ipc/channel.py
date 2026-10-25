"""Unix socket 上的帧通道。

**为什么用 Unix socket 而不是 TCP**：两端在同一台机器上，
Unix socket 天然带**文件系统权限**（socket 文件的属主与模式），
而且不经过网络栈 —— 没有端口要分配，也没有「谁能连上来」的问题。
它也是**只能本机**的，正合「worker 就在旁边」这件事。

通道本身只管**帧**：怎么把一条消息变成字节、怎么把字节变回消息。
消息是什么、谁来应答，是上面那层的事。
"""
from __future__ import annotations

import socket
from collections import deque

from app.plugins.ipc.protocol import FrameDecoder, encode_frame

#: 一次 read 的字节数。够大又不至于让内核缓冲区一直占着。
CHUNK = 65536


class Channel:
    """一条连接上的消息收发。"""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._decoder = FrameDecoder()
        self._pending: deque[dict] = deque()
        self._closed = False

    # -- 收 ---------------------------------------------------------------

    def receive(self, *, timeout: float | None = None) -> dict:
        """收**一条**消息。

        超时抛 :class:`TimeoutError`；对端关闭抛 :class:`ConnectionError`。
        两者都不该被吞掉 —— 上层要靠它们判断「worker 还在不在」。
        """
        if self._pending:
            return self._pending.popleft()
        while True:
            messages = self._decoder.feed(self._read_some(timeout))
            if messages:
                self._pending.extend(messages[1:])
                return messages[0]

    def _read_some(self, timeout: float | None) -> bytes:
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(CHUNK)
        except socket.timeout as exc:
            raise TimeoutError("等待消息超时") from exc
        finally:
            if timeout is not None:
                self._sock.settimeout(None)
        if not data:
            raise ConnectionError("连接已关闭")
        return data

    # -- 发 ---------------------------------------------------------------

    def send(self, message: dict) -> None:
        self._sock.sendall(encode_frame(message))

    # -- 关 ---------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Channel":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
