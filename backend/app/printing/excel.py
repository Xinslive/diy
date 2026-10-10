"""Excel 导出：记账凭证 / 总账 / 科目余额表 / 报表。

浏览器打印走 HTML+CSS（前端负责），Excel 导出覆盖 A5 记账凭证、
连续打印纸这类需要二次加工的场景。
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

THIN = Side(style="thin", color="000000")
MEDIUM = Side(style="medium", color="000000")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(Decimal(str(v)))


def _money_style(ws, row: int, cols: range):
    for c in cols:
        ws.cell(row=row, column=c).number_format = "#,##0.00"


# --------------------------------------------------------------------- 记账凭证


def vouchers_excel(rows: list[dict], *, title: str = "记账凭证", period: str = "") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "记账凭证"

    ws["A1"] = title
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=5)
    ws["A2"] = f"会计期间：{period}"
    ws["A2"].alignment = CENTER

    headers = ["日期", "凭证字号", "摘要", "会计科目", "借方金额", "贷方金额"]
    widths = [12, 12, 26, 30, 14, 14]
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]

    r = 4
    ws.cell(row=r, column=1, value="日期").font = Font(bold=True)
    ws.cell(row=r, column=2, value="凭证字号").font = Font(bold=True)
    ws.cell(row=r, column=3, value="摘要").font = Font(bold=True)
    ws.cell(row=r, column=4, value="会计科目").font = Font(bold=True)
    ws.cell(row=r, column=5, value="借方金额").font = Font(bold=True)
    ws.cell(row=r, column=6, value="贷方金额").font = Font(bold=True)
    for c in range(1, 7):
        ws.cell(row=r, column=c).alignment = CENTER
        ws.cell(row=r, column=c).border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)

    for v in rows:
        first = r + 1
        for i, e in enumerate(v["entries"]):
            row = first + i
            ws.cell(row=row, column=1, value=v["voucher_date"] if i == 0 else None).alignment = CENTER
            ws.cell(row=row, column=2, value=v["voucher_no"] if i == 0 else None).alignment = CENTER
            ws.cell(row=row, column=3, value=e["summary"]).alignment = LEFT
            subj = f'{e["subject_code"]} {e["subject_name"]}'
            if e["direction"] == "DEBIT":
                subj = subj
            ws.cell(row=row, column=4, value=subj).alignment = LEFT
            ws.cell(row=row, column=5,
                    value=_num(e["amount"]) if e["direction"] == "DEBIT" else None).alignment = RIGHT
            ws.cell(row=row, column=6,
                    value=_num(e["amount"]) if e["direction"] == "CREDIT" else None).alignment = RIGHT
        last = first + len(v["entries"]) - 1
        for row in range(first, last + 1):
            for c in range(1, 7):
                ws.cell(row=row, column=c).border = BOX
        _money_style(ws, last, range(5, 7))
        r = last

    ws.cell(row=r + 2, column=3, value="本页合计").font = Font(bold=True)
    ws.cell(row=r + 2, column=5, value=_num(v["total_debit"])).font = Font(bold=True)
    ws.cell(row=r + 2, column=6, value=_num(v["total_credit"])).font = Font(bold=True)
    _money_style(ws, r + 2, range(5, 7))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- 科目余额表


def balance_excel(payload: dict, *, company: str, book_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "科目余额表"

    ws["A1"] = "科目余额表"
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=10)
    ws["A2"] = f"{company} · {book_name} · 期间 {payload['period']}"
    ws["A2"].alignment = CENTER

    headers = ["科目编码", "科目名称", "方向", "期初", "本期借方", "本期贷方",
               "本年借方", "本年贷方", "方向", "期末"]
    widths = [12, 24, 8, 14, 14, 14, 14, 14, 8, 14]
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]

    r = 4
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = Font(bold=True)
        c.alignment = CENTER
        c.border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)

    totals = [0.0] * 6
    for row in payload["rows"]:
        r += 1
        values = [row["code"], row["name"], row["opening_direction"], _num(row["opening"]),
                  _num(row["period_debit"]), _num(row["period_credit"]),
                  _num(row["ytd_debit"]), _num(row["ytd_credit"]),
                  row["direction"], _num(row["closing"])]
        for i, v in enumerate(values, start=1):
            c = ws.cell(row=r, column=i, value=v)
            c.border = BOX
            c.alignment = LEFT if i in (1, 2) else (CENTER if i in (3, 9) else RIGHT)
        for idx, key in enumerate(["opening", "period_debit", "period_credit",
                                   "ytd_debit", "ytd_credit", "closing"]):
            totals[idx] += float(row[key] or 0)

    r += 1
    ws.cell(row=r, column=2, value="合计").font = Font(bold=True)
    for i, t in enumerate(totals, start=4):
        ws.cell(row=r, column=i, value=round(t, 2)).font = Font(bold=True)
    for c in range(1, 11):
        ws.cell(row=r, column=c).border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)
    _money_style(ws, r, range(4, 11))

    check = payload["check"]
    r += 2
    ws.cell(row=r, column=2, value="试算平衡：").font = Font(bold=True)
    ws.cell(row=r, column=4,
            value="本期发生额平衡 ✓" if check["period_balanced"] else "本期发生额不平衡 ✗")
    ws.cell(row=r + 1, column=4,
            value="期末余额平衡 ✓" if check["closing_balanced"] else "期末余额不平衡 ✗")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- 总账


def general_ledger_excel(payload: dict, *, company: str, book_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "总账"

    ws["A1"] = "总账"
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=9)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=9)
    ws["A2"] = f"{company} · {book_name} · {payload['from_period']} 至 {payload['to_period']}"
    ws["A2"].alignment = CENTER

    headers = ["科目编码", "科目名称", "期初方向", "期初余额", "本期借方", "本期贷方",
               "本年借方", "本年贷方", "期末余额"]
    widths = [12, 26, 10, 14, 14, 14, 14, 14, 16]
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]

    r = 4
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = Font(bold=True)
        c.alignment = CENTER
        c.border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)

    for row in payload["rows"]:
        r += 1
        values = [row["code"], row["name"], row["opening_direction"], _num(row["opening"]),
                  _num(row["period_debit"]), _num(row["period_credit"]),
                  _num(row["ytd_debit"]), _num(row["ytd_credit"]), _num(row["closing"])]
        for i, v in enumerate(values, start=1):
            c = ws.cell(row=r, column=i, value=v)
            c.border = BOX
            c.alignment = LEFT if i in (1, 2) else (CENTER if i == 3 else RIGHT)
        _money_style(ws, r, [4, 5, 6, 7, 8, 9])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- 报表


def report_excel(payload: dict, *, company: str, book_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = payload.get("name", "报表")[:30]

    ws["A1"] = payload.get("name", "报表")
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    last_col = max(len(payload["cols"]), 1)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    ws["A2"] = f"{company} · {book_name} · 期间 {payload['period']}"
    ws["A2"].alignment = CENTER

    r = 4
    for idx, col in enumerate(payload["cols"], start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col.get("width", 120) / 7
        c = ws.cell(row=r, column=idx, value=col["label"])
        c.font = Font(bold=True)
        c.alignment = CENTER
        c.border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)

    for row in payload["rows"]:
        r += 1
        c = ws.cell(row=r, column=1, value="　" * row["indent"] + row["label"])
        c.alignment = LEFT
        c.font = Font(bold=row["bold"])
        c.border = BOX
        for idx, cell in enumerate(row["cells"], start=2):
            c = ws.cell(row=r, column=idx, value=_num(cell["value"]))
            c.alignment = RIGHT
            c.border = BOX
            c.font = Font(bold=row["bold"])
            c.number_format = "#,##0.00"

    if payload.get("errors"):
        r += 2
        ws.cell(row=r, column=1, value="公式错误：").font = Font(bold=True, color="CC0000")
        for i, e in enumerate(payload["errors"][:20]):
            ws.cell(row=r + i + 1, column=1, value=e).font = Font(color="CC0000")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
