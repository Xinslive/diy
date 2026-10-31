"""发票原件跟着凭证一起打印。

**为什么要有这一页。** 发票导入生成的凭证，原件就是它的附件（生成时
``attachments=1``），但纸面上只有一张记账凭证 —— 装订时还要一张一张翻
原始 PDF 找。打印时把原件接在凭证后面，一次印完，剪下来就能订在一起。

**为什么渲染成图片，而不是把 PDF 贴进打印页。** 打印页是一份 HTML，
浏览器只肯把它认得的图片印出来；``<embed>``/``<iframe>`` 里那份 PDF
在 Chrome 的打印预览里是**空的**（PDF 插件不参与页面打印）。所以那一页
只能是图片。

**为什么自己写 PNG 编码。** 运行环境里没有 Pillow、没有 numpy
（``requirements.lock`` 里就没有这两个），使用标准库编码无损 PNG。
"""
from __future__ import annotations

import base64
import logging
import struct
import zlib
from collections import OrderedDict
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable

import pypdfium2 as pdfium
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.invoice import storage
from app.models import InvoiceBatch, InvoiceRecord

log = logging.getLogger("cwq.invoice.print")

#: 一次打印最多带多少张发票页。
#:
#: **为什么要有这道闸**：一张发票页就是一张 PNG，走 base64
#: 进 HTML 还要再涨三分之一 —— 一次几百张就是几十 MB 的字符串，那台
#: 1.6 GB 的机器扛不住。与 Excel 通道那道行数闸是同一件事：一次打印的量
#: 必须有上限，超了就明说，不静默截断（见 ``api/printing.py`` 里用它的地方）。
MAX_INVOICE_PAGES = 200

#: 发票页的分辨率（DPI）。72 是 PDF 的单位，scale = dpi / 72。
#: 150 DPI 印在 A4 上是 1240×1750 左右 —— 小票上的字还认得清；
#: 再往上调只是让同一张纸的体积翻倍（data URI 走 base64，还要再涨三分之一）。
PAGE_DPI = 150
_PAGE_SCALE = PAGE_DPI / 72
_CACHE_LIMIT = 32 * 1024 * 1024
_cache: OrderedDict[tuple, tuple[bytes, int, int]] = OrderedDict()
_cache_bytes = 0
# 本打印路径串行调用 PDFium，同时保护有容量上限的缓存。
_render_lock = RLock()


@dataclass(frozen=True)
class InvoicePage:
    """一张发票在批次原件里的位置。"""

    voucher_id: int
    batch_id: int
    file_path: str      # 批次合并 PDF 的相对路径
    page_index: int     # 这张发票在合并 PDF 里的起始页（0 起）
    page_span: int      # 分页票占几页（1 = 整张票一页）


@dataclass(frozen=True)
class InvoicePrintPage:
    html: str
    full_page: bool


def _png_bytes(width: int, height: int, rows: list[bytes]) -> bytes:
    """把 RGB 行编成 PNG（每行 filter 0，靠 zlib 压）。

    **不引 Pillow**：运行环境里没有它，而发票是白底黑字 ——
    使用 filter 0 + zlib 压缩，保留渲染后的 RGB 像素。
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


def _render_page_png(document: pdfium.PdfDocument, page_index: int) -> tuple[bytes, int, int]:
    """把 PDF 的第 ``page_index`` 页（0 起）渲染成 PNG 字节，返回 (数据, 宽, 高)。

    让 PDFium 直接输出 RGB，避免再在 Python 层复制、交换每行通道。
    """
    page = document[page_index]
    try:
        bitmap = page.render(scale=_PAGE_SCALE, rev_byteorder=True)
        try:
            width, height = bitmap.width, bitmap.height
            stride, channels = bitmap.stride, bitmap.n_channels
            raw = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        page.close()
    rows = [raw[y * stride:y * stride + width * channels] for y in range(height)]
    return _png_bytes(width, height, rows), width, height


def _cached_page(path: Path, page_index: int, document: pdfium.PdfDocument) -> tuple[bytes, int, int]:
    global _cache_bytes
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, page_index)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    rendered = _render_page_png(document, page_index)
    size = len(rendered[0])
    if size <= _CACHE_LIMIT:
        while _cache and _cache_bytes + size > _CACHE_LIMIT:
            _cache_bytes -= len(_cache.popitem(last=False)[1][0])
        _cache[key] = rendered
        _cache_bytes += size
    return rendered


def page_data_uri(path: Path, page_index: int) -> tuple[str, int, int]:
    """一页发票的 data URI（PNG）与它的像素尺寸。"""
    with _render_lock, pdfium.PdfDocument(str(path)) as document:
        data, width, height = _cached_page(path, page_index, document)
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


def invoice_pages_html(refs: dict[int, list[InvoicePage]]) -> dict[int, list[InvoicePrintPage]]:
    """读取原票；标准 210×140 mm 票占半页，其余票整张前置。

    打开发票打印后，原件缺失或页码错误必须报错，不能静默漏印附件。

    ``refs`` 是 :func:`pages_by_voucher` 的结果 —— **查**与**渲染**分开，
    是为了让接口层能在渲染之前先量一量这次要印多少张（量闸在那儿）。
    """
    out: dict[int, list[InvoicePrintPage]] = {}
    # 每个批次只打开一次，关闭后仅缓存 PNG 字节，不保留 PDFium 对象。
    with _render_lock, ExitStack() as stack:
        documents = {}
        for voucher_id, items in refs.items():
            pages: list[InvoicePrintPage] = []
            for item in items:
                page_index = item.page_index
                try:
                    path = storage.path_for(item.file_path)
                    if path not in documents:
                        documents[path] = stack.enter_context(pdfium.PdfDocument(str(path)))
                    document = documents[path]
                    indices = range(item.page_index, item.page_index + item.page_span)
                    sizes = []
                    for page_index in indices:
                        sizes.append(document.get_page_size(page_index))
                    # 多页原票中任何一页为长票，整张票都放在前面，续页不拆散。
                    full_page = any(abs(w * 25.4 / 72 - 210) > 2 or
                                    abs(h * 25.4 / 72 - 140) > 2 for w, h in sizes)
                    for page_index in indices:
                        data, width, height = _cached_page(path, page_index, document)
                        uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                        pages.append(InvoicePrintPage(
                            f'<img src="{uri}" width="{width}" height="{height}" alt="发票原件">',
                            full_page,
                        ))
                except (OSError, ValueError, IndexError, pdfium.PdfiumError) as exc:
                    log.warning("发票页渲染失败（批次 %s 第 %s 页）：%s",
                                item.batch_id, page_index, exc)
                    raise ValueError(
                        f"发票原件无法打印（批次 {item.batch_id}，第 {page_index + 1} 页），"
                        "请检查原件；如只打印凭证，请关闭“发票打印”"
                    ) from exc
            if pages:
                out[voucher_id] = pages
    return out
