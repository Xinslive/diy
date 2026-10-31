"""发票原件跟着凭证一起打印。

**为什么要有这一页。** 发票导入生成的凭证，原件就是它的附件（生成时
``attachments=1``），但纸面上只有一张记账凭证 —— 装订时还要一张一张翻
原始 PDF 找。打印时把原件接在凭证后面，一次印完，剪下来就能订在一起。

**为什么渲染成图片，而不是把 PDF 贴进打印页。** 打印页是一份 HTML，
浏览器只肯把它认得的图片印出来；``<embed>``/``<iframe>`` 里那份 PDF
在 Chrome 的打印预览里是**空的**（PDF 插件不参与页面打印）。所以那一页
只能是图片。

**为什么自己写 PNG 编码。** 运行环境里没有 Pillow、没有 numpy
（``requirements.lock`` 里就没有这两个），而发票是白底黑字 —— PNG 压得
极好：实测一张 A4 发票在 150 DPI 下只有 ~90 KB。
"""
from __future__ import annotations

import base64
import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.invoice import storage
from app.models import InvoiceBatch, InvoiceRecord
from app.printing.template import render_invoice_page

log = logging.getLogger("cwq.invoice.print")

#: 一次打印最多带多少张发票页。
#:
#: **为什么要有这道闸**：一张发票页就是一张 A4 的 PNG（~90 KB），走 base64
#: 进 HTML 还要再涨三分之一 —— 一次几百张就是几十 MB 的字符串，那台
#: 1.6 GB 的机器扛不住。与 Excel 通道那道行数闸是同一件事：一次打印的量
#: 必须有上限，超了就明说，不静默截断（见 ``api/printing.py`` 里用它的地方）。
MAX_INVOICE_PAGES = 200

#: 发票页的分辨率（DPI）。72 是 PDF 的单位，scale = dpi / 72。
#: 150 DPI 印在 A4 上是 1240×1750 左右 —— 小票上的字还认得清；
#: 再往上调只是让同一张纸的体积翻倍（data URI 走 base64，还要再涨三分之一）。
PAGE_DPI = 150
_PAGE_SCALE = PAGE_DPI / 72


@dataclass(frozen=True)
class InvoicePage:
    """一张发票在批次原件里的位置。"""

    voucher_id: int
    batch_id: int
    file_path: str      # 批次合并 PDF 的相对路径
    page_index: int     # 这张发票在合并 PDF 里的起始页（0 起）
    page_span: int      # 分页票占几页（1 = 整张票一页）


def _png_bytes(width: int, height: int, rows: list[bytes]) -> bytes:
    """把 RGB 行编成 PNG（每行 filter 0，靠 zlib 压）。

    **不引 Pillow**：运行环境里没有它，而发票是白底黑字 ——
    filter 0 + zlib 已经把一张 A4 压到 ~90 KB（实测）。
    """
    raw = b"".join(b"\x00" + row for row in rows)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    # IHDR：8 位、真彩（颜色类型 2）、无隔行、无透明。
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def _render_page_png(path: Path, page_index: int) -> tuple[bytes, int, int]:
    """把 PDF 的第 ``page_index`` 页（0 起）渲染成 PNG 字节，返回 (数据, 宽, 高)。

    pdfium 给的是 **BGR** 字节流（每行 ``stride`` 字节、每像素 ``n_channels``
    个通道），PNG 要 RGB —— 换通道用切片赋值做，不在 Python 层逐像素循环
    （一张 A4 有两百多万个点，逐像素循环会让每次打印多等好几秒）。
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        page = document[page_index]
        bitmap = page.render(scale=_PAGE_SCALE)
        try:
            width, height = bitmap.width, bitmap.height
            stride, channels = bitmap.stride, bitmap.n_channels
            raw = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        document.close()

    rows = []
    for y in range(height):
        row = raw[y * stride: y * stride + width * channels]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::3]      # R
        rgb[1::3] = row[1::3]      # G
        rgb[2::3] = row[0::3]      # B
        rows.append(bytes(rgb))
    return _png_bytes(width, height, rows), width, height


def page_data_uri(path: Path, page_index: int) -> tuple[str, int, int]:
    """一页发票的 data URI（PNG）与它的像素尺寸。"""
    data, width, height = _render_page_png(path, page_index)
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), width, height


def pages_by_voucher(db: Session, book_id: int,
                     voucher_ids: Iterable[int]) -> dict[int, list[InvoicePage]]:
    """这些凭证各自带着哪些发票。

    **认的是「哪张凭证带着哪张票」这条关系**：``invoice_record.voucher_id``
    指到哪张凭证，哪张凭证就带着这张发票 —— 生成凭证时它就是那张凭证的
    附件（``attachments=1``）。
    """
    ids = [int(value) for value in voucher_ids if value]
    if not ids:
        return {}
    rows = db.execute(
        select(InvoiceRecord, InvoiceBatch)
        .join(InvoiceBatch, InvoiceBatch.id == InvoiceRecord.batch_id)
        .where(InvoiceRecord.voucher_id.in_(ids), InvoiceRecord.book_id == book_id)
        .order_by(InvoiceRecord.id)
    ).all()
    out: dict[int, list[InvoicePage]] = {}
    for record, batch in rows:
        payload = record.payload or {}
        out.setdefault(record.voucher_id, []).append(InvoicePage(
            voucher_id=record.voucher_id,
            batch_id=batch.id,
            file_path=batch.file_path,
            page_index=max(0, int(record.page_index or 0)),
            page_span=max(1, int(payload.get("page_span") or 1)),
        ))
    return out


def invoice_pages_html(tpl: dict, refs: dict[int, list[InvoicePage]]) -> dict[int, list[str]]:
    """这些凭证的发票页（HTML），一张发票一页。

    **原件不在了就跳过这一张，不让整份打不出来**：凭证本身还得印 ——
    批次被作废、文件被删掉都不该让这一叠凭证印不出来。

    ``refs`` 是 :func:`pages_by_voucher` 的结果 —— **查**与**渲染**分开，
    是为了让接口层能在渲染之前先量一量这次要印多少张（量闸在那儿）。
    """
    out: dict[int, list[str]] = {}
    for voucher_id, items in refs.items():
        pages: list[str] = []
        for item in items:
            for offset in range(item.page_span):
                page_index = item.page_index + offset
                try:
                    uri, width, height = page_data_uri(
                        storage.path_for(item.file_path), page_index)
                except Exception as exc:  # noqa: BLE001 —— 原件坏了不能连累凭证
                    log.warning("发票页渲染失败（批次 %s 第 %s 页）：%s",
                                item.batch_id, page_index, exc)
                    continue
                pages.append(render_invoice_page(tpl, uri, width, height))
        if pages:
            out[voucher_id] = pages
    return out
