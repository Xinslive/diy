"""Excel 导出：记账凭证 / 总账 / 科目余额表 / 报表。

浏览器打印走 HTML+CSS（前端负责），Excel 导出覆盖 A5 记账凭证、
连续打印纸这类需要二次加工的场景。
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ZERO = Decimal("0")
CENT = Decimal("0.01")

#: 单次导出的最大数据行数。
#:
#: 明细账可以「跨全科目跨全年」，行数上界是**科目数 × 期间数 × 明细行数**，
#: 实测能到十几万行。openpyxl 是全内存构建（``wb.save`` 把整张表序列化成
#: 一个 zip 再进 BytesIO），没有上限时一个「导出全年明细账」能把进程内存吃光，
#: 拖垮的不是一个请求而是整个后端。
#:
#: 这里选**明确报错**而不是静默截断：导出的账表少一段，交给审计方就是错的，
#: 而报错只是让用户缩小查询范围。宁可失败得明确，也不能给一份缺行的账。
MAX_EXPORT_ROWS = 50_000

#: Excel 工作表名的非法字符（openpyxl 直接抛 ValueError）。
#: 见 ``_safe_sheet_title``。
_ILLEGAL_TITLE_CHARS = set(r"[]:*?/\\")


class ExportTooLarge(ValueError):
    """导出行数超限。由 API 层翻译成 413 并带上可操作的提示。"""


def _check_size(kind: str, rows: int) -> None:
    if rows > MAX_EXPORT_ROWS:
        raise ExportTooLarge(
            f"{kind}共 {rows:,} 行，超过单次导出上限 {MAX_EXPORT_ROWS:,} 行。"
            "请缩小查询范围（按科目或按期间分段导出）后重试。")


def _safe_sheet_title(name: str, fallback: str) -> str:
    """把模板名变成一个 Excel 认的工作表名。

    openpyxl 对 ``[]:*?/\\`` 直接抛 ValueError → HTTP 500。触发它很容易：
    报表模板名是用户自己填的，「2026[Q1]」「1月/2月对比」这类名字完全正常，
    却会让整个导出 500。

    做法是**剔除非法字符 + 截断到 31 字（Excel 上限）**，而不是报错：
    这些名字本身都是合法业务名，报错等于「你起名起得不合法」。
    剔完为空（名字全是非法字符）才退回默认名。sheet 名只在标签页上显示，
    不参与账务计算，清洗掉这几个字符不影响导出内容的正确性。
    """
    cleaned = "".join(ch for ch in str(name or "") if ch not in _ILLEGAL_TITLE_CHARS)
    cleaned = cleaned.strip()[:31]
    return cleaned or fallback

THIN = Side(style="thin", color="000000")
MEDIUM = Side(style="medium", color="000000")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")


def _num(v) -> Optional[float]:
    """金额列写进单元格的形态。

    **保持 Decimal，不收窄成 float**（P2-7 / P3-4）。

    金额在库里是 ``Numeric(20,2)``、在计算链路里是 Decimal，到导出这一步
    退回 float 就等于在最末端又把精度丢了：大额（千万级以上）float 只有
    15~17 位有效数字，逐格累加还会漂。openpyxl 原生支持 Decimal 写单元格
    （``data_type == 'n'``，写出的是十进制字面量），所以这里不需要理由退回 float。

    另外注意合计曾经用 ``float`` 累加：10 × 0.1 会得到 0.9999999999999999，
    再 ``round(2)`` 仍是 1.0 看着没事，但 0.1+0.2 这类组合在几十万行上会明显偏移，
    而导出表是要交给审计方逐格核对的。改成 Decimal 累加后不存在这个问题。
    """
    if v is None or v == "":
        return None
    return Decimal(str(v))


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

    # 口径是**分录数**不是凭证数：下面按 entry 逐行展开（`:172` 那段循环），
    # 一张凭证有几条分录就占几行。拿凭证数去卡上限，等于把上限放大了一倍
    # （每张凭证平均 8 行的情况下就是 8 倍）。
    #
    # 调用方 `/print/vouchers.xlsx` 一次取 5000 张凭证（`api/printing.py`），
    # 也就是最坏情况 4 万行；而下面每一格都逐个 `ws.cell(...)` 再赋 `.border`，
    # openpyxl 又是全内存构建 —— 没有上限时，一个请求就能把进程内存吃光。
    # 其余四个导出函数都卡了 MAX_EXPORT_ROWS，只有这里没有，是漏了不是例外。
    _check_size("记账凭证", sum(len(v.get("entries") or []) for v in rows))

    ws["A1"] = _text(title)
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=5)
    ws["A2"] = _text(f"会计期间：{period}")
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

#: 合计行用到的八个「按方向归过边」的数。键名与 ``queries.balance_sheet_of_accounts``
#: 的 ``check`` 一一对应，别再另立一套叫法。
_TOTAL_KEYS = ("opening_debit", "opening_credit", "period_debit", "period_credit",
               "ytd_debit", "ytd_credit", "closing_debit", "closing_credit")


def _balance_totals(rows: list[dict], check: dict) -> dict[str, Decimal]:
    """科目余额表合计行 = **试算合计**，不是纸上那几行的算术和。

    以前这里是 ``for row in rows: totals[idx] += _d(row.get(key))``，两个错叠在
    一起，导出的余额表正好是交给客户和审计的纸面数：

    1. **方向被吃掉了。** 行里的 ``opening`` / ``closing`` 来自
       ``display_side()``，是**绝对值** —— 贷方余额同样存正数（它靠旁边的
       「方向」列区分）。借方余额合计 1000 + 贷方余额合计 1000 直接相加，
       合计「期初」写成 2000，而下面两行试算还打着「期末余额平衡 ✓」：
       同一张纸上两处结论互相打脸，而 2000 不等于任何一边的合计。
       归边口径必须与 ``app/ledger/queries.py::balance_sheet_of_accounts``
       一致 —— 那边按 ``o_signed`` / ``c_signed`` 的符号拆借贷、只累计叶子，
       行里的「方向」列就是那次拆分的结果，所以这里按方向列归边即同一个口径。
    2. **重复计数。** 行是**过滤之后**的（``filter_balance_rows``：仅一级 /
       仅明细 / 全部）。默认「全部」时父子同在一张表上，父级行本身就是下级的
       上卷，逐行相加等于把同一笔钱数了两遍。``filter_balance_rows`` 的
       docstring 明说它**不动 check** —— 那条 check 是叶子级算的，与档位无关，
       屏幕上的加总也就与档位无关。合计必须跟 check 同源，否则「仅一级」与
       「全部」打出来的合计不一样。

    **有 check 就用 check。** 它是叶子级、按符号拆好边的四个数，是这套系统里
    唯一一份试算口径的实现；本函数退化成「同样的算法」只为了在没有 check 的
    旧调用方（以及直接调本函数的测试）下仍然给出正确的数。
    期初的拆边 check 没给（那边只落了 ``opening_balanced`` 一个布尔），
    只能从叶子行算 —— 这是本次不扩到 ledger 层的唯一遗留缺口。
    """
    # 叶子优先。``is_leaf`` 缺失（老调用方 / 直接构 payload）按叶子算，
    # 宁可与 check 的口径同向，也不要把父子算两遍。
    leaves = [row for row in rows if row.get("is_leaf", True)]
    if not leaves:
        # 「仅一级」档位下 payload 里一行叶子都没有。一级行是下级的上卷，
        # 对发生额（借贷方、本年累计）来说它就是一次不重不漏的划分，按它算
        # 与按叶子算同值；余额绝对值只在同一父级内部正负相抵时才会偏低，
        # 那种行在导出纸上另有「方向」列可核对。
        leaves = list(rows)

    totals = dict.fromkeys(_TOTAL_KEYS, ZERO)
    for row in leaves:
        # Decimal 累加，不退回 float（见 _num 的说明）。逐格 round 也不能替代：
        # round 是「四舍五入到分」，累加是「先加后舍」，两者在长表上不等价。
        if row.get("opening_direction") == "借":
            totals["opening_debit"] += _d(row.get("opening"))
        else:
            totals["opening_credit"] += _d(row.get("opening"))
        totals["period_debit"] += _d(row.get("period_debit"))
        totals["period_credit"] += _d(row.get("period_credit"))
        totals["ytd_debit"] += _d(row.get("ytd_debit"))
        totals["ytd_credit"] += _d(row.get("ytd_credit"))
        if row.get("direction") == "借":
            totals["closing_debit"] += _d(row.get("closing"))
        else:
            totals["closing_credit"] += _d(row.get("closing"))

    for key, check_key in (("period_debit", "total_debit"), ("period_credit", "total_credit"),
                           ("closing_debit", "total_closing_debit"),
                           ("closing_credit", "total_closing_credit")):
        if check.get(check_key) is not None:
            totals[key] = _d(check[check_key])
    return totals


def _verdict(check: dict, key: str, label: str, debit: Decimal, credit: Decimal) -> str:
    """试算行：「平衡 ✓ / 不平衡 ✗」+ 两边的数。

    只打 ✓ 不打数，等于让读纸面的人自己回头把几百行加一遍 —— 而这份文件
    的用途正是**免去**加法（交给审计方逐格核对）。不平衡时两边的差额一眼可见。
    """
    def money(v: Decimal) -> str:
        return f"{v.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"

    mark = "平衡 ✓" if check.get(key) else "不平衡 ✗"
    return f"{label}{mark}（借 {money(debit)} / 贷 {money(credit)}）"


def balance_excel(payload: dict, *, company: str, book_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "科目余额表"
    _check_size("科目余额表", len(payload.get("rows") or []))

    ws["A1"] = "科目余额表"
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=10)
    # 单位名（book.company_name）是建账时填的自由文本，与摘要同一类输入。
    # 以前这四处标题格绕过了 _text：openpyxl 见到以 = 开头就标成公式，
    # 导出表里就多了一格可执行公式，而这份文件是要发给客户和审计方的。
    ws["A2"] = _text(f"{company} · {book_name} · 期间 {payload['period']}")
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
        # 合计**不在这个循环里累加**，见 _balance_totals：这里逐格加出来的是
        # 「纸上那几行的算术和」，既不是试算合计，档位一变还会跟着变。

    # 合计 = 试算合计（叶子级、按方向分借贷），见 _balance_totals。
    totals = _balance_totals(payload["rows"], payload.get("check") or {})

    r += 1
    ws.cell(row=r, column=2, value="合计").font = Font(bold=True)
    # 单栏的「期初 / 期末」在合计行只放得下一边，取**借方合计**。试算平衡成立
    # 时借方合计 == 贷方合计，两边同值，所以这一格就是试算合计本身；真不平衡
    # （不该发生，但账确实会错）时差额由下面两行试算行明写出来，不会藏起来。
    #
    # 第 9 列是「方向」，合计行没有方向可言 —— 以前把期末合计写在了那一列，
    # 于是一格方向列里躺着一个金额，而真正的「期末」列空着。
    for col, key in ((4, "opening_debit"), (5, "period_debit"), (6, "period_credit"),
                     (7, "ytd_debit"), (8, "ytd_credit"), (10, "closing_debit")):
        c = ws.cell(row=r, column=col, value=totals[key].quantize(CENT, rounding=ROUND_HALF_UP))
        c.font = Font(bold=True)
    for c in range(1, 11):
        ws.cell(row=r, column=c).border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)
    _money_style(ws, r, range(4, 11))

    check = payload["check"]
    r += 2
    ws.cell(row=r, column=2, value="试算平衡：").font = Font(bold=True)
    # 试算行把**两边**的数一起写出来，不只是 ✓/✗：单栏版式在合计行只留了
    # 借方合计，贷方合计在这里现身，收到纸面上的人不必自己回头加一遍。
    ws.cell(row=r, column=4, value=_verdict(
        check, "period_balanced", "本期发生额", totals["period_debit"], totals["period_credit"]))
    ws.cell(row=r + 1, column=4, value=_verdict(
        check, "closing_balanced", "期末余额", totals["closing_debit"], totals["closing_credit"]))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- 总账


def general_ledger_excel(payload: dict, *, company: str, book_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "总账"
    _check_size("总账", len(payload.get("rows") or []))

    ws["A1"] = "总账"
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=9)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=9)
    ws["A2"] = _text(f"{company} · {book_name} · {payload['from_period']} 至 {payload['to_period']}")
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

    # 明细账是「跨全科目跨全年」的那一张，行数上界最高，先卡上限（见 MAX_EXPORT_ROWS）。
    _check_size("明细账", len(payload.get("rows") or []))

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
    name = payload.get("name", "报表")
    # 报表模板名是用户可 PATCH 的自由文本，且是**全局**报表模板 ——
    # 「2026[Q1]」这类名字完全正常，却会让 openpyxl 抛 ValueError 变成 500。
    # 清洗见 _safe_sheet_title。
    ws.title = _safe_sheet_title(name, "报表")

    rows = payload.get("rows") or []
    cols = payload.get("cols") or []
    _check_size("报表", len(rows))

    ws["A1"] = _text(name)
    ws["A1"].font = Font(name="宋体", size=16, bold=True)
    # 标题合并宽度 = **实际数据列数**。原来是 max(len(cols), 1)，那个 +1 是当初
    # 「可能一列都没有」时的兜底，代价是列数为 8 的双栏表会把标题格多并一格，
    # 标题跑出表体范围（Excel 里表现为标题盖住表头右侧）。
    last_col = len(cols) or 1
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    ws["A2"] = _text(f"{company} · {book_name} · 期间 {payload['period']}")
    ws["A2"].alignment = CENTER

    # 每个报表列落在**自己那一列**，不整体右移。原来是「第 1 列写行标签，
    # 之后 enumerate(cells, start=2)」：会小企01表有 8 个报表列、其中 2 个是
    # 标题列（无值），于是整张表从第 2 列起就错一列，最右一列直接写丢。
    # STATIC/LINE_NO 的内容在行上（与 printing/preview.py 同一套取法）。
    #
    # 一个 STATIC 都没有时（老形状 / 调用方自己拼的精简 payload：cols 只有
    # label 和 width，没有 kind），把**第 1 列**当标题列。否则行标签根本没
    # 人写 —— 那不只是少一列：行标签是用户可控文本，漏掉它等于让
    # test_excel_export 里那批「标签不注入公式」的用例失去被测对象。
    has_static = any(c.get("kind") == "STATIC" for c in cols)
    static_seen = line_seen = 0
    col_plan: list[tuple[dict, str, int]] = []
    for idx, col in enumerate(cols):
        kind = col.get("kind")
        if not kind and idx == 0 and not has_static:
            kind = "STATIC"
        side = 0
        if kind == "STATIC":
            side = static_seen
            static_seen += 1
        elif kind == "LINE_NO":
            side = line_seen
            line_seen += 1
        col_plan.append((col, kind or "", side))

    r = 4
    for idx, col in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col.get("width", 120) / 7
        c = ws.cell(row=r, column=idx, value=_text(col["label"]))
        c.font = Font(bold=True)
        c.alignment = CENTER
        c.border = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)

    for row in rows:
        r += 1
        style = row.get("style") or {}
        if not isinstance(style, dict):
            style = {}
        cells_by_no = {c.get("col_no"): c for c in row.get("cells") or []}
        # 段标题与合计行加粗。**按半**判：资产负债表的同一行两侧性质不同
        # （第 16 行左边「流动资产合计」要加粗、右边「递延收益」是普通项目），
        # 只认行级 kind 会把右半也印成合计。
        # 单栏模板（利润表）没有 kind_left/kind_right，回落到行级 kind。
        def _side_kind(side: int) -> str:
            k = style.get("kind_right" if side else "kind_left")
            return str(k or style.get("kind") or "")

        def _side_filled(side: int) -> bool:
            return bool(style.get("label_right")) or style.get("line_right") is not None \
                if side else bool(row.get("label")) or style.get("line_left") is not None

        for idx, (col, kind, side) in enumerate(col_plan, start=1):
            sk = _side_kind(side) if _side_filled(side) else ""
            # 官方表通篇没有粗体（大标题除外），表体一律不加粗。
            row_bold = bool(row.get("bold") if side == 0 else style.get("bold_right"))
            if kind == "STATIC":
                # 项目名是用户可控文本：绕过了 _text 就能把报表名/项目名写成公式
                if side:
                    label = style.get("label_right") or ""
                    depth = style.get("indent_right", 0)
                    bold = bool(style.get("bold_right"))
                else:
                    label = row.get("label") or ""
                    depth = row.get("indent", 0)
                    bold = bool(row.get("bold"))
                # 会小企01表：段标题与最外层总计压灰，其余白底。
                if sk == "SHADE":
                    cell_fill = PatternFill("solid", fgColor="D9D9D9")
                else:
                    cell_fill = PatternFill()
                try:
                    depth = int(depth or 0)
                except (TypeError, ValueError):
                    depth = 0
                c = ws.cell(row=r, column=idx, value=_text("　" * depth + str(label)))
                c.alignment = LEFT
                c.font = Font(bold=bold)
                c.fill = cell_fill
            elif kind == "LINE_NO":
                # 行次是法定固定编号，写成字符串：openpyxl 见到纯数字字符串无所谓，
                # 但它可能带 '-'（占位），转成 int 会在这种行上直接抛异常。
                no = style.get("line_right" if side else "line_left")
                c = ws.cell(row=r, column=idx, value="" if no is None else str(no))
                c.alignment = CENTER
                c.font = Font(bold=row_bold)
            else:
                value = (cells_by_no.get(col.get("col_no")) or {}).get("value")
                c = ws.cell(row=r, column=idx, value=_num(value))
                c.alignment = RIGHT
                c.number_format = "#,##0.00"
                c.font = Font(bold=row_bold or bool(row.get("bold")))
            c.border = BOX

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
    ws["A1"] = _text(company)
    ws["A1"].font = Font(size=13, bold=True)
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:D2")
    ws["A2"] = _text(f"{book_name}　期初余额表　{year}年{month}月")
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
