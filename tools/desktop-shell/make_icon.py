#!/usr/bin/env python3
"""生成壳的 .ico 图标（纯标准库，不引 Pillow）。

**为什么要自己画而不是用现成素材**：仓库里目前没有品牌图标，而
PyInstaller 的 Windows 打包要一个 ``.ico``。为了一个图标去引 Pillow
（还带一堆传递依赖）不划算 —— 一个记账程序的图标本来也就是简单的几何形状，
手写 PNG + ICO 两个格式的编码器比装个库更省事。

**画的是什么**：一个圆角方块（Element Plus 主色 #409EFF，和前端按钮同色）
加一本账簿的骨架（左侧一条竖线当书脊，右侧四条横线当行）。
它是个**占位图标** —— 正式发布前应该换成设计给的品牌图标。

**怎么用**：
    python3 make_icon.py                # 写到同目录的 shell.ico
    python3 make_icon.py out.ico        # 写到指定路径

编码格式说明：
  * ICO 容器：``ICONDIR`` + 每个尺寸一条 ``ICONDIRENTRY`` + 图像数据。
    宽高字段是**单字节**，256 要写成 0（0 表示 256）。
  * 图像数据用 PNG 嵌进 ICO —— Vista 之后的 Windows 全都认，
    而我们只面向 Win10/11，所以不必再手写 BMP/DIB 那套。
  * PNG：签名 + IHDR + IDAT + IEND，8 位 RGBA（color type 6）。
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# 前景/背景色
BG = (0x40, 0x9E, 0xFF)      # #409EFF —— Element Plus 主色
FG = (0xFF, 0xFF, 0xFF)

SIZES = (16, 24, 32, 48, 64, 128, 256)

# 超采样倍数：先按 4 倍画再降采样，边缘才不会有锯齿。
SS = 4


def _rounded_rect_alpha(x: float, y: float, size: float, radius: float) -> float:
    """点 (x, y) 落在圆角方块内的覆盖度（0 或 1）。

    用「到最近角圆心的距离 <= 半径」判定，四角之外按方块处理。
    """
    half = size / 2.0
    # 到中心的距离，用于判断在不在四个角的外接方块内
    dx = abs(x - half) - (half - radius)
    dy = abs(y - half) - (half - radius)
    if dx <= 0 and dy <= 0:
        return 1.0
    if dx <= 0 or dy <= 0:
        # 只在一个方向越界：夹回边界就是方块的一部分
        return 1.0
    return 1.0 if (dx * dx + dy * dy) <= radius * radius else 0.0


def _in_bar(x: float, y: float, x0: float, y0: float, x1: float, y1: float) -> bool:
    return x0 <= x <= x1 and y0 <= y <= y1


def render(size: int) -> bytes:
    """渲染一个尺寸，返回 RGBA 原始像素（未编码）。"""
    big = size * SS
    radius = big * 0.20

    # 账簿骨架（坐标按 big 归一化）
    spine_x0, spine_x1 = big * 0.235, big * 0.325
    spine_y0, spine_y1 = big * 0.255, big * 0.745
    line_x0, line_x1 = big * 0.405, big * 0.765
    line_thick = big * 0.062
    line_ys = (0.305, 0.435, 0.565, 0.695)

    acc = [[0, 0, 0, 0] for _ in range(size * size)]

    for py in range(big):
        for px in range(big):
            x, y = px + 0.5, py + 0.5
            if not _rounded_rect_alpha(x, y, big, radius):
                continue
            r, g, b = BG
            if _in_bar(x, y, spine_x0, spine_y0, spine_x1, spine_y1):
                r, g, b = FG
            else:
                for ly in line_ys:
                    if _in_bar(x, y, line_x0, big * ly, line_x1, big * ly + line_thick):
                        r, g, b = FG
                        break
            # 降采样：累加到 4x4 的格子
            cell = (py // SS) * size + (px // SS)
            acc[cell][0] += r
            acc[cell][1] += g
            acc[cell][2] += b
            acc[cell][3] += 255

    count = SS * SS
    out = bytearray()
    for r, g, b, a in acc:
        out += bytes((r // count, g // count, b // count, a // count))
    return bytes(out)


def encode_png(rgba: bytes, size: int) -> bytes:
    """把 RGBA 像素编成 PNG。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    stride = size * 4
    for row in range(size):
        raw.append(0)  # filter type 0 (None)
        raw += rgba[row * stride:(row + 1) * stride]

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def build_ico(sizes=SIZES) -> bytes:
    """编出完整的 .ico。"""
    images = [encode_png(render(size), size) for size in sizes]

    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(header) + 16 * len(sizes)
    entries = bytearray()
    for size, image in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 宽：256 写成 0
            0 if size >= 256 else size,   # 高
            0,        # 调色板颜色数（真彩色写 0）
            0,        # 保留
            1,        # color planes
            32,       # 位深
            len(image),
            offset,
        )
        offset += len(image)

    return bytes(header) + bytes(entries) + b"".join(images)


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "shell.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_ico()
    out.write_bytes(data)
    print(f"✅ {out}  {len(data):,} 字节  尺寸 {', '.join(str(s) for s in SIZES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
