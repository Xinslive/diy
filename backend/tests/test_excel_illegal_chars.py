"""Excel 导出遇到 XML 非法字符时整份失败（生产事故）。

**症状**：生产导出「管理费用明细账」（全部子科目、2026-01～2026-12，
1934 行）直接提示「导出失败」。用户怀疑是数据量太大。

**真相**：跟数据量无关。1934 行远在 ``MAX_EXPORT_ROWS = 50_000`` 之下。
Excel 底层是 XML，lxml 遇到 XML 1.0 不允许的字符会直接抛：

    ValueError: All strings must be XML compatible:
    Unicode or ASCII, no NULL bytes or control characters

抛在**写文件**的环节 —— 不是那一格没写上，是整个 xlsx 一个字节都生成不出来，
接口 500，前端只显示「导出失败」。493 行和 50 万行撞上同一个坏字符，
症状一模一样。

**坏字符从哪来**：发票 PDF 的文字层。pypdfium2 的 ``get_text_range()``
会把 UTF-16 的 BOM 在某些字形映射里漏成 U+FFFE，混进摘要：

    *电线电缆*广州五羊ZC\\ufffeBV2.5平方线

这份字符被原样存进 ``entry.summary``，之后每一条含它的明细账/总账导出
都会整份失败。

这里锁住三件事：
1. **导出层兜底**（``_scrub_workbook``）—— 无论脏字符从哪个写入点漏进来，
   落盘前一律扫掉；六个导出入口全部覆盖。
2. **写入层清洗**（``_text``）—— 正常的写入点直接就是干净的。
3. **源头拦截**（``text_parser.scrub_control_chars``）—— 以后不再攒新的脏字符。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import load_workbook

from app.invoice.text_parser import scrub_control_chars
from app.printing.excel import (balance_excel, detail_ledger_excel,
                                general_ledger_excel, opening_balance_excel,
                                report_excel, scrub_xml_chars, vouchers_excel)

#: U+FFFE —— 生产上真实出现过的坏字符：UTF-16 BOM 漏成它。
ZERO_FFFD = "\ufffe"

#: 一批在 XML 1.0 里非法、但肉眼看不见的字符。
INVISIBLE = [
    "\x00",   # NUL
    "\x01",   # C0 控制字符
    "\x08",   # 退格
    "\x0b",   # 垂直制表
    "\x0c",   # 换页
    "\x1b",   # ESC
    "\x1f",   # 单元分隔符
    "\x7f",   # DEL
    "\x85",   # C1：NEL
    "\x9f",   # C1 末尾
    "\ufffe",   # U+FFFE
    "\uffff",   # U+FFFF
]


def _voucher(summary: str = "正常摘要", subject_name: str = "库存现金") -> dict:
    return {
        "voucher_date": "2026-01-05", "voucher_no": "记0001",
        "entries": [
            {"summary": summary, "subject_code": "5602", "subject_name": subject_name,
             "direction": "DEBIT", "amount": "100"},
            {"summary": summary, "subject_code": "1001", "subject_name": "库存现金",
             "direction": "CREDIT", "amount": "100"},
        ],
        "total_debit": "100", "total_credit": "100",
    }


def _ledger_payload(summary: str = "正常摘要", subject_name: str = "库存现金") -> dict:
    return {"from_period": "2026-01", "to_period": "2026-12",
            "subjects": [{"code": "5602", "name": subject_name,
                          "rows": [{"period": "2026-01", "month": "01", "day": "05",
                                    "voucher_no": "记0001", "summary": summary,
                                    "debit": "100", "credit": "0",
                                    "direction": "借", "balance": "100"}]}]}


def _all_text(data: bytes) -> str:
    """把 xlsx 里所有 sheet 的原始 XML 拉出来。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return "".join(zf.read(n).decode("utf-8", "replace")
                       for n in zf.namelist() if n.startswith("xl/worksheets/"))


def _export(kind: str, *, summary: str, subject_name: str, book_name: str) -> bytes:
    if kind == "记账凭证":
        return vouchers_excel([_voucher(summary=summary, subject_name=subject_name)],
                              title="记账凭证", period="2026-01")
    if kind == "科目余额表":
        return balance_excel({
            "period": "2026-01",
            "check": {"period_balanced": True, "closing_balanced": True},
            "rows": [{"code": "5602", "name": subject_name, "opening_direction": "借",
                      "opening": "0", "period_debit": "100", "period_credit": "0",
                      "ytd_debit": "100", "ytd_credit": "0", "direction": "借",
                      "closing": "100"}]}, book_name=book_name)
    if kind == "总账":
        return general_ledger_excel({
            "from_period": "2026-01", "to_period": "2026-12",
            "rows": [{"code": "5602", "name": subject_name, "opening_direction": "借",
                      "opening": "0", "period_debit": "100", "period_credit": "0",
                      "ytd_debit": "100", "ytd_credit": "0", "direction": "借",
                      "closing": "100"}]}, book_name=book_name)
    if kind == "明细账":
        return detail_ledger_excel(
            _ledger_payload(summary=summary, subject_name=subject_name),
            book_name=book_name)
    if kind == "报表":
        return report_excel({
            "period": "2026-01", "name": f"利润表{book_name}",
            "cols": [{"label": "项目", "width": 100}],
            "rows": [{"indent": 0, "label": summary, "bold": False, "cells": []}]},
            book_name=book_name)
    if kind == "期初余额表":
        return opening_balance_excel(
            [{"code": "5602", "name": subject_name, "direction": "借", "amount": 100.0}],
            book_name=book_name, year=2026, month=1)
    raise AssertionError(f"没这个导出：{kind}")


# ===================================================================== 清洗函数本身


def test_洗掉U_FFFE():
    assert scrub_xml_chars(f"电线{ZERO_FFFD}BV2.5") == "电线BV2.5"


def test_洗掉所有不可见非法字符():
    for ch in INVISIBLE:
        assert scrub_xml_chars(f"a{ch}b") == "ab", f"{ch!r} 没被洗掉"


def test_换行制表换成空格而不是直接删():
    """TAB/LF/CR 在 XML 1.0 里是合法的，但会让单元格版面被撑开。"""
    assert scrub_xml_chars("a\tb\nc\rd") == "a b c d"


def test_正常中文一个不动():
    text = "管理费用 / 办公费 *油料*粮油米 100 元"
    assert scrub_xml_chars(text) == text


def test_解析层的清洗保留换行制表():
    """解析器按行切分要靠 TAB/LF/CR 定位，解析层不能动它们。"""
    assert scrub_control_chars("a\ufffeb\tc\nd") == "ab\tc\nd"


def test_解析层也洗掉全部非法字符():
    for ch in INVISIBLE:
        assert scrub_control_chars(f"a{ch}b") == "ab"


# ===================================================================== 导出层兜底


@pytest.mark.parametrize("导出", ["记账凭证", "科目余额表", "总账", "明细账", "报表", "期初余额表"])
def test_脏字符不会让整份导出失败(导出):
    """六个导出入口，摘要/科目名带 U+FFFE 时都必须出得来文件。"""
    data = _export(导出, summary=f"*油料*粮油米{ZERO_FFFD}粮油米",
                   subject_name=f"管理费用{ZERO_FFFD}", book_name=f"启明物业{ZERO_FFFD}")
    assert data, f"{导出} 导出为空"
    assert load_workbook(io.BytesIO(data)) is not None


@pytest.mark.parametrize("导出", ["记账凭证", "科目余额表", "总账", "明细账", "报表", "期初余额表"])
def test_导出的文件里不再有U_FFFE(导出):
    data = _export(导出, summary=f"摘要{ZERO_FFFD}", subject_name=f"科目{ZERO_FFFD}",
                   book_name=f"账套{ZERO_FFFD}")
    assert ZERO_FFFD not in _all_text(data), f"{导出} 的文件里还留着 U+FFFE"


@pytest.mark.parametrize("导出", ["记账凭证", "科目余额表", "总账", "明细账", "报表", "期初余额表"])
def test_所有不可见非法字符都被洗掉(导出):
    data = _export(导出, summary="摘要" + "".join(INVISIBLE),
                   subject_name="科目" + "".join(INVISIBLE),
                   book_name="账套" + "".join(INVISIBLE))
    xml = _all_text(data)
    for ch in INVISIBLE:
        assert ch not in xml, f"{导出} 的文件里还留着 {ch!r}"


def test_洗过之后内容没有丢():
    """只该洗字符，不该把整段摘要洗没了。"""
    data = vouchers_excel([_voucher(summary=f"*油料*粮油米{ZERO_FFFD}粮油米")])
    ws = load_workbook(io.BytesIO(data)).active
    assert ws.cell(row=5, column=3).value == "*油料*粮油米粮油米"


def test_金额和科目编码原样保留():
    data = vouchers_excel([_voucher(summary=f"摘要{ZERO_FFFD}", subject_name=f"管理费用{ZERO_FFFD}")])
    ws = load_workbook(io.BytesIO(data)).active
    assert ws.cell(row=5, column=4).value == "5602 管理费用"
    assert ws.cell(row=5, column=5).value == 100


def test_原始写入绕过_text也会被落盘前的清扫兜住():
    """**这才是真正的兜底**。

    ``_text`` 只覆盖「经过它的写入点」。将来有人写
    ``ws.cell(..., value=raw)`` 忘了套 ``_text``，脏字符照样会进 XML。
    落盘前统一扫一遍才是可靠的 —— 症状是「导出失败」，现场一行都查不出
    是哪一格坏。
    """
    from openpyxl import Workbook

    from app.printing.excel import _to_bytes

    wb = Workbook()
    ws = wb.active
    ws["A1"] = f"绕过_text的脏值{ZERO_FFFD}"
    data = _to_bytes(wb)
    assert ZERO_FFFD not in _all_text(data)


def test_大数据量导出不受影响():
    """用户怀疑「数据过大导致导出失败」—— 这条用例把两件事分开。

    几百行（生产那个数量级）+ 一个坏字符 = 失败；
    几百行 + 干净字符 = 成功。行数从来不是原因。
    """
    rows = [_voucher(summary=f"第{i}行摘要{ZERO_FFFD}") for i in range(300)]
    data = vouchers_excel(rows)
    assert load_workbook(io.BytesIO(data)) is not None
    assert ZERO_FFFD not in _all_text(data)
