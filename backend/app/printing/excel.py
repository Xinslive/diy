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

ZERO = Decimal("0")

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


def _d(v) -> Decimal:
    """宽松取数：None / 空串 / 已经是 Decimal 都吃得下。"""
    if v in (None, ""):
        return ZERO
    return Decimal(str(v))


def _text(value) -> str:
    """把用户可控文本写成**纯字符串**，不让 Excel 当公式执行。

    openpyxl 看到以 ``=``/``+``/``-``/``@`` 开头的字符串会把它标成公式
    （``cell.data_type == 'f'``）。摘要、备注、科目名都是用户填的，
    摘要写个 ``=1+1`` 导出后在 Excel 里就是一格可执行公式 ——
    别人打开可能触发外部链接请求，显示出来的也不是原文。

    这不是洁癖：导出文件是要发给客户和审计方的，账上的字必须原样显示。
    加一个前导单引号是 Excel 自己的「强制文本」记法，读起来仍是原文。
    """
    if value is None:
        return ""
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _money_style(ws, row: int, cols: range):
    for c in cols:
        ws.cell(row=row, column=c).number_format = "#,##0.00"


def _to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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

    # 本页合计要**累计本批所有凭证**。原来写在 for 循环外面、直接引用
    # 循环变量 v，取到的是**最后一张**的合计 —— 导出 2 张各 100 的凭证，
    # 合计写出来是 200 而不是 300。凭证导出的合计错了会直接对不上账。
    sum_dr = ZERO
    sum_cr = ZERO

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
        sum_dr += _d(v.get("total_debit"))
        sum_cr += _d(v.get("total_credit"))
        first = r + 1
        for i, e in enumerate(v["entries"]):
            row = first + i
            ws.cell(row=row, column=1,
                    value=_text(v["voucher_date"]) if i == 0 else None).alignment = CENTER
            ws.cell(row=row, column=2,
                    value=_text(v["voucher_no"]) if i == 0 else None).alignment = CENTER
            ws.cell(row=row, column=3, value=_text(e["summary"])).alignment = LEFT
            subj = _text(f'{e["subject_code"]} {e["subject_name"]}')
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

    # 空结果也要能导出：原来这一段在 for 循环**外面**却引用了循环变量 v，
    # rows 为空时 v 从未被赋值 → UnboundLocalError → 500。
    # 「这个期间没有凭证」是完全正常的筛选结果，不该报错。
    if rows:
        ws.cell(row=r + 2, column=3, value="本页合计").font = Font(bold=True)
        ws.cell(row=r + 2, column=5, value=_num(sum_dr)).font = Font(bold=True)
        ws.cell(row=r + 2, column=6, value=_num(sum_cr)).font = Font(bold=True)
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
        values = [_text(row["code"]), _text(row["name"]), _text(row["opening_direction"]),
                  _num(row["opening"]),
                  _num(row["period_debit"]), _num(row["period_credit"]),
                  _num(row["ytd_debit"]), _num(row["ytd_credit"]),
                  _text(row["direction"]), _num(row["closing"])]
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
        values = [_text(row["code"]), _text(row["name"]), _text(row["opening_direction"]),
                  _num(row["opening"]),
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


def detail_ledger_excel(payload: dict, *, book_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "明细账"
    ws.sheet_view.showGridLines = False

    subject = payload.get("subject", {})
    subject_range = payload.get("subject_range")
    comprehensive = bool(payload.get("comprehensive"))
    headers = ["凭证号"]
    widths = [13]
    if comprehensive:
        headers.extend(["科目编码", "科目名称"])
        widths.extend([13, 26])
    headers.extend(["摘要", "借方", "贷方", "方向", "余额"])
    widths.extend([42, 15, 15, 9, 17])
    last_col = len(headers) + 2

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws.cell(row=1, column=1, value="明细账").font = Font(name="宋体", size=16, bold=True)
    ws.cell(row=1, column=1).alignment = CENTER
    ws.row_dimensions[1].height = 30

    subtitle = f"{book_name} · {subject.get('code', '')} {subject.get('name', '')}"
    if subject_range:
        subtitle += f" · {subject_range['from']['code']} 至 {subject_range['to']['code']}"
    subtitle += f" · {payload.get('from_period', '')} 至 {payload.get('to_period', '')}"
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    ws.cell(row=2, column=1, value=_text(subtitle)).alignment = CENTER

    for col, width in enumerate([8, 8, *widths], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width

    year = str(payload.get("from_period", ""))[:4]
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=2)
    ws.cell(row=4, column=1, value=f"{year}年" if year else "日期")
    ws.cell(row=5, column=1, value="月")
    ws.cell(row=5, column=2, value="日")
    for col, header in enumerate(headers, start=3):
        ws.merge_cells(start_row=4, start_column=col, end_row=5, end_column=col)
        ws.cell(row=4, column=col, value=header)

    header_border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)
    for row_num in (4, 5):
        for col in range(1, last_col + 1):
            cell = ws.cell(row=row_num, column=col)
            cell.font = Font(name="宋体", size=10, bold=True)
            cell.alignment = CENTER
            cell.border = header_border
    ws.row_dimensions[4].height = 22
    ws.row_dimensions[5].height = 21

    money_cols = [last_col - 3, last_col - 2, last_col]
    row_num = 6
    subject_level = int(subject.get("level") or 1)
    for row in payload.get("rows", []):
        voucher_date = str(row.get("voucher_date") or "")
        period = str(row.get("period") or "")
        date_value = voucher_date or period
        month = date_value[5:7] if len(date_value) >= 7 else ""
        day = voucher_date[8:10] if len(voucher_date) >= 10 else ""
        kind = row.get("kind")
        values = [month, day, _text(row.get("voucher_no") if kind == "ENTRY" else "")]
        if comprehensive:
            depth = max(0, int(row.get("subject_level") or 1) - subject_level)
            name = f"{'　' * depth}{row.get('subject_name', '')}"
            values.extend([_text(row.get("subject_code", "")), _text(name)])
        summary = "累　　计" if kind == "YTD_TOTAL" else row.get("summary", "")
        values.extend([
            _text(summary), _num(row.get("debit")), _num(row.get("credit")),
            _text(row.get("direction")), _num(row.get("balance")),
        ])
        is_total = kind in ("OPENING", "MONTH_TOTAL", "YTD_TOTAL")
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_num, column=col, value=value)
            cell.border = BOX
            cell.font = Font(name="宋体", size=10, bold=is_total)
            cell.alignment = CENTER if col in (1, 2, last_col - 1) else (
                RIGHT if col in money_cols else LEFT
            )
        _money_style(ws, row_num, money_cols)
        ws.row_dimensions[row_num].height = 21
        row_num += 1

    ws.freeze_panes = "A6"
    ws.print_title_rows = "1:5"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return _to_bytes(wb)


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
        c = ws.cell(row=r, column=idx, value=_text(col["label"]))
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
            ws.cell(row=r + i + 1, column=1, value=_text(e)).font = Font(color="CC0000")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def opening_balance_excel(rows: list[dict], *, company: str, book_name: str, year: int,
                          month: int) -> bytes:
    """期初余额表（导入模板 / 导出当前录入值 共用一份格式）。

    列刻意做成「财务系统间最好抄」的样子：科目编码、科目名称、方向、期初余额。
    方向列是必填的 —— 同一个正数在借方科目和贷方科目上存进去是不一样的，
    光看金额抄不出方向，会静默记反。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "期初余额"

    ws.merge_cells("A1:D1")
    ws["A1"] = f"{company}"
    ws["A1"].font = Font(size=13, bold=True)
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:D2")
    ws["A2"] = f"{book_name}　期初余额表　{year}年{month}月"
    ws["A2"].font = Font(size=11, bold=True)
    ws["A2"].alignment = Alignment(horizontal="center")

    head = ["科目编码", "科目名称", "方向", "期初余额"]
    for i, h in enumerate(head, start=1):
        c = ws.cell(row=4, column=i, value=h)
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border = Border(bottom=Side(style="thin"))

    r = 5
    for row in rows:
        ws.cell(row=r, column=1, value=_text(row["code"]))
        ws.cell(row=r, column=2, value=_text(row["name"]))
        ws.cell(row=r, column=3, value=_text(row["direction"]))
        ws.cell(row=r, column=4, value=_num(row["amount"]))
        r += 1

    if r > 5:
        _money_style(ws, 4, range(4, 5))     # 列格式（作用于整个 D 列）
        for rr in range(5, r):
            ws.cell(row=rr, column=4).number_format = "#,##0.00"
    for col, w in zip("ABCD", (14, 32, 10, 18)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"
    return _to_bytes(wb)
