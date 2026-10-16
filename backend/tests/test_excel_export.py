"""Excel 导出的三个缺陷（审计 P2-03）。

1. **空结果崩溃**：合计那段写在 for 循环**外面**却引用循环变量 v，
   rows 为空时 v 从未被赋值 → UnboundLocalError → 500。
   而「这个期间没有凭证」是完全正常的筛选结果。
2. **合计只取最后一张**：实测导出 2 张各 100 的凭证，合计写出来是 200
   而不是 300。凭证导出的合计错了会直接对不上账。
3. **摘要公式注入**：摘要以 ``=`` 开头时 openpyxl 判成公式
   （``data_type == 'f'``）。用户可控文本被写成可执行公式，
   别人在 Excel 里打开可能触发外部链接请求，显示的也不是原文。
   导出文件是要发给客户和审计方的，账上的字必须原样显示。
"""
from __future__ import annotations

import io
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app.printing.excel import (MAX_EXPORT_ROWS, ExportTooLarge, balance_excel,
                                detail_ledger_excel, general_ledger_excel,
                                opening_balance_excel, report_excel, vouchers_excel)


def _voucher(no: str, amount: str, summary: str = "测试") -> dict:
    return {
        "voucher_date": "2026-01-05", "voucher_no": no,
        "entries": [
            {"summary": summary, "subject_code": "1001", "subject_name": "库存现金",
             "direction": "DEBIT", "amount": amount},
            {"summary": summary, "subject_code": "5001", "subject_name": "主营业务收入",
             "direction": "CREDIT", "amount": amount},
        ],
        "total_debit": amount, "total_credit": amount,
    }


def _sheet(data: bytes):
    return load_workbook(io.BytesIO(data)).active


# ---------------------------------------------------------------- ① 空结果


def test_空结果能导出合法空表():
    """「这个期间没有凭证」是正常筛选结果，不该 500。"""
    data = vouchers_excel([])
    ws = _sheet(data)
    # 表头还在
    assert ws.cell(row=4, column=3).value == "摘要"
    assert ws.cell(row=4, column=5).value == "借方金额"


def test_空结果不产生合计行():
    ws = _sheet(vouchers_excel([]))
    values = [ws.cell(row=r, column=3).value for r in range(1, ws.max_row + 1)]
    assert "本页合计" not in values


# ---------------------------------------------------------------- ② 合计


def test_合计是全部凭证之和而不是最后一张():
    """审计实测：2 张各 100 → 期望 300，实际 200。"""
    ws = _sheet(vouchers_excel([_voucher("记-1", "100"), _voucher("记-2", "200")]))
    last = ws.max_row
    assert ws.cell(row=last, column=3).value == "本页合计"
    assert Decimal(str(ws.cell(row=last, column=5).value)) == Decimal("300")
    assert Decimal(str(ws.cell(row=last, column=6).value)) == Decimal("300")


def test_单张凭证的合计就是它自己():
    ws = _sheet(vouchers_excel([_voucher("记-1", "888")]))
    last = ws.max_row
    assert Decimal(str(ws.cell(row=last, column=5).value)) == Decimal("888")


def test_红字凭证参与合计():
    """红字是冲销，它也是账的一部分，合计必须算进去。"""
    rows = [_voucher("记-1", "100"), _voucher("记-2", "-30")]
    ws = _sheet(vouchers_excel(rows))
    last = ws.max_row
    assert Decimal(str(ws.cell(row=last, column=5).value)) == Decimal("70")


# ---------------------------------------------------------------- ③ 公式注入


@pytest.mark.parametrize("payload", ["=1+1", "+1", "-1+1", "@SUM(A1)", "=HYPERLINK(\"http://x\")"])
def test_摘要开头的公式符号不会变成可执行公式(payload):
    ws = _sheet(vouchers_excel([_voucher("记-1", "100", summary=payload)]))
    cell = ws.cell(row=5, column=3)
    assert cell.data_type != "f", f"{payload!r} 被写成了公式"
    # 内容仍是原文（前面可能多一个 Excel 的「强制文本」单引号）
    assert payload in str(cell.value)


def test_日期和凭证号也不受注入影响():
    rows = [_voucher("=1+1", "100")]
    rows[0]["voucher_date"] = "=1+1"
    ws = _sheet(vouchers_excel(rows))
    for row in range(5, ws.max_row + 1):
        for col in (1, 2):
            c = ws.cell(row=row, column=col)
            assert c.data_type != "f", f"第 {row} 行第 {col} 列被写成了公式：{c.value!r}"


def test_正常摘要不受影响():
    ws = _sheet(vouchers_excel([_voucher("记-1", "100", summary="收到租金")]))
    assert ws.cell(row=5, column=3).value == "收到租金"


def test_科目余额表导出也不注入():
    """科目名是用户填的，同样要防。"""
    payload = {"period": "2026-01", "check": {"period_balanced": True, "closing_balanced": True}, "rows": [{
        "code": "1001", "name": "=1+1", "opening_direction": "借",
        "opening": "0", "period_debit": "100", "period_credit": "0",
        "ytd_debit": "100", "ytd_credit": "0", "direction": "借", "closing": "100",
    }]}
    ws = _sheet(balance_excel(payload, company="甲", book_name="乙"))
    cells = [ws.cell(row=r, column=c)
             for r in range(1, ws.max_row + 1) for c in range(1, ws.max_column + 1)]
    assert all(c.data_type != "f" for c in cells), "科目余额表里有单元格被写成了公式"


def test_总账导出也不注入():
    payload = {"from_period": "2026-01", "to_period": "2026-01", "rows": [{
        "code": "1001", "name": "=1+1", "opening_direction": "借",
        "opening": "0", "period_debit": "100", "period_credit": "0",
        "ytd_debit": "100", "ytd_credit": "0", "closing": "100",
    }]}
    ws = _sheet(general_ledger_excel(payload, company="甲", book_name="乙"))
    cells = [ws.cell(row=r, column=c)
             for r in range(1, ws.max_row + 1) for c in range(1, ws.max_column + 1)]
    assert all(c.data_type != "f" for c in cells), "总账里有单元格被写成了公式"


def test_期初余额表导出也不注入():
    ws = _sheet(opening_balance_excel(
        [{"code": "1001", "name": "=1+1", "direction": "借", "amount": 100.0}],
        company="甲", book_name="乙", year=2026, month=1))
    cells = [ws.cell(row=r, column=c)
             for r in range(1, ws.max_row + 1) for c in range(1, ws.max_column + 1)]
    assert all(c.data_type != "f" for c in cells), "期初余额表里有单元格被写成了公式"


# ===================================================================== P1-11 标题格与行标签的注入绕过
#
# ``_text`` 本来就写了防护（openpyxl 遇到 =/+/-/@ 开头的字符串会标成公式），
# 但它只用在数据行上。**表头区的单元格全都绕过了它**：
#
#   * A2 的「单位名 · 账套名 · 期间」—— company 来自 book.company_name，
#     是建账者自己填的自由文本；
#   * A1 的报表名 —— 来自可 PATCH 的**全局**报表模板名；
#   * 报表行标签 —— 来自报表公式的 label；
#   * 期初余额表 A1 —— 同样是 company。
#
# 可利用性不是理论上的：company 由建账者填写、报表名可被 PATCH，而
# 导出文件是要发给客户和审计方的（这个文件自己的注释就这么写）。

#: Excel/CSV 公式注入的经典载荷。=HYPERLINK 会在别人打开文件时
#: 弹出一个可点击的外部链接；=cmd|'...'!A0 这类在老版本 Excel 上能直接执行。
INJECTIONS = [
    "=1+1",
    "+1",
    "-1+1",
    "@SUM(A1)",
    '=HYPERLINK("http://evil.example","点我")',
    "=cmd|'/c calc'!A0",
]


def _all_cells(ws):
    return [ws.cell(row=r, column=c)
            for r in range(1, ws.max_row + 1)
            for c in range(1, ws.max_column + 1)]


def _assert_no_formula(ws, what: str):
    bad = [f"{c.coordinate}={c.value!r}" for c in _all_cells(ws) if c.data_type == "f"]
    assert not bad, f"{what}里有单元格被写成了公式：{bad}"


def _assert_payload_preserved(ws, what: str, payload: str):
    """防住了注入还不够：原文必须还在（前导单引号是 Excel 的强制文本记法）。"""
    assert any(payload in str(c.value) for c in _all_cells(ws)), \
        f"{what}里找不到原文 {payload!r}"


@pytest.mark.parametrize("payload", INJECTIONS)
def test_科目余额表标题格里的单位名不注入(payload):
    ws = _sheet(balance_excel(
        {"period": "2026-01", "check": {"period_balanced": True, "closing_balanced": True},
         "rows": []}, company=payload, book_name="乙"))
    _assert_no_formula(ws, "科目余额表")
    _assert_payload_preserved(ws, "科目余额表", payload)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_总账标题格里的单位名不注入(payload):
    ws = _sheet(general_ledger_excel(
        {"from_period": "2026-01", "to_period": "2026-01", "rows": []},
        company=payload, book_name="乙"))
    _assert_no_formula(ws, "总账")
    _assert_payload_preserved(ws, "总账", payload)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_期初余额表标题格里的单位名不注入(payload):
    ws = _sheet(opening_balance_excel([], company=payload, book_name="乙",
                                       year=2026, month=1))
    _assert_no_formula(ws, "期初余额表")
    _assert_payload_preserved(ws, "期初余额表", payload)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_报表名不注入(payload):
    """报表名来自**可 PATCH 的全局报表模板**，是这批绕过点里最危险的一个。"""
    ws = _sheet(report_excel(
        {"period": "2026-01", "name": payload, "cols": [{"label": "项目", "width": 100}],
         "rows": []}, company="甲", book_name="乙"))
    _assert_no_formula(ws, "报表")
    _assert_payload_preserved(ws, "报表", payload)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_报表行标签不注入(payload):
    """行标签带缩进前缀（"　" * indent），不能因为拼了前缀就绕过 _text。"""
    ws = _sheet(report_excel(
        {"period": "2026-01", "name": "利润表",
         "cols": [{"label": "项目", "width": 100}],
         "rows": [{"indent": 2, "label": payload, "bold": False, "cells": []}]},
        company="甲", book_name="乙"))
    _assert_no_formula(ws, "报表")
    _assert_payload_preserved(ws, "报表", payload)


def test_凭证导出的标题和期间也不注入():
    ws = _sheet(vouchers_excel([_voucher("记-1", "100")],
                               title="=1+1", period="+1"))
    _assert_no_formula(ws, "记账凭证")


# ===================================================================== P2-16 工作表名非法字符


@pytest.mark.parametrize("name", ["2026[Q1]", "1月/2月对比", "利润表*草案",
                                  "报表:最终版", "a?b", "x\\y", "很长的名字" * 10])
def test_含非法字符的报表名不会500(name):
    """``ws.title = name`` 遇 []:*?/\\ 直接抛 ValueError → HTTP 500。

    而这些名字都是**正常业务名**：季度对比、期间对比、草稿/终版标记。
    报错等于「你起名起得不合法」。这里清洗成 Excel 认的名字。
    """
    ws = _sheet(report_excel(
        {"period": "2026-01", "name": name, "cols": [{"label": "项目", "width": 100}],
         "rows": []}, company="甲", book_name="乙"))
    assert ws.title                      # 一定拿到一个可用名字
    assert len(ws.title) <= 31           # Excel 上限
    assert not (set("[]:*?/\\") & set(ws.title))


def test_报表名全是非法字符时退回默认名():
    ws = _sheet(report_excel(
        {"period": "2026-01", "name": "[[[", "cols": [{"label": "项目", "width": 100}],
         "rows": []}, company="甲", book_name="乙"))
    assert ws.title == "报表"


def test_正常报表名原样保留():
    ws = _sheet(report_excel(
        {"period": "2026-01", "name": "利润表", "cols": [{"label": "项目", "width": 100}],
         "rows": []}, company="甲", book_name="乙"))
    assert ws.title == "利润表"


# ===================================================================== P3-6 导出行数上限


def test_导出行数超限给出明确错误():
    """明细账跨全科目跨全年，openpyxl 全内存构建，不卡上限能把后端吃光。

    刻意选**报错**而不是静默截断：交给审计方的账表少一段，比导不出来糟得多。
    """
    with pytest.raises(ExportTooLarge) as ei:
        detail_ledger_excel({"rows": [{}] * (MAX_EXPORT_ROWS + 1)}, book_name="乙")
    assert "超过" in str(ei.value)


def test_各导出接口都有行数上限():
    too_many = [{}] * (MAX_EXPORT_ROWS + 1)
    check = {"period_balanced": True, "closing_balanced": True}
    cases = [
        lambda: detail_ledger_excel({"rows": too_many}, book_name="乙"),
        lambda: balance_excel({"rows": too_many, "check": check, "period": "2026-01"},
                              company="甲", book_name="乙"),
        lambda: general_ledger_excel(
            {"rows": too_many, "from_period": "2026-01", "to_period": "2026-01"},
            company="甲", book_name="乙"),
        lambda: report_excel(
            {"period": "2026-01", "name": "报表", "cols": [{"label": "项目", "width": 100}],
             "rows": too_many}, company="甲", book_name="乙"),
        # 凭证这一路原先漏了：它的行数是**分录数**，一张凭证写几行就占几行，
        # 按凭证数卡等于把上限放大了一倍（每张 8 行时就是 8 倍）。
        # 调用方一次取 5000 张凭证，最坏情况 4 万行，而每格都要写 border。
        lambda: vouchers_excel([_voucher("记-1", 1)] * (MAX_EXPORT_ROWS + 1)),
    ]
    for fn in cases:
        with pytest.raises(ExportTooLarge):
            fn()


def test_刚好到上限不报错():
    """边界：等于上限应当放行（否则上限就是「少一行也不行」）。"""
    rows = [{"voucher_date": "2026-01-05", "kind": "ENTRY", "summary": "测试",
             "debit": "1.00", "credit": "", "direction": "借", "balance": "1.00",
             "subject_code": "1001", "subject_name": "库存现金", "subject_level": 1}
            for _ in range(MAX_EXPORT_ROWS)]
    ws = _sheet(detail_ledger_excel({"rows": rows, "from_period": "2026-01"},
                                    book_name="乙"))
    assert ws.max_row >= MAX_EXPORT_ROWS


# ===================================================================== P3-4 / P2-7 Decimal 导出


def test_合计用Decimal累加不漂移():
    """float 累加 10 × 0.1 = 0.9999999999999999，导出表要交给审计方逐格核对。"""
    payload = {"period": "2026-01",
               "check": {"period_balanced": True, "closing_balanced": True},
               "rows": [{
                   "code": f"100{i}", "name": f"科目{i}", "opening_direction": "借",
                   "opening": "0", "period_debit": "0.10", "period_credit": "0",
                   "ytd_debit": "0", "ytd_credit": "0",
                   "direction": "借", "closing": "0",
               } for i in range(10)]}
    ws = _sheet(balance_excel(payload, company="甲", book_name="乙"))
    total_row = [r for r in range(1, ws.max_row + 1)
                 if ws.cell(row=r, column=2).value == "合计"][0]
    assert Decimal(str(ws.cell(row=total_row, column=5).value)) == Decimal("1.00")


def test_导出金额不经过float中转():
    """Numeric(20,2) 的值直出，不先 float() 再写 —— 大额下 float 只有 15~17 位有效数字。"""
    big = "1234567890.12"
    ws = _sheet(balance_excel(
        {"period": "2026-01", "check": {"period_balanced": True, "closing_balanced": True},
         "rows": [{"code": "1001", "name": "现金", "opening_direction": "借",
                   "opening": big, "period_debit": big, "period_credit": "0",
                   "ytd_debit": "0", "ytd_credit": "0", "direction": "借",
                   "closing": big}]},
        company="甲", book_name="乙"))
    assert Decimal(str(ws.cell(row=5, column=4).value)) == Decimal(big)
