"""对账单**容器**的识别：看文件头，不看扩展名。

为什么不能信扩展名：这一批真实样例里就有三份「扩展名是假的」——


- ``泓农商9月份.xls`` / ``秋农商9月份.xls`` 其实是 **HTML**（WPS 导出的
  Excel 网页版），``.xls`` 只是文件名后缀；
- ``玲招商9月.xlsx`` 其实是 **CSV 文本**（UTF-8 BOM + CRLF + 带引号），
  网银导出时随手改的后缀。

按扩展名分发会把这三份送进错的解析器，然后报一句「文件损坏」——
而文件根本没坏，是分发错了。所以这里的判据一律是**字节**：

- ``%PDF`` → PDF
- ``D0 CF 11 E0`` → OLE2 复合文档（真 xls）
- ``PK\\x03\\x04`` → ZIP；**还要看包里有没有 ``xl/``**——docx 也是 ZIP，
  但它不是表格
- 前若干字节里出现 ``<html`` / ``<table`` → HTML
- 其余一律当文本（CSV/TSV），再交给 :func:`decode_text` 与
  :func:`sniff_delimiter` 细分

扩展名只作为**错误提示**用（「你传的是 .xls，其实是 HTML」），不参与判断。
"""
from __future__ import annotations

import io
import zipfile

CONTAINER_XLSX = "XLSX"
CONTAINER_XLS = "XLS"
CONTAINER_HTML = "HTML"
CONTAINER_CSV = "CSV"
CONTAINER_PDF = "PDF"

CONTAINER_LABELS = {
    CONTAINER_XLSX: "Excel 工作簿（xlsx）",
    CONTAINER_XLS: "Excel 工作簿（xls）",
    CONTAINER_HTML: "网页表格（HTML）",
    CONTAINER_CSV: "文本表格（CSV/TSV）",
    CONTAINER_PDF: "PDF 对账单",
}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"

#: 文本解码的尝试顺序。
#:
#: ``gb18030`` 排在 ``utf-8`` 后面是有意的：UTF-8 遇到非法字节会抛错，
#: 而 GB18030 是 GBK 的超集，能吃下几乎所有中文导出文件。``utf-16``
#: 放在 ``big5`` 前面——UTF-16 的 BOM 特征比 Big5 明显。
#:
#: ``latin-1`` 放在最后当兜底：它**永不抛错**，所以「解不出来」这种情形
#: 不会变成 500，只会解出一堆乱码——那时解析结果里没有一行能识别成日期，
#: 用户看到的是「没找到表头」，而不是「编码不认识」。
_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16", "big5", "latin-1")

#: 分隔符候选。顺序即优先级：逗号最常见，制表符次之，分号/竖线罕见。
_DELIMITERS = (",", "\t", ";", "|")


def sniff(data: bytes) -> str:
    """认出这份字节到底是什么容器。"""
    if data.startswith(_PDF_MAGIC):
        return CONTAINER_PDF
    if data.startswith(_OLE_MAGIC):
        return CONTAINER_XLS
    if data.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError):
            return CONTAINER_CSV
        # xlsx 的判据是包里有没有 xl/。docx/xlsx 都是 ZIP，只看魔数会把
        # word 文档送进 openpyxl，报一句看不懂的错。
        if any(name.startswith("xl/") for name in names):
            return CONTAINER_XLSX
        return CONTAINER_CSV
    head = data[:8192].lstrip(b"\xef\xbb\xbf\x00 \t\r\n").lower()
    if b"<html" in head or b"<table" in head or b"<!doctype html" in head:
        return CONTAINER_HTML
    return CONTAINER_CSV


def decode_text(data: bytes) -> tuple[str, str]:
    """把字节解成文本，返回 ``(文本, 编码名)``。

    解不出来也**不抛错**：返回 latin-1 的替换结果。真正的失败点在后面
    （表头认不出、日期解析不出来），那里给出的提示比「编码不认识」有用。
    """
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), "latin-1"


def sniff_delimiter(text: str) -> str:
    """猜文本表格的分隔符。

    判据不是「哪种分隔符出现得多」——一张表里逗号可能只出现在某个字段
    内部。真正的判据是**一致性**：同一份文件里每一行被同一种分隔符切成
    **同样多**的段，这个分隔符才可能是真的。

    逐个候选试一遍：取前若干行分别按它切，段数一致且大于 1 才算通过；
    通过的候选里选「平均段数最多」的（同样的段数时，逗号优先）。
    """
    lines = [line for line in text.splitlines() if line.strip()][:30]
    if not lines:
        return ","
    best = ","
    best_score = 0.0
    for index, delimiter in enumerate(_DELIMITERS):
        widths = []
        for line in lines:
            width = len(line.split(delimiter))
            if width > 1:
                widths.append(width)
        if not widths:
            continue
        if len(set(widths)) != 1:
            # 段数不一致 → 不是它。允许少数几行例外（文件尾部的汇总行
            # 常常少几个字段），只要**多数**行一致就算过。
            common = max(set(widths), key=widths.count)
            if widths.count(common) * 2 <= len(widths):
                continue
            widths = [common]
        score = float(widths[0]) - index * 0.01
        if score > best_score:
            best_score = score
            best = delimiter
    return best
