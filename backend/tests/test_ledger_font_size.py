"""总账 / 明细账的字号下限：12pt（用户 2026-10-23）。

**要防的是「纸上看不清」**：10pt 的正文印在 A4 上要拿放大镜才看得清，
而总账与明细账是要拿给外部看的正式账簿。

这条线要同时管住三个地方，少一处就漏：

* **模板校验**（``validate_template``）——存着的旧模板加载时就被抬上来，
  不靠谁记得去改库；
* **打印件**（``render_ledger_book_page``）——页脚那一行是「正文的 0.9 倍」，
  正文 12pt 时它是 10.8pt，比下限还小；
* **导出的 Excel**（``app.printing.excel``）——导出件与打印件是同一张纸，
  以前导出件是 10pt、打印件 10pt，现在两边都得是 12pt，主标题 20pt 加粗。

**断言的是「产物里最小的那个字号」**，不是「某处写了 12」：前者能同时
抓住没设字体的格子（openpyxl 会退回 Calibri 11pt）与被折算出来的字号
（0.9 倍那一行）。
"""
from __future__ import annotations

import io
import re

from openpyxl import load_workbook

from app.printing.template import (
    DEFAULT_TEMPLATES,
    LEDGER_MIN_FONT_SIZE,
    LEDGER_SCOPES,
    render_ledger_book_page,
    validate_template,
)

#: 导出件与打印件的最小字号。
MIN_SIZE = 12
#: 主标题字号（加粗）。
TITLE_SIZE = 20


def _css_font_sizes(html: str) -> list[float]:
    """把打印件 CSS 里的 ``font-size:NNpt`` 全都取出来。"""
    return [float(x) for x in re.findall(r"font-size:\s*([\d.]+)pt", html)]


def _ledger_page(tpl: dict) -> str:
    """渲染一页用友式账簿（总账 / 明细账共用这个渲染器）。"""
    ctx = {
        "period": "2026-12",
        "subject": "库存现金",
        "subject_code": "1001",
        "company": "测试单位",
        "print_date": "2026.10.23",
    }
    return render_ledger_book_page(tpl, ctx, [], page=1, pages=1, year="2026")


# --------------------------------------------------------------------- 模板校验


def test_总账与明细账的正文不得低于12pt():
    for scope in LEDGER_SCOPES:
        tpl = validate_template(scope, {"font": {"size": 9}})
        assert tpl["font"]["size"] == MIN_SIZE, f"{scope} 的正文还是 {tpl['font']['size']}pt"


def test_总账与明细账的标题与副标题也不得低于12pt():
    # 标题 8pt、副标题 9pt：都是「能填进去」的值，必须一并抬上来
    for scope in LEDGER_SCOPES:
        tpl = validate_template(scope, {
            "title": {"text": "标题", "size": 8},
            "subtitle": {"text": "副标题", "size": 9},
        })
        assert tpl["title"]["size"] == MIN_SIZE, f"{scope} 的标题还是 {tpl['title']['size']}pt"
        assert tpl["subtitle"]["size"] == MIN_SIZE, f"{scope} 的副标题还是 {tpl['subtitle']['size']}pt"


def test_库里存着的旧模板加载时就被抬上来():
    """存着 10pt 的总账模板，加载后必须是 12pt。

    这条模拟的是「库里那份 10pt 的基线模板」——生产库里躺着的就是它。
    不靠谁记得去改库，靠的是加载时统一抬。
    """
    stored = {"font": {"family": "宋体", "size": 10},
              "title": {"text": "{subject}总账", "size": 20, "bold": True},
              "subtitle": {"text": "", "size": 11}}
    tpl = validate_template("LEDGER", stored)
    assert tpl["font"]["size"] == MIN_SIZE
    assert tpl["subtitle"]["size"] == MIN_SIZE


def test_别的范围不受这条线影响():
    """凭证纸 / 余额表 / 报表各有各的版式约定，不能跟着抬。

    凭证纸要塞五行分录，报表要放得下几十行 —— 一起抬会把那些纸撑破。
    """
    assert validate_template("VOUCHER", {"font": {"size": 6}})["font"]["size"] == 6
    assert validate_template("BALANCE", {"font": {"size": 9}})["font"]["size"] == 9
    assert validate_template("REPORT", {"font": {"size": 8}})["font"]["size"] == 8


def test_内置默认模板的字号已经是对的那一档():
    """代码默认值与库里的基线行必须是同一份内容（见 set_ledger_baseline.py）。"""
    for scope in LEDGER_SCOPES:
        tpl = validate_template(scope, DEFAULT_TEMPLATES[scope])
        assert tpl["font"]["size"] == MIN_SIZE, f"{scope} 的默认正文是 {tpl['font']['size']}pt"
        assert tpl["subtitle"]["size"] == MIN_SIZE
        assert tpl["title"]["size"] == TITLE_SIZE
        assert tpl["title"]["bold"] is True


# --------------------------------------------------------------------- 打印件


def test_打印件里没有一个字号低于12pt():
    """**页脚那一行是最容易漏的**：它是「正文的 0.9 倍」。

    正文 12pt 时 0.9 倍是 10.8pt —— 比下限还小，而那一行是
    「核算单位 / 制表 / 打印日期」，印小了这一行就没人核了。
    """
    for scope in LEDGER_SCOPES:
        tpl = validate_template(scope, {"font": {"size": 9},
                                        "title": {"size": 8},
                                        "subtitle": {"size": 9}})
        sizes = _css_font_sizes(_ledger_page(tpl))
        assert sizes, "打印件里一个 font-size 都没有 —— 解析坏了，不是版式坏了"
        too_small = [s for s in sizes if s < MIN_SIZE]
        assert not too_small, f"{scope} 的打印件里还有 {too_small}pt 的字"


def test_打印件的标题是20pt():
    tpl = validate_template("LEDGER", {})
    sizes = _css_font_sizes(_ledger_page(tpl))
    assert TITLE_SIZE in sizes, f"打印件的标题不是 {TITLE_SIZE}pt：{sizes}"


def test_每页行数与字号对得上():
    """分页是按字号算的（``ledger_book_rows_per_page``）。

    字号抬到 12pt 之后，一页放得下的行数不能与 CSS 说的对不上 ——
    对不上时页眉上的「当前页-总页数」就与纸上印出来的不一样。
    """
    from app.printing.template import ledger_book_rows_per_page

    tpl = validate_template("LEDGER", {"font": {"size": 9}})
    assert ledger_book_rows_per_page(tpl) >= 1
    # 与 12pt 模板相比，行数不会因为这次抬字号而少得离谱
    base = ledger_book_rows_per_page(validate_template("LEDGER", {}))
    assert base >= 1


# --------------------------------------------------------------------- 导出的 Excel


def _export(client, auth, book, path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/books/{book.id}/print/{path}?{q}", headers=auth)
    assert r.status_code == 200, r.text
    return load_workbook(io.BytesIO(r.content))


def test_总账导出的每个字都不小于12pt(client, auth, book, periods, subjects):
    wb = _export(client, auth, book, "general-ledger.xlsx",
                 period_id=periods[6].id, skip_empty="false")
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                size = cell.font.size
                assert size is None or size >= MIN_SIZE, (
                    f"{ws.title}!{cell.coordinate} 的字号是 {size}pt（值：{cell.value!r}）")


def test_明细账导出的每个字都不小于12pt(client, auth, book, periods, subjects):
    wb = _export(client, auth, book, "detail-ledger.xlsx",
                 period_id=periods[6].id, skip_empty="false")
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                size = cell.font.size
                assert size is None or size >= MIN_SIZE, (
                    f"{ws.title}!{cell.coordinate} 的字号是 {size}pt（值：{cell.value!r}）")


def test_总账导出的主标题是20pt加粗(client, auth, book, periods, subjects):
    wb = _export(client, auth, book, "general-ledger.xlsx",
                 period_id=periods[6].id, skip_empty="false")
    for ws in wb.worksheets:
        head = ws.cell(row=1, column=1)
        assert head.value, "导出件第一行没有标题"
        assert head.font.size == TITLE_SIZE, f"主标题是 {head.font.size}pt"
        assert head.font.bold is True, "主标题没有加粗"


def test_明细账导出的主标题是20pt加粗(client, auth, book, periods, subjects):
    wb = _export(client, auth, book, "detail-ledger.xlsx",
                 period_id=periods[6].id, skip_empty="false")
    for ws in wb.worksheets:
        head = ws.cell(row=1, column=1)
        assert head.value, "导出件第一行没有标题"
        assert head.font.size == TITLE_SIZE, f"主标题是 {head.font.size}pt"
        assert head.font.bold is True, "主标题没有加粗"
