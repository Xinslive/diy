#!/usr/bin/env python3
"""量一量发票上传那条路的**瞬时内存**（2026-10-24 性能审计）。

    .venv/bin/python scripts/measure_invoice_upload_memory.py [总字节数]

对比两种取字节的方式，各跑一遍：

* **旧**：``read_upload_sync`` —— 一次读成一个 ``bytes``；
* **新**：``save_upload_to_temp`` —— 边读边写，内存里最多一个 64 KB 的块。

两种都模拟「一批 5 个文件」。

**为什么这个对比有意义**：上传的字节流本来就已经在磁盘上了
（FastAPI 的 ``UploadFile.file`` 是 ``SpooledTemporaryFile``，超过 1 MB
自动溢写），读成 ``bytes`` 等于把已经在磁盘上的东西**又抄一份进内存**。
旧路的峰值 = 全部文件加起来；新路的峰值 = 一个块。
"""
from __future__ import annotations

import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.uploads import read_upload_sync, save_upload_to_temp  # noqa: E402


class _FakeUpload:
    """只提供 ``read_upload_sync`` 用得到的两个属性。

    **刻意不真的持有那几 MB 字节** —— 测量的是「取字节这条路的峰值」，
    而夹具自己占着内存的话，量出来的就是夹具而不是被测的东西。
    """

    def __init__(self, size: int):
        self.file = _ZeroReader(size)
        self.size = size


class _ZeroReader:
    """按需产出零字节的读者：内存里只有一个块。"""

    def __init__(self, size: int):
        self._left = size

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        if n is None or n < 0:
            n = self._left
        n = min(n, self._left)
        self._left -= n
        return b"\0" * n


def _peak(fn) -> tuple[float, int]:
    tracemalloc.start()
    tracemalloc.reset_peak()
    result = fn()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1024 / 1024, result


def main() -> int:
    total_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    per_file = total_mb * 1024 * 1024 // 5
    workdir = Path(__file__).resolve().parents[1] / "data" / "upload-measure"
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"一批 5 个文件，每个 {per_file / 1024 / 1024:.1f} MB"
          f"（合计 {total_mb} MB）")

    # 旧路：全部读成 bytes 攒在列表里
    def old() -> int:
        payloads = []
        for _ in range(5):
            payloads.append((f"f.pdf", read_upload_sync(
                _FakeUpload(per_file), limit=per_file + 1, label="发票")))
        return len(payloads)

    old_peak, n = _peak(old)
    print(f"  旧（read_upload_sync 攒 bytes）：峰值 {old_peak:.1f} MB（{n} 个文件）")

    # 新路：边读边写，每读完一个就丢掉
    def new() -> int:
        made = []
        for i in range(5):
            name = save_upload_to_temp(
                _ZeroReader(per_file), workdir, limit=per_file + 1,
                label=f"发票 {i}")
            made.append(name)
        return len(made)

    new_peak, n2 = _peak(new)
    print(f"  新（save_upload_to_temp 边读边写）：峰值 {new_peak:.1f} MB（{n2} 个文件）")

    for p in workdir.glob(".upload-*"):
        p.unlink(missing_ok=True)
    workdir.rmdir()

    if old_peak > 0:
        print(f"  → 峰值降为原来的 {new_peak / old_peak * 100:.1f}%"
              f"（省了 {old_peak - new_peak:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
