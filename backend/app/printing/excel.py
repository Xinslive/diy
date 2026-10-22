"""Excel 导出：记账凭证 / 总账 / 科目余额表 / 报表。

浏览器打印走 HTML+CSS（前端负责），Excel 导出覆盖 A5 记账凭证、
连续打印纸这类需要二次加工的场景。

**抬头一律不带单位名**（2026-10-20 起）。单位名是账套的属性，不是这张纸的
抬头；每份导出件上都印一遍，只会让同一套账的多个导出件看上去像不同单位的
账。账套名、期间、区间照旧留着 —— 少了它们，纸面上就分不清是哪本账的哪一段。
本模块的六个生成器里只有 `detail_ledger_excel` 与 `vouchers_excel` 本来就没有
单位名，现在六个一致。
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.printing.template import ledger_book_period_label

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
#: 表头那种「上下加粗线」的框
HEADER_BOX = Border(left=THIN, right=THIN, top=MEDIUM, bottom=MEDIUM)

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")

#: **正文用的字体必须是模块级常量，不要在循环里现造。**
#:
#: 一次明细账导出最多 5 万行 × 9 列 = 45 万个格子。``cell.font = Font(...)``
#: 每格都要现造一个 Font 对象（构造 + 哈希 + 序列化），
#: 实测 5 万行：每格现造 **10.0 秒**，复用同一个对象 **4.4 秒**。
#: 而 openpyxl 是全内存构建，省下来的都是实打实的请求时长。
#:
#: 只有两种：正常体与合计行的加粗体。样式表里是同一个东西，文件不变。
FONT_CELL = Font(name="宋体", size=10)
FONT_CELL_BOLD = Font(name="宋体", size=10, bold=True)


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
        ws.cell(row=r, column=c).border = HEADER_BOX

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

#: 合计行用到的八个「按方向归过边」的数，以及试算行的文案 —— **都在
#: ``app.printing.preview`` 里**（``balance_totals`` / ``balance_verdict``）。
#:
#: 合计与试算是**纸面口径**，Excel 导出与 HTML 打印印的是同一张纸：一份
#: 实现、一个数字。以前 Excel 通道自己算、HTML 通道压根没有合计行 ——
#: 「打出来的那张」与「导出来的那张」对不上（审计 P1-4 / P3-3）。
from app.printing.preview import balance_totals as _balance_totals  # noqa: F401
from app.printing.preview import balance_verdict as _verdict  # noqa: F401
from app.printing.template import REPORT_LINE_NO, REPORT_STATIC, report_column_sides


def balance_excel(payload: dict, *, book_name: str) -> bytes:
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
    # 抬头只带账套名与期间，**不带单位名**（2026-10-20 起所有 Excel 导出一致）。
    # 单位名是账套的属性而不是这张纸的抬头，出现在每份导出件上只会让
    # 同一套账的多个导出件看上去像不同单位的账。
    #
    # 账套名同样是建账时填的自由文本，与摘要同一类输入。以前这四处标题格
    # 绕过了 _text：openpyxl 见到以 = 开头就标成公式，导出表里就多了一格
    # 可执行公式，而这份文件是要发给客户和审计方的。
    ws["A2"] = _text(f"{book_name} · 期间 {payload['period']}")
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
        c.border = HEADER_BOX

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
        ws.cell(row=r, column=c).border = HEADER_BOX
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


# --------------------------------------------------------------------- 总账（用友式）

#: 用友总账的八列与列宽（Excel 列宽单位 ≈ 一个半角字符）。
#: 与 ``app/printing/template.py`` 的打印列宽比例一致 —— 打印件与导出件
#: 是同一张纸，列的比例也得看着一样。
LEDGER_BOOK_HEADERS = [
    ("月", 5), ("日", 5), ("凭证号数", 12), ("摘要", 42),
    ("借方", 16), ("贷方", 16), ("方向", 7), ("余额", 16),
]


def _ledger_book_sheet(
    wb: Workbook,
    *,
    sheet_title: str,
    fallback_title: str,
    doc_title: str,
    subject_code: str,
    subject_name: str,
    rows: list[dict],
    year_label: str,
    company: str,
    preparer: str,
    print_date: str,
    period_label: str = "",
):
    """用友账簿的**一张表**：抬头 + 两行表头 + 表体 + 页脚。

    总账与明细账是**同一张纸**，所以这段版式只有一份实现 —— 两处各写
    一遍，迟早有一处少一列，而纸上少一列看不出来。

    表体的行是**用友八列形状**（月 / 日 / 凭证号数 / 摘要 / 借方 / 贷方 /
    方向 / 余额），由 ``app.printing.template.ledger_book_detail_row`` 摊好。

    **标题下面那一行是期间**（用户 2026-10-22）：打印件在标题下面印期间
    副标题，导出件也要有 —— 两个通道是同一张纸，一边有一边没有，纸上与
    文件上就差了一行。
    """
    ws = wb.create_sheet(title=_safe_sheet_title(sheet_title, fallback_title))

    for i, (_label, width) in enumerate(LEDGER_BOOK_HEADERS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    # ---- 抬头 ----
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    head = ws.cell(row=1, column=1, value=_text(doc_title))
    head.font = Font(name="宋体", size=16, bold=True)
    head.alignment = CENTER
    ws.row_dimensions[1].height = 26

    # 副标题 = 期间（与打印件同一串字，见 ledger_book_period_label）。
    if period_label:
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=8)
        sub = ws.cell(row=2, column=1, value=_text(period_label))
        sub.font = Font(name="宋体", size=10)
        sub.alignment = CENTER
        ws.row_dimensions[2].height = 18

    ws.cell(row=3, column=6, value=_text("本币名称: 人民币")).alignment = RIGHT
    ws.merge_cells(start_row=3, start_column=6, end_row=3, end_column=8)
    # 科目行只在真的印某一个科目时才有 —— 整册被「不打印无发生额科目」跳空
    # 之后的那张空白页，不该顶着「科目: ()」出去。
    if subject_name or subject_code:
        ws.cell(row=3, column=1,
                value=_text(f"科目: {subject_name}({subject_code})")).alignment = LEFT

    # ---- 两行表头（年份横跨「月+日」）----
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=2)
    y = ws.cell(row=4, column=1, value=_text(year_label))
    y.font = Font(name="宋体", size=11, bold=True)
    y.alignment = CENTER
    for col, (label, _w) in enumerate(LEDGER_BOOK_HEADERS[2:], start=3):
        ws.merge_cells(start_row=4, start_column=col, end_row=5, end_column=col)
        c = ws.cell(row=4, column=col, value=_text(label))
        c.font = FONT_CELL_BOLD
        c.alignment = CENTER
        c.border = BOX
    for col, label in ((1, "月"), (2, "日")):
        c = ws.cell(row=5, column=col, value=_text(label))
        c.font = FONT_CELL_BOLD
        c.alignment = CENTER
        c.border = BOX
    y.border = BOX

    # ---- 表体 ----
    r = 5
    for row in rows:
        r += 1
        values = [
            _text(row.get("month") or ""),
            _text(row.get("day") or ""),
            _text(row.get("voucher_no") or ""),
            _text(row.get("summary") or ""),
            _num(row.get("debit")),
            _num(row.get("credit")),
            _text(row.get("direction") or ""),
            _num(row.get("balance")),
        ]
        for i, value in enumerate(values, start=1):
            c = ws.cell(row=r, column=i, value=value)
            c.border = BOX
            c.font = FONT_CELL
            c.alignment = (LEFT if i == 4 else
                           CENTER if i in (1, 2, 3, 7) else RIGHT)
        ws.row_dimensions[r].height = 16

    # ---- 页脚 ----
    r += 2
    foot = ws.cell(row=r, column=1,
                   value=_text(f"核算单位: {company}"))
    foot.alignment = LEFT
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    ws.cell(row=r, column=5, value=_text(f"制表: {preparer}")).alignment = CENTER
    ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=6)
    ws.cell(row=r, column=7, value=_text(f"打印日期: {print_date}")).alignment = RIGHT
    ws.merge_cells(start_row=r, start_column=7, end_row=r, end_column=8)

    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A6"
    return ws


def general_ledger_excel(payload: dict, *, book_name: str,
                          company: str = "", preparer: str = "",
                          print_date: str | None = None) -> bytes:
    """总账（用友式）：**一个一级科目一张工作表**，版式与打印件一致。

    以前这里是「一行一个科目」的余额表形态（编码/名称/期初/本期/本年/期末），
    与打印件完全不是一回事 —— 而这两个通道印的是**同一张纸**。现在两边
    一起改成用友的总分类账：一级科目一张表，逐月展开。

    ``payload["subjects"]`` 是 ``[{code, name, rows}, ...]``；每张表的行与
    ``queries.general_ledger`` 的返回一一对应（上年结转 / 本月合计 / 累计 /
    本年累计 / 结转下年）。
    """
    wb = Workbook()
    wb.remove(wb.active)

    subjects = payload.get("subjects") or []
    total_rows = sum(len(s.get("rows") or []) for s in subjects)
    _check_size("总账", total_rows)

    print_date = print_date or date.today().strftime("%Y.%m.%d")
    # 标题下面那一行的期间（与打印件同一串字）。
    period_label = ledger_book_period_label(
        payload.get("from_period"), payload.get("to_period"))
    for index, subject in enumerate(subjects):
        rows = subject.get("rows") or []
        year = str((rows[0].get("period") if rows else "") or "")[:4]
        _ledger_book_sheet(
            wb,
            sheet_title=f"{subject.get('name') or ''}({subject.get('code') or ''})",
            fallback_title=f"总账{index + 1}",
            doc_title=f"{subject.get('name') or ''}总账",
            subject_code=subject.get("code") or "",
            subject_name=subject.get("name") or "",
            rows=rows,
            year_label=f"{year}年",
            company=company or book_name,
            preparer=preparer,
            print_date=print_date,
            period_label=period_label,
        )

    if not subjects:
        # 一个科目都没印（整册都被「不打印无发生额科目」跳空了）时，也要给一张
        # **完整的账簿页** —— 抬头、页脚都在。裸一张「（没有一级科目）」的表不是
        # 账簿页：打印件那一路此时给的是一张带页眉页脚的空白页，两边得是同一张纸。
        _ledger_book_sheet(
            wb,
            sheet_title="总账",
            fallback_title="总账",
            doc_title="总账",
            subject_code="",
            subject_name="",
            rows=[],
            year_label=f"{date.today().year}年",
            company=company or book_name,
            preparer=preparer,
            print_date=print_date,
            period_label=period_label,
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def detail_ledger_excel(payload: dict, *, book_name: str,
                         company: str = "", preparer: str = "",
                         print_date: str | None = None) -> bytes:
    """明细账（用友式）：**一个一级科目一张工作表**，版式与打印件一致。

    以前这里是「一张表塞所有科目」的样子（凭证号 / 摘要 / 借贷 / 方向 /
    余额五列，无月日、无页眉页脚），与打印件不是一回事 —— 而这两个通道
    印的是**同一张纸**。现在与总账共用同一份版式代码：册页单位逐个成表，
    逐笔展开，行是**用友八列形状**。

    册页单位是**一级科目**（用户 2026-10-22 批注 3，与总账一致）：一级科目
    下面是综合明细账时，一张表里排着它那几个下级科目的几块。

    ``payload["subjects"]`` 是 ``[{code, name, rows}, ...]``。
    """
    wb = Workbook()
    wb.remove(wb.active)

    subjects = payload.get("subjects") or []
    # 明细账是「跨全科目跨全年」的那一册，行数上界最高，先卡上限
    # （见 MAX_EXPORT_ROWS）。
    total_rows = sum(len(s.get("rows") or []) for s in subjects)
    _check_size("明细账", total_rows)

    print_date = print_date or date.today().strftime("%Y.%m.%d")
    # 标题下面那一行的期间（与打印件同一串字）。
    period_label = ledger_book_period_label(
        payload.get("from_period"), payload.get("to_period"))
    for index, subject in enumerate(subjects):
        rows = subject.get("rows") or []
        year = str((rows[0].get("period") if rows else "") or "")[:4]
        _ledger_book_sheet(
            wb,
            sheet_title=f"{subject.get('name') or ''}({subject.get('code') or ''})",
            fallback_title=f"明细账{index + 1}",
            doc_title=f"{subject.get('name') or ''}明细账",
            subject_code=subject.get("code") or "",
            subject_name=subject.get("name") or "",
            rows=rows,
            year_label=f"{year}年",
            company=company or book_name,
            preparer=preparer,
            print_date=print_date,
            period_label=period_label,
        )

    if not subjects:
        # 同总账：整册跳空时也给一张完整的空白账簿页（抬头、页脚都在）。
        _ledger_book_sheet(
            wb,
            sheet_title="明细账",
            fallback_title="明细账",
            doc_title="明细账",
            subject_code="",
            subject_name="",
            rows=[],
            year_label=f"{date.today().year}年",
            company=company or book_name,
            preparer=preparer,
            print_date=print_date,
            period_label=period_label,
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- 报表


def report_excel(payload: dict, *, book_name: str) -> bytes:
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
    ws["A2"] = _text(f"{book_name} · 期间 {payload['period']}")
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
    has_static = any(c.get("kind") == REPORT_STATIC for c in cols)
    # 「这一列属于左半还是右半」：**共享实现**（``app.printing.template``）。
    #
    # 判据是「STATIC 列开启的分块」：STATIC 是每一半的首列，从它到下一个
    # STATIC 之间的所有列（含行次列与金额列）都属于同一半。
    #
    # 以前这条通道按「第几个 STATIC / 第几个 LINE_NO」两个**互相独立**的
    # 计数器算，HTML 打印那条通道按 STATIC 分块算 —— 两套实现。分叉的后果
    # 是右半的**金额列**在 Excel 侧落到了左半（用的是 ``kind_left`` 而不是
    # ``kind_right``），于是「流动资产合计」那行的右半（递延收益）被当成合计
    # 加粗压灰；相邻两个 STATIC 列时行次列也会判错半。同一张表在两条通道上
    # 版式不一样（审计 P3-2）。
    side_of = report_column_sides(cols)
    col_plan: list[tuple[dict, str, int]] = []
    for idx, col in enumerate(cols):
        kind = col.get("kind")
        if not kind and idx == 0 and not has_static:
            kind = REPORT_STATIC
        col_plan.append((col, kind or "", side_of.get(col.get("col_no"), 0)))

    r = 4
    for idx, col in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col.get("width", 120) / 7
        c = ws.cell(row=r, column=idx, value=_text(col["label"]))
        c.font = Font(bold=True)
        c.alignment = CENTER
        c.border = HEADER_BOX

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
            if kind == REPORT_STATIC:
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
            elif kind == REPORT_LINE_NO:
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
        # 错误清单不截断：JSON 通道（api/reports.py）和 HTML 通道都给出全量，
        # 这里若只写前 20 条，Excel 上其余出错格就被静默藏起来了。
        for i, e in enumerate(payload["errors"]):
            ws.cell(row=r + i + 1, column=1, value=_text(e)).font = Font(color="CC0000")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def opening_balance_excel(rows: list[dict], *, book_name: str, year: int,
                          month: int | None = None) -> bytes:
    """期初余额表（导入模板 / 导出当前录入值 共用一份格式）。

    列刻意做成「财务系统间最好抄」的样子：科目编码、科目名称、方向、期初余额。
    方向列是必填的 —— 同一个正数在借方科目和贷方科目上存进去是不一样的，
    光看金额抄不出方向，会静默记反。

    **抬头不带单位名**（2026-10-20 起所有 Excel 导出一致）：原先 A1 单独
    一行摆单位名，那一行整行删掉、标题上提，于是表头从第 4 行变成第 3 行、
    数据从第 5 行变成第 4 行。表头仍落在解析端的前 15 行扫描范围里，
    往返导入（导出 → 再喂回解析）不受影响。

    **抬头是年度，不是月份**（2026-10-20）：期初余额是一个会计年度的起点，
    不再按月分份。``month`` 不给就是年度口径（``{年}年度``）；给了才退回到
    旧的「{年}年{月}月」写法 —— 留着是为了那份参数化调用不炸。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "期初余额"

    scope = f"{year}年度" if month is None else f"{year}年{month}月"
    ws.merge_cells("A1:D1")
    ws["A1"] = _text(f"{book_name}　期初余额表　{scope}")
    ws["A1"].font = Font(size=13, bold=True)
    ws["A1"].alignment = Alignment(horizontal="center")

    head = ["科目编码", "科目名称", "方向", "期初余额"]
    for i, h in enumerate(head, start=1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border = Border(bottom=Side(style="thin"))

    r = 4
    for row in rows:
        ws.cell(row=r, column=1, value=_text(row["code"]))
        ws.cell(row=r, column=2, value=_text(row["name"]))
        ws.cell(row=r, column=3, value=_text(row["direction"]))
        ws.cell(row=r, column=4, value=_num(row["amount"]))
        r += 1

    if r > 4:
        _money_style(ws, 3, range(4, 5))     # 列格式（作用于整个 D 列）
        for rr in range(4, r):
            ws.cell(row=rr, column=4).number_format = "#,##0.00"
    for col, w in zip("ABCD", (14, 32, 10, 18)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    return _to_bytes(wb)
