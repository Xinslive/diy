"""把任何容器变成同一张**网格**：行 × 列的单元格。

这一层只管「把字读出来」，**不认业务**——它不知道哪一列是收入、哪一列是
余额，那是 :mod:`app.bankstatement.semantics` 的活。

分成两层的原因很实际：五种容器的读法完全不同（openpyxl / xlrd / HTML 解析器 /
csv / PDF 坐标），但读出来之后要做的事**完全一样**（找表头、映射列、抽行）。
合成一层的话，五个读法各写一遍表头识别，五个地方各错一次。

``Grid`` 里放的是**原始值**（datetime、float、str），不是文本：日期单元格
在 xlsx 里是 ``datetime`` 对象，在 xls 里是 Excel 序列号，在 CSV 里是字符串，
归一化放到语义层做，这里保持原样。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

from app.bankstatement import formats

#: 单份对账单允许的最大行数。超了直接报错，不截断——
#: 截断出来的对账单会让余额对不上，而用户看不出少了尾巴。
MAX_GRID_ROWS = 20000


@dataclass
class Grid:
    """一份表格。

    ``rows`` 是全部行（**含**表头之前的元信息行）。元信息行不丢：账号、
    户名、期间、期末余额都在那儿，是后面「本户是谁」「期末余额多少」
    的唯一来源。
    """

    container: str
    sheet: str
    rows: list[list[Any]] = field(default_factory=list)
    encoding: str = ""
    delimiter: str = ""
    notes: list[str] = field(default_factory=list)
    #: 表头以上的自由文本（账号、户名、期间、期末余额……）。
    #: 非 PDF 的容器由语义层从 ``rows`` 里切出来；PDF 由 :mod:`pdf_table`
    #: 直接给出（它的表头行不在网格里）。
    meta: list[str] = field(default_factory=list)
    #: 表体以下的自由文本（本期合计、提示语……）。
    footer: list[str] = field(default_factory=list)


def cell_text(value: Any) -> str:
    """单元格 → 文本。

    浮点数要当心：``1045.0`` 打成 ``"1045.0"`` 与 ``"1045"`` 在对账单里
    是同一个数，但 ``"1045.0"`` 拿去跟表头文本比对毫无意义。所以整数值的
    浮点统一打成不带小数点的形式，小数保留原样。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value).rstrip("0").rstrip(".")
    return str(value).strip()


# ----------------------------------------------------------------------
# Excel
# ----------------------------------------------------------------------

def _read_xlsx(data: bytes) -> list[Grid]:
    import openpyxl

    grids: list[Grid] = []
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:  # noqa: BLE001 —— openpyxl 的异常类型很杂
        raise ValueError("这不是能读的 Excel 工作簿（xlsx）") from exc
    try:
        for sheet in workbook.worksheets:
            rows: list[list[Any]] = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(list(row))
                if len(rows) > MAX_GRID_ROWS:
                    raise ValueError(f"这份表格超过 {MAX_GRID_ROWS} 行，太大了")
            grids.append(Grid(container=formats.CONTAINER_XLSX, sheet=sheet.title, rows=rows))
    finally:
        workbook.close()
    return grids


def _read_xls(data: bytes) -> list[Grid]:
    import xlrd

    grids: list[Grid] = []
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("这不是能读的 Excel 工作簿（xls）") from exc
    for sheet in book.sheets():
        rows: list[list[Any]] = []
        for index in range(sheet.nrows):
            row = []
            for column in range(sheet.ncols):
                cell = sheet.cell(index, column)
                value: Any = cell.value
                # xls 的日期是「序列号 + 书里的 datemode」，直接读出来是个
                # 浮点数。这里就换成真的日期对象，别把「日期怎么算」的知识
                # 泄漏到语义层。
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        value = xlrd.xldate_as_datetime(value, book.datemode)
                    except Exception:  # noqa: BLE001
                        pass
                row.append(value)
            rows.append(row)
            if len(rows) > MAX_GRID_ROWS:
                raise ValueError(f"这份表格超过 {MAX_GRID_ROWS} 行，太大了")
        grids.append(Grid(container=formats.CONTAINER_XLS, sheet=sheet.name, rows=rows))
    return grids


# ----------------------------------------------------------------------
# HTML（网银导出成 Excel 网页版的那种）
# ----------------------------------------------------------------------

class _TableCollector(HTMLParser):
    """把 ``<table>`` 里的行抽出来。

    只用标准库的 :mod:`html.parser`——不引 lxml、不引 bs4：这个模块要能被
    插件与宿主的裁剪环境共用，多一个依赖就多一处装不上。

    处理 ``colspan`` / ``rowspan``：跨列的格子占住后面几列，跨行的格子在
    下面几行重复出现。没有这两样，HTML 表格的列会整个错位。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._colspan = 1
        self._rowspan = 1
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            mapping = {key.lower(): (value or "") for key, value in attrs}
            try:
                self._colspan = max(1, int(mapping.get("colspan", "1") or "1"))
            except ValueError:
                self._colspan = 1
            try:
                self._rowspan = max(1, int(mapping.get("rowspan", "1") or "1"))
            except ValueError:
                self._rowspan = 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth == 1 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None:
            text = "".join(self._cell)
            text = re.sub(r"[ \t\u3000]+", " ", text).strip()
            if self._row is None:
                self._row = []
            for _ in range(self._colspan):
                self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is None:
                self._table = []
            self._table.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _read_html(data: bytes) -> list[Grid]:
    text, encoding = formats.decode_text(data)
    collector = _TableCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception:  # noqa: BLE001 —— html.parser 遇到畸形 HTML 会抛
        pass
    grids: list[Grid] = []
    for index, table in enumerate(collector.tables):
        rows = [list(row) for row in table]
        if len(rows) > MAX_GRID_ROWS:
            raise ValueError(f"这份表格超过 {MAX_GRID_ROWS} 行，太大了")
        grids.append(Grid(container=formats.CONTAINER_HTML, sheet=f"表{index + 1}",
                          rows=rows, encoding=encoding))
    if not grids:
        raise ValueError("这份 HTML 里没找到表格")
    return grids


# ----------------------------------------------------------------------
# 文本表格
# ----------------------------------------------------------------------

def _read_csv(data: bytes) -> list[Grid]:
    text, encoding = formats.decode_text(data)
    delimiter = formats.sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        rows.append([cell.strip() for cell in row])
        if len(rows) > MAX_GRID_ROWS:
            raise ValueError(f"这份表格超过 {MAX_GRID_ROWS} 行，太大了")
    if not rows:
        raise ValueError("这份文件是空的")
    return [Grid(container=formats.CONTAINER_CSV, sheet="文本表", rows=rows,
                 encoding=encoding, delimiter=delimiter)]


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------

def _read_pdf(data: bytes) -> list[Grid]:
    from app.bankstatement import pdf_table

    grid = pdf_table.extract_grid(data)
    if grid is None:
        raise ValueError(
            "这份 PDF 取不到表格（没有文本层，或版面认不出来）。"
            "可以先在电脑上把对账单导出成 Excel 再上传")
    return [grid]


# ----------------------------------------------------------------------

def read_grids(data: bytes) -> list[Grid]:
    """把一份文件读成若干张网格（多 sheet / 多表格时不止一张）。

    返回多张是刻意的：光大那种文件有三个 sheet（后两个是空的），
    农商那种网页版可能一张文件里有好几张表。**挑哪一张是语义层的活** —
    这里不做判断，只把候选都交出去。
    """
    container = formats.sniff(data)
    if container == formats.CONTAINER_XLSX:
        grids = _read_xlsx(data)
    elif container == formats.CONTAINER_XLS:
        grids = _read_xls(data)
    elif container == formats.CONTAINER_HTML:
        grids = _read_html(data)
    elif container == formats.CONTAINER_PDF:
        grids = _read_pdf(data)
    else:
        grids = _read_csv(data)
    if not grids:
        raise ValueError("这份文件里没有可读的数据")
    return grids
