"""对账单的语义层：网格 → 一张张能对账的流水行。

这一层回答四个问题：

1. **哪一行是表头**（银行的表头可能在第 1 行，也可能在第 18 行）；
2. **哪一列是哪一列**（靠别名表 + 结构证据）；
3. **这一行是收还是付**（五条通路，见 :func:`resolve_direction`）；
4. **解析得对不对**（跟文件自己报的总笔数、总金额对一遍）。

输出的 :class:`ParsedStatement` 是**与容器无关**的中间表示：不论来的是
Excel、网页版、CSV 还是 PDF，到这里都是同一种东西。
"""
from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.bankstatement.aliases import (DIRECTION_HINTS, direction_from_word,
                                       match_field)
from app.bankstatement.grid import Grid, cell_text, read_grids

#: 归一化后允许出现的字段集合（人工指定列映射时也用这张表）。
FIELDS = (
    "DATE", "POSTING_DATE", "TIME", "IN", "OUT", "AMOUNT", "BALANCE", "DIRECTION",
    "CP_NAME", "CP_ACCOUNT", "PAYER_NAME", "PAYER_ACCOUNT", "PAYEE_NAME",
    "PAYEE_ACCOUNT", "SUMMARY", "PURPOSE", "TX_TYPE", "EXTRA",
)

#: 「这行不需要处理」的判据：合计、小计、累计、笔数汇总、脚注。
_SUMMARY_NOISE = re.compile(
    r"合计|小计|累计|共计|总计|本页|以下空白|以上|笔数|备注|说明|提示|声明|打印|第\s*\d+\s*页|共\s*\d+\s*页")


# ----------------------------------------------------------------------
# 中间表示
# ----------------------------------------------------------------------

@dataclass
class StatementRow:
    """一行流水。"""

    seq: int
    tx_date: date | None = None
    tx_time: str = ""
    direction: str = "UNKNOWN"       # IN / OUT / UNKNOWN
    amount: Decimal | None = None
    balance: Decimal | None = None
    summary: str = ""
    purpose: str = ""
    counterparty_name: str = ""
    counterparty_account: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


@dataclass
class StatementMeta:
    """文件自己报出来的账户信息。"""

    account_no: str = ""
    account_name: str = ""
    bank_name: str = ""
    currency: str = ""
    period_start: date | None = None
    period_end: date | None = None
    closing_balance: Decimal | None = None
    declared_income_count: int | None = None
    declared_income_total: Decimal | None = None
    declared_expense_count: int | None = None
    declared_expense_total: Decimal | None = None
    #: 文件自报的**总**笔数（不分收支）。兴业那种模板只给总数，
    #: 拿它跟「收入笔数 + 支出笔数」对，才知道有没有漏行。
    declared_total_count: int | None = None


@dataclass
class ParsedStatement:
    """一份对账单解析完的结果。"""

    container: str
    sheet: str = ""
    header_row: int = -1
    header_cells: list[str] = field(default_factory=list)
    column_mapping: dict[str, int] = field(default_factory=dict)
    rows: list[StatementRow] = field(default_factory=list)
    meta: StatementMeta = field(default_factory=StatementMeta)
    diagnosis: list[str] = field(default_factory=list)
    grid_preview: list[list[str]] = field(default_factory=list)
    fingerprint: str = ""
    #: 这一份是**走 OCR 认出来的**（不是读 PDF 文字层）。
    #:
    #: 界面上要说出来，原因有两个：OCR 是**识别**、会认错字（文字层是精确
    #: 取字），而这份文件的正文**离开过本机**（交给外部 OCR 服务）。这两件事
    #: 用户有权知道。
    via_ocr: bool = False
    #: 这一份是**两路合并**出来的（文字层 + OCR 各认出一半，缺的那半
    #: 从另一路补上）。界面上同样要说出来：合并就意味着 OCR 参与了，
    #: 正文离开过本机。
    via_merge: bool = False
    #: 两路解析**认得不一样**的地方（哪一行、哪个字段、两路各是什么）。
    #:
    #: 这不是「解析失败」—— 哪一份对由自检裁定（见
    #: :func:`app.bankstatement.service._merge_parsed`），但差异要**能被人
    #: 看见**：一份文件两路读出来不一样，本身就是值得知道的事。
    merge_conflicts: list[str] = field(default_factory=list)
    #: 自检结论在 ``diagnosis`` 里的**位置**。诊断分两段：前面是**解析期**
    #: 的结论（表头认不出、有 N 行没日期），后面是**自检**的结论（自称
    #: 笔数对不上、方向认不出、余额接不上）。
    #:
    #: 记这个位置是为了**重跑自检时只换自己那一段** —— 合并补上字段之后
    #: 自检的结论会变样（方向补齐了，「有 N 行认不出收支方向」就该消失），
    #: 而解析期的结论（表头认不出）与别人追加的诊断不能被顺手清掉。
    check_notes_start: int = 0
    #: 上一轮自检产生了几条结论（重跑时据此精确地摘掉那一段）。
    check_notes_count: int = 0

    @property
    def income_total(self) -> Decimal:
        return sum((row.amount for row in self.rows
                    if row.direction == "IN" and row.amount is not None), Decimal("0"))

    @property
    def expense_total(self) -> Decimal:
        return sum((row.amount for row in self.rows
                    if row.direction == "OUT" and row.amount is not None), Decimal("0"))


# ----------------------------------------------------------------------
# 值归一
# ----------------------------------------------------------------------

_FULLWIDTH = str.maketrans("０１２３４５６７８９．，－（）　",
                            "0123456789.,-() ")

_CURRENCY = "¥￥$€£元人民币RMB人民币"


def parse_amount(value: Any) -> Decimal | None:
    """一个单元格 → 金额。

    认得这些写法：``5,993.37``、``13 050.00``（PDF 里常带空格）、
    ``¥100``、``(123.45)``（会计上的负数）、``-``（表示没有）、
    ``0.0``（农商 xlsx 用它表示「这一侧没有发生额」——**是 0，不是空**）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    text = str(value).strip().translate(_FULLWIDTH)
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    text = "".join(ch for ch in text if ch not in _CURRENCY and ch not in ",， \t\r\n")
    if text in ("", "-", "--", "—", "/", "\\"):
        return None
    if text.endswith("-"):
        negative = not negative
        text = text[:-1]
    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -amount if negative else amount


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y年%m月%d日",
    "%Y-%m-%d%H:%M:%S", "%Y-%m-%d%H:%M", "%Y/%m/%d%H:%M:%S", "%Y/%m/%d%H:%M",
    "%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S",
)


def parse_date(value: Any) -> tuple[date | None, str]:
    """一个单元格 → ``(日期, 时间文本)``。

    时间文本单独留着，是因为平安那种表只有一列「交易时间」，里面是
    ``2026-09-30 00:16:48`` —— 日期与时间都在一格里。

    解析前先**去掉所有空白**：PDF 里折行的时间会变成
    ``2026-09-26 07: 20:10``，去掉空白后就是 ``2026-09-2607:20:10``，
    正好能被 ``%Y-%m-%d%H:%M:%S`` 吃掉。
    """
    if value is None:
        return None, ""
    if isinstance(value, datetime):
        return value.date(), value.strftime("%H:%M:%S")
    if isinstance(value, date):
        return value, ""
    # **Excel 序列号**：.xls 里日期常常存成数字（单元格没设成日期格式时）。
    #
    # 换算的基准是 1899-12-30 —— 这是 Excel 那个著名的 1900 闰年bug 的
    # 补偿（Excel 认定 1900 年是闰年，序列号 60 是并不存在的 2 月 29 日；
    # 用 1899-12-30 做基准正好把这个偏移吃掉了）。
    #
    # **只认整数且落在合理区间里**：区间外的数字（0、负数、六位数以上）
    # 更可能是金额而不是日期，放进来会把金额解析成日期。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = int(value)
        if 1 <= serial <= 80000 and float(value) == serial:
            return date(1899, 12, 30) + timedelta(days=serial), ""
    text = str(value).strip().translate(_FULLWIDTH)
    if not text:
        return None, ""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None, ""
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(compact, fmt)
        except ValueError:
            continue
        if any(token in fmt for token in ("%H", "%M", "%S")):
            return parsed.date(), parsed.strftime("%H:%M:%S")
        # **只有日期的写法不要编一个时间出来。** ``strptime("20260901",
        # "%Y%m%d")`` 会得到 00:00:00，而那是**编的** —— 调用方会以为
        # 「时间确实是零点」，于是不再去看旁边那列「交易时间」。
        # 农商行那份 Excel 就是日期一列、时间一列分开的：时间被丢掉之后，
        # 同一天里的多笔流水排不出先后，**余额链接不上**，末行余额是
        # 「哪一笔之后的」就成了猜。
        return parsed.date(), ""
    # 「2026-09-01」这种带分隔符的两位年份写法（09/01/2026）也认一下
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(compact, fmt).date(), ""
        except ValueError:
            continue
    return None, ""


def looks_like_amount(text: str) -> bool:
    """这个格子看起来是不是金额（用来做结构证据）。"""
    if not str(text).strip():
        return False
    stripped = str(text).strip().translate(_FULLWIDTH)
    stripped = "".join(ch for ch in stripped if ch.isdigit() or ch in ".,-()")
    return bool(stripped) and any(ch.isdigit() for ch in stripped)


def looks_like_date(text: str) -> bool:
    """这个格子看起来是不是日期。"""
    parsed, _time = parse_date(text)
    return parsed is not None


def parse_time(value: Any) -> str:
    """一个单元格 → 时间文本（``HH:MM:SS``）。

    **为什么要单独一个函数**：``parse_date`` 吃的是**日期**，而「交易时间」
    那一列里装的是**纯时间**（``16:55:22``）。把它交给 ``parse_date`` 会
    什么也得不到（纯时间不在任何日期格式里）—— 时间就此丢掉。

    **丢掉的时间是有代价的**：同一天里有多笔流水时，光有日期排不出先后，
    **余额链就接不上**。招商那份就是（两笔 09-28 的流水一进一出，时分秒
    一丢就分不出谁在前，「末行余额是哪笔之后的」就成了猜），而**接不上的
    余额链意味着推不出期末余额**，期末余额对账整个用不了。

    认得：``16:55:22`` / ``16:55`` / ``16时55分22秒``（全角也认）。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    text = str(value).strip().translate(_FULLWIDTH)
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    match = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", compact)
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return ""
    return f"{hour:02d}:{minute:02d}:{second:02d}"


# ----------------------------------------------------------------------
# 表头定位与列映射
# ----------------------------------------------------------------------

def _score_header(cells: list[str]) -> int:
    """一行当表头能认出几个字段。

    必需要有的东西是**日期**与**金额**（或收入/支出这一对）：只认出
    「余额」「摘要」的表头，多半是数据行里碰巧出现了这两个词。
    """
    fields: set[str] = set()
    for cell in cells:
        hit = match_field(cell)
        if hit:
            fields.add(hit[0])
    if not fields:
        return 0
    has_money = bool(fields & {"AMOUNT", "IN", "OUT"})
    has_date = bool(fields & {"DATE", "TIME", "POSTING_DATE"})
    if not (has_money and has_date):
        return 0
    return len(fields)


def _merge_two(cells: list[str], other: list[str]) -> list[str]:
    """把两行表头拼成一行（有些银行把列名分两行放）。"""
    width = max(len(cells), len(other))
    out: list[str] = []
    for index in range(width):
        top = cells[index] if index < len(cells) else ""
        bottom = other[index] if index < len(other) else ""
        out.append(f"{top}{bottom}")
    return out


def find_header(rows: list[list[Any]]) -> tuple[int, list[str]]:
    """在网格里找表头行，返回 ``(行号, 表头单元格)``；找不到返回 ``(-1, [])``。"""
    best_index = -1
    best_cells: list[str] = []
    best_score = 0
    for index, row in enumerate(rows):
        cells = [cell_text(cell) for cell in row]
        score = _score_header(cells)
        if score > best_score:
            best_score = score
            best_index = index
            best_cells = cells
        if score >= 1 and index + 1 < len(rows):
            merged = _merge_two(cells, [cell_text(cell) for cell in rows[index + 1]])
            merged_score = _score_header(merged)
            if merged_score > best_score:
                best_score = merged_score
                best_index = index
                best_cells = merged
    return best_index, best_cells


def map_columns(header: list[str], sample_rows: list[list[Any]]) -> dict[str, int]:
    """表头 → ``{字段: 列号}``。

    名称证据是主；名称认不出时用**结构证据**补：

    - 有一列全是日期 → 它是日期列；
    - 有一列全是金额、且另有一列是方向 → 它是金额列；
    - 有两列金额，一列恒为 0/空 → 这一对是收入/支出。
    """
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for index, cell in enumerate(header):
        hit = match_field(cell)
        if hit is None:
            continue
        field = hit[0]
        if field in mapping:
            continue
        mapping[field] = index
        used.add(index)

    def column_values(index: int) -> list[str]:
        values = []
        for row in sample_rows:
            if index < len(row):
                values.append(cell_text(row[index]))
        return values

    # 「交易时间」这种一格里同时有日期和时间的列：日期列缺了就顶上
    if "DATE" not in mapping and "TIME" in mapping:
        mapping["DATE"] = mapping["TIME"]
    if "DATE" not in mapping and "POSTING_DATE" in mapping:
        mapping["DATE"] = mapping["POSTING_DATE"]

    # 结构证据：找出没被认领的列里，哪一列全是日期 / 全是金额
    if "DATE" not in mapping:
        for index in range(len(header)):
            if index in used:
                continue
            values = [value for value in column_values(index) if value]
            if values and all(looks_like_date(value) for value in values):
                mapping["DATE"] = index
                used.add(index)
                break
    if "AMOUNT" not in mapping and "IN" not in mapping and "OUT" not in mapping:
        for index in range(len(header)):
            if index in used:
                continue
            values = [value for value in column_values(index) if value]
            if values and all(looks_like_amount(value) for value in values):
                mapping["AMOUNT"] = index
                used.add(index)
                break
    if "DIRECTION" not in mapping:
        for index in range(len(header)):
            if index in used:
                continue
            values = [value for value in column_values(index) if value]
            if values and all(direction_from_word(value) for value in values):
                mapping["DIRECTION"] = index
                used.add(index)
                break
    return mapping


# ----------------------------------------------------------------------
# 方向判定
# ----------------------------------------------------------------------

def resolve_direction(*, amount_in: Decimal | None, amount_out: Decimal | None,
                      amount: Decimal | None, direction_text: str,
                      tx_type: str, payer: str, payee: str,
                      summary: str, account_no: str, account_name: str) -> tuple[str, str]:
    """这一行是收还是付 → ``(方向, 依据)``。

    五条通路，按可信度从高到低：

    1. **收入/支出两列** —— 文件自己把两侧分开了，最可信；
    2. **方向列**（借/贷、收支方向、收入/支出）；
    3. **交易类型列**（转入/转出、汇入汇款…）—— 有些银行的这一列确实带方向；
    4. **本户身份** —— 付款方是本户就是支出、收款方是本户就是收入
       （平安那种「付款方/收款方」双列表）；
    5. **摘要提示** —— 只在前四条都给不出结论时用，且标出来是「推测」。

    返回的「依据」会显示在界面上：用户看到「按摘要推测」就知道这行
    可能不准。
    """
    if amount_in is not None or amount_out is not None:
        # 文件把两侧分成两列时，以**哪一侧大于 0**为准。
        #
        # 判据是「> 0」而不是「有值」：农商的 xlsx 用 ``0.0`` 表示
        # 「这一侧没有发生额」，看「非空」会把 0.00 当成一笔收入。
        in_positive = amount_in is not None and amount_in > 0
        out_positive = amount_out is not None and amount_out > 0
        if in_positive and out_positive:
            return "UNKNOWN", "收入与支出两列都有金额"
        if in_positive:
            return "IN", "收入列"
        if out_positive:
            return "OUT", "支出列"
        return "UNKNOWN", "收入与支出两列都是 0"
    direction = direction_from_word(direction_text)
    if direction:
        return direction, "方向列"
    direction = direction_from_word(tx_type)
    if direction:
        return direction, "交易类型"
    identity = _direction_by_identity(payer, payee, account_no, account_name)
    if identity:
        return identity, "本户身份"
    for word, hint in DIRECTION_HINTS.items():
        if word in summary:
            return hint, "按摘要推测"
    return "UNKNOWN", ""


def _direction_by_identity(payer: str, payee: str, account_no: str,
                           account_name: str) -> str | None:
    """本户在付款方还是收款方。

    账号带脱敏（``6230****5263``）也能比 —— 比的就是**同一串脱敏文本**，
    文件里两边写的是一样的。
    """
    if not (payer or payee):
        return None
    identities = [value for value in (account_no, account_name) if value]
    if not identities:
        return None
    payer_hit = any(value and value in payer for value in identities)
    payee_hit = any(value and value in payee for value in identities)
    if payer_hit and not payee_hit:
        return "OUT"
    if payee_hit and not payer_hit:
        return "IN"
    return None


# ----------------------------------------------------------------------
# 元信息
# ----------------------------------------------------------------------

def _search(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def extract_meta(lines: list[str], rows: list[StatementRow]) -> StatementMeta:
    """从表头之外的自由文本里抽账户信息。"""
    blob = " ".join(lines)
    meta = StatementMeta()
    # 标签前面必须**没有别的字**：``账户名称:`` 里也含「户名」，
    # 不设这道门槛就会把「对方单位名称」当成户名，抓出一截「称:」。
    meta.account_no = _first_of(
        _search(r"(?:^|[\s，,；;、|])(?:账\s*号|卡\s*号)\s*[:：]\s*\[?\s*([0-9A-Za-z*\-]+)", blob),
        _search(r"(?:^|[\s，,；;、|])账户\s*[:：]\s*\[?\s*([0-9A-Za-z*\-]+)", blob),
    )
    meta.account_name = _search(r"(?:^|[\s，,；;、|])户\s*名\s*[:：]\s*([^\s,，]+)", blob)
    meta.bank_name = _search(r"(?:^|[\s，,；;、|])开户行\s*[:：]\s*([^\s,，]+)", blob)
    meta.currency = _search(r"(?:^|[\s，,；;、|])币\s*种\s*[:：]\s*([^\s,，]+)", blob)

    start = _search(r"(?:起始日期|开始日期)[:：]?\s*([0-9\-/年月日\.]+)", blob)
    end = _search(r"(?:结束日期|截至日期|终止日期)[:：]?\s*([0-9\-/年月日\.]+)", blob)
    if not start or not end:
        span = re.search(r"本期时间范围[:：]?\s*([0-9\-/年月日\.\s:]+?)至\s*([0-9\-/年月日\.\s:]+)", blob)
        if span:
            start = start or span.group(1)
            end = end or span.group(2)
    meta.period_start, _ = parse_date(start)
    meta.period_end, _ = parse_date(end)

    closing = _search(r"期末(?:账户)?余额[:：]?\s*([\d,，\. ]+)", blob)
    meta.closing_balance = parse_amount(closing) if closing else None

    meta.declared_expense_count = _int_or_none(
        _search(r"支出笔数[:：]?\s*(\d+)", blob)
        or _search(r"借方笔数[:：]?\s*(\d+)", blob))
    meta.declared_expense_total = _decimal_or_none(
        _search(r"支出金额[:：]?\s*([\d,，\. ]+)", blob)
        or _search(r"借方发生额[:：]?\s*([\d,，\. ]+)", blob)
        or _search(r"借方累计发生额[:：]?\s*([\d,，\. ]+)", blob)
        or _search(r"本期总支出金额[:：]?\s*([\d,，\. ]+)", blob))
    meta.declared_income_count = _int_or_none(
        _search(r"存入笔数[:：]?\s*(\d+)", blob)
        or _search(r"贷方笔数[:：]?\s*(\d+)", blob))
    # 「本期总笔数」是**总**数，不分收支。
    meta.declared_total_count = _int_or_none(
        _search(r"本期总笔数[:：]?\s*(\d+)", blob))
    meta.declared_income_total = _decimal_or_none(
        _search(r"存入金额[:：]?\s*([\d,，\. ]+)", blob)
        or _search(r"贷方发生额[:：]?\s*([\d,，\. ]+)", blob)
        or _search(r"贷方累计发生额[:：]?\s*([\d,，\. ]+)", blob)
        or _search(r"本期总收入金额[:：]?\s*([\d,，\. ]+)", blob))

    # 期间以**行里的日期**为准：文件自报的期间可能写得含糊（"2026-09"），
    # 而流水日期是实打实的。
    dates = [row.tx_date for row in rows if row.tx_date]
    if dates:
        meta.period_start = min(dates)
        meta.period_end = max(dates)
    return meta


def _is_label_row(cells: list[str], mapped_indexes: set[int]) -> bool:
    """一行只有首格有内容 → 它是标签行，不是流水行。

    页尾的「本期总收入金额：22.80」「重要提示:」「合计：1,234.00」都是这种。
    这种行**不能当流水**：它没有日期，混进来会被当成「有金额的怪行」，
    而用户在诊断里看到的是「有 N 行没有日期」—— 他会以为丢了交易。
    但它也不是垃圾：文件自报的总额就写在这儿，后面的自检要拿它当基准，
    所以归到页脚，不扔掉。

    「只有首格有内容」是结构证据，不看具体措辞 —— 各家银行写页尾的
    词五花八门（本期合计 / 小计 / 累计 / 本页合计……），逐个认词永远漏。
    """
    if len(cells) < 2 or not cells[0].strip():
        return False
    # 在所有认出来的数据列上都是空的：这行压根不是一条流水。
    # 首格不算数据列 —— 它放的就是这句标签本身。
    for index in mapped_indexes:
        if index <= 0:
            continue
        if index < len(cells) and cells[index].strip():
            return False
    return True


def _first_of(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def _int_or_none(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _decimal_or_none(text: str) -> Decimal | None:
    return parse_amount(text)


# ----------------------------------------------------------------------
# 解析入口
# ----------------------------------------------------------------------

def parse_statement(data: bytes, *, column_mapping: dict[str, int] | None = None,
                    header_row: int | None = None) -> ParsedStatement:
    """把一份对账单文件解析成流水行。

    ``column_mapping`` / ``header_row`` 是**人工指定**的入口：自动映射认不
    出来时（没见过的银行格式），用户在页面上把「第几列是什么」填了，
    这里照着那份指定重跑一遍。给 ``None`` 时走自动那条路。
    """
    grids = read_grids(data)
    if not grids:
        raise ValueError("这份文件里没有可读的数据")
    best = grids[0]
    best_score = -1
    for grid in grids:
        score = _grid_score(grid)
        if score > best_score:
            best_score = score
            best = grid
    parsed = parse_grid(best, column_mapping=column_mapping, header_row=header_row)
    if parsed.header_row < 0:
        raise ValueError(
            "没能认出表头（没找到同时含日期与金额的表头行）。"
            "可以在页面上手工指定各列")
    return parsed


def _grid_score(grid: Grid) -> int:
    """这张网格有多像一张对账单（用来在多 sheet 里挑）。"""
    best = 0
    for row in grid.rows[:60]:
        best = max(best, _score_header([cell_text(cell) for cell in row]))
    return best


def parse_grid(grid: Grid, *, column_mapping: dict[str, int] | None = None,
               header_row: int | None = None) -> ParsedStatement:
    """一张网格 → 解析结果。

    ``column_mapping`` 给定时**完全取代**自动映射：这是「用户已经亲手
    指定过了」的场合 —— 自动映射认不出来只是「这一家银行的列名怪」，
    不是「列名里藏着线索」，这时候再自动一遍只会把错的答案又算一遍。
    """
    result = ParsedStatement(container=grid.container, sheet=grid.sheet)
    preview = [[cell_text(cell) for cell in row] for row in grid.rows[:20]]
    result.grid_preview = preview

    if grid.container == "PDF":
        header_cells = [cell_text(cell) for cell in grid.rows[0]] if grid.rows else []
        data_rows = grid.rows[1:]
        meta_lines = list(grid.meta)
        footer_lines = list(grid.footer)
        header_index = 0
    else:
        if header_row is not None:
            # 人工指定了表头行：照它切，认不出来也照切（用户说第几行就是
            # 第几行 —— 这里的失败模式是「切出来的行里没有流水」，那会在
            # 界面上表现为「0 行」，比自动乱猜一个表头好）。
            header_index = int(header_row)
            header_cells = ([cell_text(cell) for cell in grid.rows[header_index]]
                            if 0 <= header_index < len(grid.rows) else [])
            if header_index < 0 or header_index >= len(grid.rows):
                return result
        else:
            header_index, header_cells = find_header(grid.rows)
            if header_index < 0:
                return result
        data_rows = grid.rows[header_index + 1:]
        meta_lines = []
        for row in grid.rows[:header_index]:
            text = " ".join(cell_text(cell) for cell in row).strip()
            if text:
                meta_lines.append(text)
        footer_lines = []

    result.header_row = header_index
    result.header_cells = header_cells
    mapping = (dict(column_mapping) if column_mapping
               else map_columns(header_cells, data_rows[:40]))
    result.column_mapping = mapping
    result.fingerprint = fingerprint_of(grid.container, header_cells)

    if not mapping.get("DATE") and not mapping.get("AMOUNT") \
            and not mapping.get("IN") and not mapping.get("OUT"):
        result.diagnosis.append("没认出任何可用列：这份文件的表头不像常见的对账单")

    # 先从表头之外的自由文本里抽一次账户信息：方向判定里的「本户身份」
    # 通路要靠它（平安那种「付款方/收款方」双列表只有本户身份能定方向）。
    pre_meta = extract_meta(meta_lines, [])

    seq = 0
    dateless = 0
    skipped: list[str] = []
    mapped_indexes = {index for index in mapping.values()
                      if index is not None and index >= 0}
    for row in data_rows:
        cells = [cell_text(cell) for cell in row]
        if not any(value for value in cells):
            continue
        # 合计行、脚注行：首格写着「以上数据仅供参考」「本期合计」这类话，
        # 整行不是流水。（建行那张表最后一行就是这种脚注。）
        if _SUMMARY_NOISE.search(cells[0]) and not looks_like_date(cells[0]):
            # 归页脚而不是直接扔掉：文件自报的总额常写在这些行里，
            # 后面「跟文件自己报的数字对一遍」要拿它当基准。
            footer_lines.append(" ".join(cells))
            continue
        # 只有首格有内容的行同样是标签行（「本期总收入金额：22.80」）。
        if _is_label_row(cells, mapped_indexes):
            footer_lines.append(" ".join(cells))
            continue
        statement_row = _build_row(seq, cells, mapping, pre_meta)
        if statement_row is None:
            continue
        if statement_row.tx_date is None:
            # **没有日期的行不留**：它进不了任何一个会计期间，也没法跟发票
            # 按「金额 + 收支方向」对上，留着只会变成一张永远对不上的行。
            # 真实文件里这种行确实存在（平安那张表的末尾就跟着三行没有日期
            # 的杂数据）。
            #
            # 但必须**说出来**：这类行里既有页尾的杂数据，也有可能真是
            # 一笔日期格式没认出来的交易。把内容摘进诊断，用户能自己核对，
            # 而不是只看到一句「已略过 N 行」就去猜少了多少钱。
            dateless += 1
            if len(skipped) < 3:
                # 摘第一个有内容的格子：平安那种杂数据的首格是空的，
                # 只报「、、」等于什么都没报。
                sample = next((value for value in cells if value.strip()), "")
                if sample:
                    skipped.append(sample[:24])
            continue
        if statement_row.amount is None:
            # 有日期没金额：留下并标出来（可能是拆单行或只记了一侧的流水）。
            pass
        seq += 1
        statement_row.seq = seq
        result.rows.append(statement_row)

    if dateless:
        detail = f"（例如：{'、'.join(skipped)}）" if skipped else ""
        result.diagnosis.append(f"有 {dateless} 行没有日期，未计入流水{detail}")

    result.meta = extract_meta(meta_lines + footer_lines, result.rows)
    if result.meta.closing_balance is None:
        # 文件**没写**期末余额，但余额列里其实有 —— 从余额列推。
        result.meta.closing_balance = _closing_from_rows(result.rows, result.meta)
    _self_check(result)
    return result


def _link_ok(previous: StatementRow | None, current: StatementRow | None) -> bool:
    """「上一笔的余额 ± 这一笔的金额 = 这一笔的余额」这一段接不接得上。

    **接不上不算错**：这一笔的金额认不出来（``None``）时，这一段**没法验**，
    验不了的事不当它错 —— 当成「过」才是诚实的做法。
    """
    if previous is None or current is None:
        return True
    if current.amount is None or previous.balance is None or current.balance is None:
        return True
    delta = abs(previous.balance - current.balance)
    return abs(delta - abs(current.amount)) <= Decimal("0.05")


#: 一组「同一秒」的流水最多试多少种排法。``6! = 720`` 种，每种验几段链 ——
#: 够快，也够把「同一秒里挤了十几笔」那种极端情况挡在外面。
_MAX_TIE_PERMUTATIONS = 6


def _order_tie_groups(ordered: list[StatementRow]) -> list[StatementRow]:
    """同一秒的几笔，谁先谁后 —— 由**余额链**决定。

    **为什么需要**：有的银行会把两笔印成**同一秒**。兴业那份就有：冲账那笔
    和它撤销的那笔，时间戳完全一样（``19:13:38`` 两笔）。按时间排序时它们
    谁先谁后是**任意的**（排序键一样，顺序只能靠文件里本来谁在前面），
    而**余额链**是唯一的判据。

    排错了会怎样：余额链**接不上** → 推不出期末余额 → 期末余额对账整个用不了，
    界面上还会说「对账单未提供余额列」—— 而**余额列明明有**。

    **排不出来就保持原样**：宁可不给期末余额，也不要给一个错的。
    """
    out: list[StatementRow] = []
    index = 0
    total = len(ordered)
    while index < total:
        key = (ordered[index].tx_date, ordered[index].tx_time or "")
        end = index + 1
        while end < total and (ordered[end].tx_date, ordered[end].tx_time or "") == key:
            end += 1
        group = ordered[index:end]
        out.extend(group if len(group) == 1 else _resolve_tie_group(group, ordered, index, end))
        index = end
    return out


def _resolve_tie_group(group: list[StatementRow], ordered: list[StatementRow],
                       start: int, end: int) -> list[StatementRow]:
    """一组「同一秒」的流水 → 排出来的顺序。

    判据是**余额链**：这一组按某种顺序摆好时，前一笔接得上第一笔、组内
    接得上、最后一笔接得上后一笔，就用那个顺序。

    **都接不上就保持原样**（返回 ``group``）—— 宁可不给期末余额，
    也不要给一个错的。
    """
    if len(group) > _MAX_TIE_PERMUTATIONS:
        return group
    before = ordered[start - 1] if start > 0 else None
    after = ordered[end] if end < len(ordered) else None
    for candidate in itertools.permutations(group):
        if not _link_ok(before, candidate[0]):
            continue
        if not _link_ok(candidate[-1], after):
            continue
        if all(_link_ok(candidate[index], candidate[index + 1])
               for index in range(len(candidate) - 1)):
            return list(candidate)
    return group


def _closing_from_rows(rows: list[StatementRow],
                       meta: StatementMeta) -> Decimal | None:
    """从**余额列**推出期末余额。

    **为什么需要**：不少银行的对账单**只有流水、没有「期末余额」行** ——
    农商行那份 Excel 就是这样（表头有「账户余额」列，页尾只有「收入合计」）。
    少了这个数，期末余额对账（对账单期末余额 vs 账面余额）整个用不了，
    界面只显示一句「对账单未提供余额列，无法核对」—— 而**余额列明明有**。

    **推法**：余额列记的是**每笔交易之后**的余额（实测 14 份真实样例里
    绝大多数如此），所以**时间上最后一笔的余额就是期末余额**。

    **时间上最后**，不是「文件里最后一行」：有的银行倒序印流水（最新的在
    最上面），还有的同一天里有多笔 —— 靠「日期 + 时分秒」排才排得对。

    **什么时候不推**（宁可留空，也不要给一个错的数）：

    - 余额链**接不上**（相邻两笔的余额差 ≠ 后一笔的金额）→ 余额列的语义
      不是「交易后余额」，或者中间有没被解析出来的行；
    - 文件自己报了总笔数/收支合计，而解析出来的对不上 → **漏了行**，
      那么末行余额根本不是期末余额；
    - 一行余额都没有。
    """
    dated = [row for row in rows if row.balance is not None and row.tx_date is not None]
    if not dated:
        return None
    ordered = _order_tie_groups(
        sorted(dated, key=lambda row: (row.tx_date, row.tx_time or "")))

    # 余额链校验：相邻两笔（时间顺序）的余额差，应当等于**后一笔**的金额
    # —— 余额记的是交易之后的。
    chain = [row for row in ordered if row.amount is not None]
    for previous, current in zip(chain, chain[1:]):
        delta = abs(current.balance - previous.balance)
        if abs(delta - abs(current.amount)) > Decimal("0.05"):
            return None

    # 文件自报的数字与解析结果对不上 → 漏了行 → 末行余额不是期末余额。
    if meta.declared_total_count is not None and meta.declared_total_count != len(rows):
        return None
    if all(row.direction in ("IN", "OUT") for row in rows):
        income = sum((row.amount for row in rows
                      if row.direction == "IN" and row.amount is not None), Decimal("0"))
        expense = sum((row.amount for row in rows
                       if row.direction == "OUT" and row.amount is not None), Decimal("0"))
        if meta.declared_income_total is not None and meta.declared_income_total != income:
            return None
        if meta.declared_expense_total is not None and meta.declared_expense_total != expense:
            return None
    return ordered[-1].balance


def _normalize_negative_sides(amount_in: Decimal | None, amount_out: Decimal | None
                              ) -> tuple[Decimal | None, Decimal | None, str]:
    """把「负数金额」翻译成方向。

    **为什么必须有这一步**：银行把**冲账**（当天撤销的一笔）印成负数。
    兴业那份对账单里就有一行：支出列写着 ``-13,510.00``、摘要写「当日冲账」
    —— 意思是「上午那笔转出被撤销了，钱退回来了」。

    **不处理会怎样**：负数落在支出列里，``amount_out > 0`` 不成立 →
    这一行既没有金额（``None``）也没有方向（``UNKNOWN``）→ 它成了整份对账单里
    唯一一个「有余额、没金额」的行 → 余额连续性自检时 ``Decimal + None``
    直接抛 ``TypeError`` → **上传直接失败**，而文件本身一点毛病都没有。

    口径：**金额永远存绝对值，方向由符号决定** ——
    支出列的负数 = 被撤销的支出 = 钱退回来了 = **收入**；
    收入列的负数 = 被撤销的收入 = 钱又出去了 = **支出**。

    返回 ``(收入侧, 支出侧, 说明)``。说明会进 ``extra``（接口里带得出来），
    而这一行的**摘要列**写的就是「当日冲账」—— 用户在界面上能直接看到
    自己账上有一笔被撤销过。
    """
    if amount_out is not None and amount_out < 0:
        # 另一侧没有发生额（``None`` 或 ``0``）才翻 —— 两侧都有数时那本来
        # 就是一行「方向自相矛盾」的数据，交给方向判定去报 UNKNOWN，别猜。
        if amount_in is None or amount_in <= 0:
            return -amount_out, None, "支出列为负数（冲账），按收入记"
    if amount_in is not None and amount_in < 0:
        if amount_out is None or amount_out <= 0:
            return None, -amount_in, "收入列为负数（冲账），按支出记"
    return amount_in, amount_out, ""


def _build_row(seq: int, cells: list[str], mapping: dict[str, int],
               meta: StatementMeta) -> StatementRow | None:
    def value(field: str) -> str:
        index = mapping.get(field)
        if index is None or index >= len(cells):
            return ""
        return cells[index].strip()

    row = StatementRow(seq=seq)
    date_text = value("DATE")
    if not date_text:
        date_text = value("POSTING_DATE")
    row.tx_date, row.tx_time = parse_date(date_text)
    if not row.tx_time:
        row.tx_time = parse_time(value("TIME"))

    row.amount = parse_amount(value("AMOUNT"))
    amount_in = parse_amount(value("IN"))
    amount_out = parse_amount(value("OUT"))
    # **负数金额 = 冲账**：方向由符号决定，金额一律存绝对值。
    amount_in, amount_out, sign_note = _normalize_negative_sides(amount_in, amount_out)
    row.balance = parse_amount(value("BALANCE"))
    row.summary = value("SUMMARY")
    row.purpose = value("PURPOSE")
    row.counterparty_name = value("CP_NAME")
    row.counterparty_account = value("CP_ACCOUNT")

    payer_name = value("PAYER_NAME")
    payee_name = value("PAYEE_NAME")
    payer = " ".join(filter(None, [payer_name, value("PAYER_ACCOUNT")]))
    payee = " ".join(filter(None, [payee_name, value("PAYEE_ACCOUNT")]))

    extra = value("EXTRA")
    if extra:
        row.extra["extra"] = extra
    row.extra["direction_basis"] = ""
    row.extra["tx_type"] = value("TX_TYPE")
    if value("POSTING_DATE"):
        row.extra["posting_date"] = value("POSTING_DATE")

    if row.amount is None:
        if amount_in is not None and amount_in > 0:
            row.amount = amount_in
        elif amount_out is not None and amount_out > 0:
            row.amount = amount_out

    # 单一「金额」列（没有收入/支出两列）且金额为负：**符号自己就是方向**。
    # 金额存绝对值（负金额会把收入/支出合计算反），方向在判出来之后翻。
    single_negative = (row.amount is not None and row.amount < 0
                       and amount_in is None and amount_out is None)
    if single_negative:
        row.amount = -row.amount
        sign_note = sign_note or "金额列为负数，方向按符号翻转"

    direction, basis = resolve_direction(
        amount_in=amount_in, amount_out=amount_out, amount=row.amount,
        direction_text=value("DIRECTION"), tx_type=value("TX_TYPE"),
        payer=payer, payee=payee, summary=f"{row.summary} {row.purpose}",
        account_no=meta.account_no, account_name=meta.account_name)
    if single_negative and direction in ("IN", "OUT"):
        direction = "OUT" if direction == "IN" else "IN"
        basis = basis + "（金额为负，方向已翻转）"
    row.direction = direction
    row.extra["direction_basis"] = basis
    if sign_note:
        row.extra["sign_note"] = sign_note

    # 对方是谁：**不是本户的那一方**。付款方是本户 → 对方是收款方；
    # 收款方是本户 → 对方是付款方。方向定下来之后才知道该取哪一边。
    if not row.counterparty_name:
        if direction == "OUT":
            row.counterparty_name = payee_name
            row.counterparty_account = value("PAYEE_ACCOUNT")
        elif direction == "IN":
            row.counterparty_name = payer_name
            row.counterparty_account = value("PAYER_ACCOUNT")
        else:
            row.counterparty_name = " ".join(filter(None, [payee_name, payer_name]))
            row.counterparty_account = " ".join(
                filter(None, [value("PAYEE_ACCOUNT"), value("PAYER_ACCOUNT")]))

    if row.tx_date is None:
        row.problems.append("没有日期")
    if row.amount is None:
        row.problems.append("没有金额")
    elif row.amount <= 0:
        row.problems.append("金额为 0")
    if row.direction == "UNKNOWN":
        row.problems.append("方向未知")
    return row


def fingerprint_of(container: str, header_cells: list[str]) -> str:
    """格式指纹：容器 + 归一化后的表头。

    用来认「这个文件是不是上次手工指定过列的那一类」。表头里的数字与
    空格都去掉，只留列名与顺序。
    """
    parts = [container]
    for cell in header_cells:
        parts.append(re.sub(r"[\s　]+", "", str(cell or "")))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------

def self_check(result: ParsedStatement) -> None:
    """自检的**公开入口**。

    **什么时候要重跑**：两路解析（文字层 + OCR）的结果合并之后 ——
    补上的字段可能让诊断变了样（方向补齐了，「有 N 行认不出收支方向」
    这条就该消失；合计补齐了，「文件自称收入合计 X，解析出 0」也该消失）。

    **重跑是安全的**：自检只换 ``diagnosis`` 里**自己那一段**
    （见 :attr:`ParsedStatement.check_notes_start`），解析期的结论与别人
    追加的诊断都原样保留。
    """
    _self_check(result)


def _self_check(result: ParsedStatement) -> None:
    """自检：把结论记进诊断的**自检段**。

    **为什么要分段**：自检是「拿解析结果跟文件自己报的数字对一遍」，
    而解析结果**会变**（两路合并会补上字段）。结论一变，诊断就得跟着变，
    否则界面上会一直挂着一条已经不成立的抱怨（「有 2 行认不出收支方向」
    —— 而方向早就补齐了）。所以自检必须**可重跑**，而且重跑时只能动
    自己那几条，不能把解析期的结论（「表头认不出」）一起清掉。
    """
    # 先把上一轮自检的结论摘掉（别人在自检之后追加的诊断原样保留）。
    end = min(result.check_notes_start + result.check_notes_count,
              len(result.diagnosis))
    extra = result.diagnosis[end:]
    result.diagnosis = result.diagnosis[:result.check_notes_start] + extra
    result.check_notes_start = len(result.diagnosis)
    result.check_notes_count = 0

    notes = _check_notes(result)
    result.diagnosis.extend(notes)
    result.check_notes_count = len(notes)


def _check_notes(result: ParsedStatement) -> list[str]:
    """跟文件自己报的总笔数、总金额对一遍 —— 结论清单。

    对不上**不拦下解析** —— 漏一行比多一行更难查，但它必须被说出来：
    页面上的「解析诊断」会写「文件自称 24 笔，解析出 22 笔」。
    """
    notes: list[str] = []
    meta = result.meta
    income_count = sum(1 for row in result.rows if row.direction == "IN")
    expense_count = sum(1 for row in result.rows if row.direction == "OUT")
    unknown_count = sum(1 for row in result.rows if row.direction == "UNKNOWN")

    if meta.declared_income_count is not None and meta.declared_income_count != income_count:
        notes.append(
            f"文件自称收入 {meta.declared_income_count} 笔，解析出 {income_count} 笔")
    if meta.declared_expense_count is not None and meta.declared_expense_count != expense_count:
        notes.append(
            f"文件自称支出 {meta.declared_expense_count} 笔，解析出 {expense_count} 笔")
    if meta.declared_income_total is not None and meta.declared_income_total != result.income_total:
        notes.append(
            f"文件自称收入合计 {meta.declared_income_total}，解析出 {result.income_total}")
    if meta.declared_expense_total is not None and meta.declared_expense_total != result.expense_total:
        notes.append(
            f"文件自称支出合计 {meta.declared_expense_total}，解析出 {result.expense_total}")
    if meta.declared_total_count is not None:
        # 文件只给总笔数时（兴业那种模板），拿它跟解析出的总行数对，
        # 差几笔就是漏了几行 —— 这正是「余额对不上」最常见的原因。
        total = income_count + expense_count
        if meta.declared_total_count != total:
            notes.append(
                f"文件自称共 {meta.declared_total_count} 笔，解析出 {total} 笔")
    if unknown_count:
        notes.append(f"有 {unknown_count} 行认不出收支方向，需要人工指定")

    # 余额连续性：上一行余额 ± 本行金额 = 本行余额
    broken = 0
    previous: StatementRow | None = None
    for row in result.rows:
        # **四个空值都要守卫** —— 少一个就是一次崩溃。
        #
        # ``previous.amount`` 曾经没被守卫：有一行的金额认不出来（``None``）
        # 而余额认得出来，下面的 ``descending`` 就拿 ``Decimal + None`` 去算，
        # 直接抛 ``TypeError``。而这行崩溃发生在**上传那一步**（自检是解析的
        # 最后一道），于是用户看到的是「导入失败」，文件本身却一点毛病没有。
        if previous is not None and previous.balance is not None \
                and previous.amount is not None \
                and row.balance is not None and row.amount is not None:
            # 有的银行**倒序**印流水（最新的在最上面），余额是往上走的。
            #
            # 两种排法都要认：
            #
            # - 正序：上一行余额 ± **本行**金额 = 本行余额；
            # - 倒序：本行余额 ± **上一行**金额 = 上一行余额
            #   （倒序时先发生的是下面那一行，余额是倒着长的）。
            ascending = min(
                abs(previous.balance + row.amount - row.balance),
                abs(previous.balance - row.amount - row.balance),
            )
            descending = min(
                abs(row.balance + previous.amount - previous.balance),
                abs(row.balance - previous.amount - previous.balance),
            )
            if min(ascending, descending) > Decimal("0.05"):
                broken += 1
        previous = row
    if broken:
        notes.append(f"有 {broken} 行的余额接不上前一行（可能漏了行或方向认错）")

    if not result.rows:
        notes.append("没有解析出任何流水行")
    return notes
