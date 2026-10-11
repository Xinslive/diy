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

from app.printing.excel import (balance_excel, general_ledger_excel,
                                opening_balance_excel, vouchers_excel)


def _voucher(no: str, amount: str, summary: str = "测试") -> dict:
    return {
        "voucher_date": "2026-01-05", "voucher_no": no,
        "entries": [
            {"summary": summary, "subject_code": "1001", "subject_name": "库存现金",
             "direction": "DEBIT", "amount": amount},
            {"summary": summary, "subject_code": "6001", "subject_name": "主营业务收入",
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
