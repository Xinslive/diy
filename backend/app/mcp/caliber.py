"""MCP 层的**口径集中地**。

这一整个模块存在的理由只有一条：**MCP 侧不自己写第二套聚合**。

余额、发生额、损益、现金类判定、方向判定、年度与期间解析 —— 全部委托给
既有的引擎与报表求值器（``app/ledger/*``、``app/reports/evaluate.py``、
``app/accounting/*``）。本仓的历史审计第 7.3 节记过一次「导出与页面显示
对不上」，根因是同一个口径有了两份实现。MCP 若再写一份，它就是第三份。

所以这里的函数**只做三件事**：

1. 把 agent 传来的字符串（``"2026-10"`` / ``"560207"`` / ``"租金"``）
   翻译成既有服务能吃的对象；
2. 把既有服务抛的 ``HTTPException`` 翻译成 MCP 错误；
3. 把「取数口径」的**差异**显式写进出参（``caliber`` / ``notes``），
   让 agent 知道自己手里的数是哪个口径下的。

**年度一律显式化。** 科目表每个会计年度各有一份（迁移 0032），2026 的 1002
与 2027 的 1002 是两行、两个 id。跨年 id 混用**不会报错，只会静默返回空**
—— 那是最坏的一种错。所以「不传年度」时用 ``current_accounting_year``
（**不是**服务器日历年：连接池把会话时区钉死成 UTC），并在出参里标注用的是
哪一年。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.organize import CASH_SUBJECT_PREFIXES, cash_flow_kind
from app.core.config import settings
from app.models import Book, Period, Subject, Voucher

#: 科目类别的中文映射。**以 ``api/subjects.py`` 为准** —— 那是界面上显示的
#: 那一版。``printing/preview.py`` 与 ``accounting/subject_importer.py`` 里
#: 各有一份不一致的副本；MCP 用界面的口径，agent 看到的词与用户看到的词
#: 才是一致的。
CATEGORY_CN = {
    "ASSET": "资产",
    "LIABILITY": "负债",
    "EQUITY": "权益",
    "COST": "成本",
    "PROFIT": "损益",
}

#: 现金类科目编码前缀。与 ``accounting/organize.py`` 是**同一份常量**，
#: 不是复制 —— 「真付出去」这个判据全仓只能有一处。
CASH_PREFIXES = CASH_SUBJECT_PREFIXES

ZERO = Decimal("0")


# --------------------------------------------------------------------- 金额

def money_str(value) -> str:
    """金额 → 字符串（两位小数，单位元）。

    **为什么用字符串而不是数字**：JSON 里的浮点数是二进制近似，
    ``Decimal("12800.00")`` 转成 float 再转回字符串会得到 ``12800.0`` 甚至
    ``12800.000000000002``。agent 拿到的数要能和系统界面**逐字**对上，
    所以这里出参一律用字符串承载金额，并带 ``currency: "CNY"``。

    负数是**红字**（冲销），照原样保留符号 —— 把它改成正数就丢了信息。
    """
    if value is None:
        return "0.00"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value.quantize(Decimal('0.01')):f}"


def money_value(value) -> Decimal:
    """把出参里的金额字符串变回 Decimal（给内部计算用）。"""
    if value is None:
        return ZERO
    return Decimal(str(value))


def is_cash_code(code: str | None) -> bool:
    """这个科目是不是现金类（库存现金 / 银行存款 / 其他货币资金）。"""
    return (code or "").startswith(CASH_PREFIXES)


def cash_flow_kind_of(entries, income_of=None) -> str:
    """收/付/转账判定 → 英文标识。

    委托 ``accounting/organize.cash_flow_kind``（官方口径，按现金类科目
    **净额**判方向），这里只做一层命名映射。MCP 不自己判「钱往哪边走」。
    """
    kind = cash_flow_kind(entries, income_of)
    return {0: "RECEIPT", 1: "PAYMENT", 2: "TRANSFER"}.get(kind, "TRANSFER")


# --------------------------------------------------------------------- 年度

def resolve_year(db: Session, book: Book, year: Optional[int]) -> int:
    """把「用户指的年度」解析成一个具体的年度。

    没指定时用**这个账套当前在记的那一年**（最近一期有凭证的期间所在的
    年度，一张都没有时退回账套启用年度）—— 与
    ``accounting.periods.current_accounting_year`` 同一个口径。

    **不用服务器日历年**：本项目把数据库连接的会话时区钉死成 UTC，于是
    每年 1 月 1 日的 8 小时窗口里北京时间已经跨年、UTC 还没跨年；而一本
    2026 年建的账补录 2025 年的分录时，「本年」算的也应该是 2026。
    """
    from app.accounting.periods import current_accounting_year

    if year is not None:
        return int(year)
    return int(current_accounting_year(db, book))


def known_years(db: Session, book_id: int) -> list[int]:
    """这个账套**有科目表**的年度（倒序）。

    「有年度」的口径是**有科目表行**，不是有期间行 —— 期间是账套启用后
    自动铺的，而科目表可能还没有（初始化没做完）。agent 问「有哪些年」时
    拿到的是「哪些年的账能查」。
    """
    rows = db.scalars(
        select(Subject.year).where(Subject.book_id == book_id).distinct()
    ).all()
    return sorted({int(y) for y in rows}, reverse=True)


# --------------------------------------------------------------------- 期间

def all_periods(db: Session, book_id: int) -> list[Period]:
    return list(
        db.scalars(
            select(Period).where(Period.book_id == book_id).order_by(Period.ord)
        ).all()
    )


def find_period_by_code(db: Session, book_id: int, code: str) -> Period:
    """按 ``YYYY-MM`` 取期间。

    取不到时**说清楚原因**：「这个账套从 X 月起启用」比「期间不存在」
    有用得多 —— agent 问 2026-01 而账套 2026-03 才启用，它需要知道是
    「没这个期间」还是「问错了年份」。

    **按 year/month 查，不按 ``code`` 查。**
    ``Period.code`` 是模型上的一个 **Python property**（``f"{year}-{month:02d}"``），
    **不是数据库列** —— 拿它拼 where 子句不会报错，只会永远查不到
    （class 级取到的是 property 对象，与字符串比较恒为假）。
    """
    code = (code or "").strip()
    year, month = _split_period_code(code)
    period = db.scalar(
        select(Period).where(
            Period.book_id == book_id,
            Period.year == year,
            Period.month == month,
        )
    )
    if period is not None:
        return period
    periods = all_periods(db, book_id)
    if not periods:
        raise McpPeriodError(
            f"账套还没有任何会计期间，无法定位 {code}")
    first, last = periods[0], periods[-1]
    if code < first.code:
        raise McpPeriodError(
            f"该账套从 {first.code} 起才有会计期间，{code} 在启用之前")
    if code > last.code:
        raise McpPeriodError(
            f"该账套的最后一个会计期间是 {last.code}，{code} 还没有到")
    raise McpPeriodError(f"期间 {code} 不在该账套的会计期间范围内")


def _split_period_code(code: str) -> tuple[int, int]:
    """``YYYY-MM`` → ``(year, month)``。格式不对就报 400。"""
    parts = (code or "").split("-")
    if len(parts) != 2:
        raise McpPeriodError(
            f"期间格式应为 YYYY-MM，收到的是 {code!r}")
    try:
        year = int(parts[0])
        month = int(parts[1])
    except (TypeError, ValueError):
        raise McpPeriodError(
            f"期间格式应为 YYYY-MM，收到的是 {code!r}")
    if not 1 <= month <= 12:
        raise McpPeriodError(f"月份 {month} 不合法（期间格式为 YYYY-MM）")
    return year, month


def current_period(db: Session, book_id: int) -> Optional[Period]:
    """「当前在记的那一期」。

    口径：**最近一期有凭证的期间**；一张凭证都没有时退回**最后一个期间**
    （账套刚建好、还没记第一张凭证时，agent 问「现在到哪个月了」，
    答「最后一个月」比答「没有」有用）。
    """
    period_id = db.scalar(
        select(Voucher.period_id)
        .join(Period, Period.id == Voucher.period_id)
        .where(Voucher.book_id == book_id)
        .order_by(Period.ord.desc())
        .limit(1)
    )
    if period_id is not None:
        period = db.get(Period, period_id)
        if period is not None:
            return period
    periods = all_periods(db, book_id)
    return periods[-1] if periods else None


def latest_closed_period(db: Session, book_id: int) -> Optional[Period]:
    """最近一期**已结账**（locked）的期间。"""
    return db.scalar(
        select(Period).where(Period.book_id == book_id, Period.locked.is_(True))
        .order_by(Period.ord.desc()).limit(1)
    )


def months_between(from_period: Period, to_period: Period) -> int:
    """两个期间之间跨多少个月（含首尾）。"""
    return abs(to_period.ord - from_period.ord) + 1


def check_range(from_period: Period, to_period: Period) -> None:
    """区间上限闸。

    区间上限是 ``mcp_max_range_months``。它的作用是**防止 agent 一次把整本
    账拉走** —— 不是性能问题（分页已经限住了），是「一个令牌被拿去批量
    导出数据」这件事本身。

    **这一项只约束「跨期查询」，不约束「按年查询」（审查 L-27）。**
    ``search_vouchers`` 先用 ``year`` 把窗口夹成该年度（最多 12 个月），
    再调这里 —— 所以在那个工具上这条闸**永远不会触发**。这是**有意的**，
    不是漏洞：按年查询的窗口本来就比 60 个月更窄（12 < 60），也就是说
    它**比这条闸更严**。要把它「变成会触发」而靠放宽上限，是把一条保护
    拆了。

    所以各工具的口径是：

    * **跨期查询**（``latest_payment`` 那种可以跨年的窗口）→ 这条闸真的会
      触发，超过 ``mcp_max_range_months`` 就报错；
    * **按年查询**（``search_vouchers``）→ 窗口已被年度夹住，比这条闸更严；
      工具的 ``notes`` 里会说明这一点（免得 agent 以为这条闸没生效）。
    """
    limit = settings.mcp_max_range_months
    span = months_between(from_period, to_period)
    if span > limit:
        raise McpPeriodError(
            f"查询区间跨 {span} 个月，超过单次上限 {limit} 个月；"
            "请拆成多次查询")
    if from_period.ord > to_period.ord:
        raise McpPeriodError("起始期间不能晚于结束期间")


def check_enum(value: Optional[str], allowed: tuple[str, ...], param: str) -> str:
    """枚举参数校验：非法值直接回**列出合法取值**的参数错误。

    **为什么不能让它落到服务端异常。** 非法枚举值以前是抛异常，异常又被
    MCP 的协议层翻成 ``-32602``（"结构不匹配"）—— 调用方看到的是一句跟
    「你传错了粒度」毫无关系的话，只能猜。**枚举参数传错是调用方的错，
    而调用方的错必须用一句「这里只支持 A / B / C」回给他**，他才能改。

    大小写与空白一律归一化（``" Month "`` 也算 ``month``）—— agent 传参
    不会特意对齐大小写，而大小写不敏感比报错更友好。
    """
    from app.mcp.errors import McpBadRequest

    text = (value or "").strip().lower()
    if text not in allowed:
        raise McpBadRequest(
            f"{param} 只支持 {'、'.join(allowed)}；收到的是 {value!r}。")
    return text


def clamp_page_size(page_size: Optional[int]) -> int:
    """把页长夹到 ``[1, mcp_max_page_size]``。

    **夹而不是报错**：agent 传一个大于上限的页长，是在说「我要更多」，
    而不是说「我做错了什么」。回一个夹过的页长 + 明确的 ``total``，
    它能自己继续翻页；回一个错误，它多半会直接把整个调用当失败。
    """
    limit = max(1, int(settings.mcp_max_page_size))
    if page_size is None:
        return min(50, limit)
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return min(50, limit)
    return max(1, min(value, limit))


def clamp_page(page: Optional[int]) -> int:
    try:
        value = int(page or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


# --------------------------------------------------------------------- 科目

def subject_dict(subject: Subject, *, parent_code: Optional[str] = None) -> dict:
    """一个科目的出参形状。**字段名英文、描述中文**（与出参规范一致）。"""
    return {
        "id": subject.id,
        "code": subject.code,
        "name": subject.name,
        "category": subject.category,
        "category_name": CATEGORY_CN.get(subject.category or "", subject.category),
        "direction": subject.direction,
        "level": subject.level,
        "is_leaf": bool(subject.is_leaf),
        "active": bool(subject.active),
        "year": subject.year,
        "parent_code": parent_code,
        "is_cash": is_cash_code(subject.code),
    }


def match_keyword(subject: Subject, keyword: str) -> bool:
    """科目名的模糊匹配。

    匹配 **编码 + 名称**，大小写不敏感。真实账套的科目名可能是
    「房屋租赁费」「租金」「租赁费」 variously，agent 只会给一个词。
    """
    if not keyword:
        return True
    kw = keyword.strip().lower()
    if not kw:
        return True
    return kw in (subject.name or "").lower() or kw in (subject.code or "").lower()


def build_income_lookup(db: Session, book_id: int):
    """「这个科目是不是收入方」的判定函数。

    直接复用 ``accounting/organize.build_income_lookup``（按**顶层科目名**
    白名单 ``收入/收益/补偿`` 判定）—— 不自己写一份。
    """
    from app.accounting.organize import build_income_lookup as _build

    return _build(db, book_id)


# --------------------------------------------------------------------- 方向

def orient(signed: Decimal, *, credit_natural: bool) -> Decimal:
    """按科目自然方向取正数：收入类（自然方向贷）取正。

    与 ``reports/evaluate.py`` 的 ``_orient`` **同一个规则**：
    ``subject.direction == "CREDIT"`` ⇒ ``-signed``。

    注意这与「借/贷展示」是两件事：展示方向由 ``ledger.engine.display_side``
    按**符号**判定（早期版本按科目自然方向判，导致「短期借款 −1000 显示成
    借 1000」）。本模块不复刻那个缺陷。
    """
    return -signed if credit_natural else signed


# --------------------------------------------------------------------- 日期

def iso(value) -> Optional[str]:
    """日期 → ``YYYY-MM-DD``。"""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def parse_date(value: Optional[str]) -> Optional[date]:
    """``YYYY-MM-DD`` → date。解析不了就抛 400。"""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        raise McpPeriodError(f"日期 {value!r} 不是 YYYY-MM-DD 格式")


def parse_year(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise McpPeriodError(f"年度 {value!r} 不是整数")


# --------------------------------------------------------------------- 错误

class McpPeriodError(Exception):
    """期间/年度层面的输入问题。

    单独一个异常类型而不是直接用 ``McpBadRequest``：这样工具层可以
    把它**统一**映射成 400 + 可读文案，而不必在每个工具里各写一遍
    try/except。
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_error(self):
        from app.mcp.errors import McpBadRequest

        return McpBadRequest(self.message)


def period_dict(period: Period, *, vouchers: int = 0, posted: int = 0) -> dict:
    """一个期间的出参形状。带 ``locked`` / ``closed_at`` —— agent 需要能说
    「该月已封账」。"""
    return {
        "id": period.id,
        "code": period.code,
        "year": period.year,
        "month": period.month,
        "ord": period.ord,
        "locked": bool(period.locked),
        "closed_at": iso(period.closed_at),
        "closed_by": period.closed_by,
        "vouchers": vouchers,
        "posted": posted,
        "unposted": max(0, vouchers - posted),
    }


__all__ = [
    "CASH_PREFIXES", "CATEGORY_CN", "McpPeriodError", "build_income_lookup",
    "cash_flow_kind_of", "check_range", "clamp_page", "clamp_page_size",
    "current_period", "find_period_by_code", "is_cash_code",
    "iso", "known_years", "latest_closed_period", "match_keyword",
    "money_str", "money_value", "months_between", "orient", "parse_date",
    "parse_year", "period_dict", "resolve_year", "subject_dict",
]
