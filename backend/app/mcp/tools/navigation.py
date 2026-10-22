"""MCP 导航类工具：账套、期间、科目、上下文。

**这一块为什么存在。**
agent 回答「最近一笔租金是什么时候付出去的」，第一步不是查凭证，而是
**找到那本账、那一期、那个科目**。这一组工具只回答「去哪儿看」：有哪些
账套、记到哪个月了、「租金」对应哪个科目、这本账现在是什么状态。它们不碰
金额，因此也不碰账簿引擎与报表求值器。

**为什么导航类工具更要写死口径。**
最容易出错的不是金额，是**年度**。科目表每个会计年度各有一份（迁移 0032）：
2026 年的 1002 与 2027 年的 1002 是两行、两个 id。跨年 id 混用**不报错、
只会静默返回空** —— 那是最坏的一种错：agent 会据此回答「今年没有付过租金」。
所以这里凡涉及科目的出参都带着年份；不传年度时统一走 ``caliber.resolve_year``
（这个账套**当前在记的那一年**，不是服务器日历年），并在 ``notes[]`` 里写明
用的是哪一年。

**只读。**
本模块不写任何业务表，也不碰任何写路径（建凭证 / 结账 / 结转）。

**注解必须是真对象，不能是字符串。**
本模块**刻意不写** ``from __future__ import annotations``：MCP SDK 解析工具
注解时用的是**装饰器所在模块**（``base.py``）的全局命名空间，注解一旦变成
字符串就会解析到错的命名空间里去。
"""
from typing import Any, Optional

from mcp.server.mcpserver import Context

from app.accounting.initialization import init_states
from app.accounting.periods import current_accounting_year
from app.accounting.vouchers import query_vouchers
from app.accounting.years import subjects_of
from app.api.books import _years_of
from app.mcp.auth import accessible_books
from app.mcp.caliber import (CATEGORY_CN, all_periods, clamp_page, clamp_page_size,
                              current_period, is_cash_code, iso, known_years,
                              latest_closed_period, match_keyword, period_dict,
                              resolve_year, subject_dict)
from app.mcp.errors import McpBadRequest
from app.mcp.schemas import (
    AccountingContextResult,
    FindSubjectsResult,
    ListBooksResult,
    ListPeriodsResult,
)
from app.mcp.tools.base import (current_tool_context, envelope, mcp_tool,
                                require_book)

#: 方向的中文别名。agent 有时给「借方」「贷方」，收下它们比回一句
#: 「参数不合法」有用 —— 它要的只是把那一边筛出来。
_DIRECTION_ALIASES = {
    "借": "DEBIT", "借方": "DEBIT",
    "贷": "CREDIT", "贷方": "CREDIT",
}


# --------------------------------------------------------------------- 入参归一

def _normalize_category(value: Optional[str]) -> Optional[str]:
    """把 agent 给的科目类别归一到那 5 个枚举值。

    英文枚举与中文名都收（agent 有时给「资产类」），但**出参里的
    ``category`` 永远是英文枚举** —— 中文名另有 ``category_name`` 字段。
    归一失败要报错而不是忽略：静默忽略会让 agent 以为筛过了。
    """
    if value is None or value == "":
        return None
    raw = str(value).strip()
    upper = raw.upper()
    if upper in CATEGORY_CN:
        return upper
    for code, cn in CATEGORY_CN.items():
        if raw in (cn, cn + "类"):
            return code
    raise McpBadRequest(
        "科目类别只能是 " + "、".join(f"{code}（{cn}）" for code, cn in CATEGORY_CN.items())
        + f"，收到的是 {raw!r}")


def _normalize_direction(value: Optional[str]) -> Optional[str]:
    """把 agent 给的科目方向归一到 DEBIT / CREDIT。"""
    if value is None or value == "":
        return None
    raw = str(value).strip()
    upper = raw.upper()
    if upper in ("DEBIT", "CREDIT"):
        return upper
    if raw in _DIRECTION_ALIASES:
        return _DIRECTION_ALIASES[raw]
    raise McpBadRequest(
        f"科目方向只能是 DEBIT（借）或 CREDIT（贷），收到的是 {raw!r}")


# --------------------------------------------------------------------- 计数

def _voucher_counts(db, book_id: int, periods: list) -> tuple[dict, dict]:
    """每个期间的凭证数与已记账数（按期间 id 索引）。

    **用既有的凭证查询函数算，不自己拼 SQL。** 凭证数要跟凭证列表里看到的
    数字一致，而那份列表走的就是 ``accounting.vouchers.query_vouchers`` ——
    这里复用同一个函数，两处口径才不会漂。

    ``page_size=0`` 是故意的：只要计数、不要明细。明细那两次往返在这里
    纯属浪费（一个账套十几期，每期都要数一遍）。
    """
    totals: dict[int, int] = {}
    posted: dict[int, int] = {}
    for period in periods:
        _rows, total = query_vouchers(
            db, book_id, from_period=period, to_period=period, page=1, page_size=0)
        totals[period.id] = total
        _rows, posted_count = query_vouchers(
            db, book_id, from_period=period, to_period=period,
            status="POSTED", page=1, page_size=0)
        posted[period.id] = posted_count
    return totals, posted


def _carry_forward_period_ids(db, book_id: int) -> set[int]:
    """哪些期间里有损益结转凭证（按期间 id）。

    **判据与 ``accounting.organize.is_carry_forward`` 同一套**：source 标记
    + 摘要/备注里的结转短语。之所以在 SQL 侧做而不是逐张凭证跑一遍那个
    函数：``list_periods`` 要的是**每个期间**的标志，一张张判就是
    「凭证数 × 一次 Python 调用」。

    结转凭证不是可选信息 —— 它把损益余额搬到「本年利润」，多个工具的
    金额口径都受它影响（本次评估里「2025 年水电费」算错 48 万就是
    没剔它）。把它前置成显式字段，排查就不用靠猜。
    """
    from sqlalchemy import or_, select

    from app.accounting.organize import CARRY_FORWARD_MARKERS
    from app.models import Entry, Voucher

    clauses = [Voucher.source == "CARRY_FORWARD"]
    for marker in CARRY_FORWARD_MARKERS:
        clauses.append(Voucher.remark.like(f"%{marker}%"))
    stmt = (
        select(Voucher.period_id)
        .outerjoin(Entry, Entry.voucher_id == Voucher.id)
        .where(Voucher.book_id == book_id,
               or_(*clauses,
                   *[Entry.summary.like(f"%{m}%") for m in CARRY_FORWARD_MARKERS]))
        .distinct()
    )
    return {row[0] for row in db.execute(stmt).all()}


def _subject_counts_by_year(db, book_id: int) -> dict[int, int]:
    """各年度的科目表科目数。"""
    from sqlalchemy import func, select

    from app.models import Subject

    rows = db.execute(
        select(Subject.year, func.count(Subject.id))
        .where(Subject.book_id == book_id)
        .group_by(Subject.year)
    ).all()
    return {int(year): int(count) for year, count in rows}


def _year_voucher_counts(db, book_id: int, year_periods: list) -> tuple[int, int]:
    """某个年度的凭证总数与已记账数（一次区间查询算完）。

    区间查询而不是逐期相加：一条区间查询抵十二次单期查询，而口径与
    ``list_periods`` 里的逐期计数一致（同一个 ``query_vouchers``）。
    """
    if not year_periods:
        return 0, 0
    first, last = year_periods[0], year_periods[-1]
    _rows, total = query_vouchers(
        db, book_id, from_period=first, to_period=last, page=1, page_size=0)
    _rows, posted = query_vouchers(
        db, book_id, from_period=first, to_period=last,
        status="POSTED", page=1, page_size=0)
    return total, posted


# --------------------------------------------------------------------- 工具

@mcp_tool(
    name="list_books",
    description="列出当前令牌用户**有授权**的账套，以及每个账套的年度结账进度与初始化进度。"
                "years[] 里的 year 是会计年度、total 是该年的期间总数、closed 是已结账期数、"
                "vouchers 是凭证数、carried 表示该年是否已全部结完。",
)
def list_books(ctx: Context) -> ListBooksResult:
    """列出当前主体有授权的账套。"""
    tc = current_tool_context(ctx)
    db = tc.db
    # 可见性 100% 来自用户-账套授权表，不搞第二套「令牌账套范围」。
    rows = accessible_books(db, tc.principal)
    books = [book for book, _role in rows]
    # 年度进度与初始化状态都是「一批账套一次算完」的形状 —— 直接复用 HTTP
    # 层那两份现成算法，别在这儿再写一份（同一个口径两份实现迟早对不上）。
    years_by_book = _years_of(db, [book.id for book in books])
    inits = init_states(db, books)

    items: list[dict[str, Any]] = []
    for book, role in rows:
        years = years_by_book.get(book.id, [])
        items.append({
            "id": book.id,
            "name": book.name,
            "company_name": book.company_name,
            "role": role,
            "start_year": book.start_year,
            "latest_year": years[-1]["year"] if years else book.start_year,
            "years": years,
            "init": inits.get(book.id),
        })
    tc.count(len(items))
    return envelope(ListBooksResult, 
        caliber="navigation",
        notes=[
            "只列出当前令牌绑定用户被授权的账套；授权每次调用都重新查，"
            "用户那边撤授权后，下一次调用就看不到这本账了。",
            "years[] 是按年度的结账进度：total 是该年的期间总数、closed 是已结账期数、"
            "carried=true 表示该年已全部结完。",
            "init 是该账套当前在记那一年的初始化进度（两步：定科目、录期初）。",
            "latest_year 是账套最近一个有会计期间的年度；一本账还没有期间时退回启用年度。",
        ],
        items=items,
        total=len(items),
    )


@mcp_tool(
    name="list_periods",
    description="列出账套的**全部会计期间**（code 形如 2026-01），带每期的凭证数、"
                "已记账数与结账状态（locked）。已结账的期间照读不误。",
)
def list_periods(ctx: Context, book_id: int) -> ListPeriodsResult:
    """列出一个账套的全部会计期间。"""
    tc = current_tool_context(ctx)
    db = tc.db
    book = require_book(tc, book_id)
    periods = all_periods(db, book_id)
    totals, posted = _voucher_counts(db, book_id, periods)
    carry_periods = _carry_forward_period_ids(db, book_id)
    subject_counts = _subject_counts_by_year(db, book_id)
    items = []
    for period in periods:
        item = period_dict(period, vouchers=totals.get(period.id, 0),
                           posted=posted.get(period.id, 0))
        item["has_carry_forward"] = period.id in carry_periods
        item["subject_count"] = subject_counts.get(period.year, 0)
        items.append(item)
    tc.count(len(items))
    return envelope(ListPeriodsResult, 
        caliber="period",
        notes=[
            "期间按账套内连续序号 ord 排序；code 形如 2026-01。",
            "vouchers 是该期凭证总数，posted 是其中已记账的，unposted = vouchers - posted。",
            "locked=true 表示该期已结账（封账后不能再写凭证，但账照读不误）。",
            "has_carry_forward=true 表示该期有损益结转凭证 —— 它把损益余额搬到"
            "「本年利润」，聚合收入/费用时通常要排除（多个工具的金额口径"
            "都受它影响）。",
            "subject_count 是该期所属年度的科目表科目数。",
        ],
        book_id=book.id,
        items=items,
        total=len(items),
    )


@mcp_tool(
    name="find_subjects",
    description="按年度查科目表。keyword 同时模糊匹配**科目编码与科目名**"
                "（大小写不敏感），一次可命中多个（例：keyword='租赁' 命中「房屋租赁费」）。"
                "不传 year 时取该账套**当前在记的那一年**（不是服务器日历年）。"
                "同一编码在不同年度是两个科目、两个 id，跨年请显式指定 year。"
                "结果强制分页，page_size 上限 200。",
)
def find_subjects(ctx: Context, book_id: int, year: Optional[int] = None,
                  keyword: Optional[str] = None, category: Optional[str] = None,
                  direction: Optional[str] = None, only_leaf: bool = False,
                  active_only: bool = False, page: int = 1,
                  page_size: Optional[int] = None) -> FindSubjectsResult:
    """按年度查科目表，支持编码/名称模糊匹配与若干过滤。"""
    tc = current_tool_context(ctx)
    db = tc.db
    book = require_book(tc, book_id)
    # 年度必须显式化：不传就用这个账套当前在记的那一年（不是服务器日历年）。
    resolved_year = resolve_year(db, book, year)
    rows = subjects_of(db, book_id, resolved_year, include_inactive=True)
    code_by_id = {row.id: row.code for row in rows}
    want_category = _normalize_category(category)
    want_direction = _normalize_direction(direction)

    matched = []
    for row in rows:
        if not match_keyword(row, keyword):
            continue
        if want_category and row.category != want_category:
            continue
        if want_direction and row.direction != want_direction:
            continue
        if only_leaf and not row.is_leaf:
            continue
        if active_only and not row.active:
            continue
        matched.append(row)

    total = len(matched)
    current_page = clamp_page(page)
    size = clamp_page_size(page_size)
    start = (current_page - 1) * size
    window = matched[start:start + size]
    items = [
        subject_dict(row, parent_code=code_by_id.get(row.parent_id))
        for row in window
    ]
    # 审计记的是**命中数**而不是本页行数：agent 问「有哪些租赁科目」，
    # 审计要能看出它一次牵出了多少个候选，而不是只看到翻了一页。
    tc.count(total)

    notes = [
        f"科目表按 {resolved_year} 年度取"
        + ("（未指定年度，取的是该账套当前在记的那一年）" if year is None
           else "（按入参指定）"),
        "同一编码在不同年度是两行、两个 id；跨年请显式指定 year，"
        "混用不会报错，只会静默返回空。",
        "keyword 同时匹配科目编码与科目名（大小写不敏感），一次可命中多个科目。",
        f"共命中 {total} 个科目，本次返回第 {current_page} 页（每页最多 {size} 个）。",
    ]
    if not rows:
        notes.append(
            f"该账套 {resolved_year} 年还没有科目表；"
            f"现有科目表的年度是 {known_years(db, book_id) or '（无）'}。")
    hint = None
    if not matched:
        # **查空时要说清「你可能查错了年度」。**
        # 科目表每个会计年度各有一份，同一编码在不同年度是两个 id ——
        # 漏传 year（或传了另一年）会拿到空结果，而空结果看起来与
        # 「这个科目不存在」一模一样。
        other = []
        for y in known_years(db, book_id):
            if y == resolved_year:
                continue
            for row in subjects_of(db, book_id, y, include_inactive=True):
                if match_keyword(row, keyword):
                    other.append(y)
                    break
        if other:
            hint = (f"本次查的是 {resolved_year} 年度科目表，没有命中；"
                    f"但同样的关键字在 {'、'.join(str(y) for y in other)} 年的"
                    f"科目表里存在 —— 科目表每个会计年度各有一份，请显式指定 year。")

    return envelope(FindSubjectsResult, 
        caliber="subject",
        notes=notes,
        book_id=book.id,
        year=resolved_year,
        total=total,
        page=current_page,
        page_size=size,
        items=items,
        hint=hint,
    )


@mcp_tool(
    name="accounting_context",
    description="账套的「现在是什么情况」：当前在记的会计年度、当前期间、"
                "最近一期已结账的期间、当年的科目数与凭证数、现金类科目清单，"
                "以及这个账套有哪些年度的科目表。",
)
def accounting_context(ctx: Context, book_id: int) -> AccountingContextResult:
    """回答「这个账套现在是什么情况」。"""
    tc = current_tool_context(ctx)
    db = tc.db
    book = require_book(tc, book_id)
    periods = all_periods(db, book_id)
    periods_by_year: dict[int, list] = {}
    for period in periods:
        periods_by_year.setdefault(period.year, []).append(period)

    # 「有科目表」的年度 —— 期间是账套启用后自动铺的，科目表可能还没有。
    years = known_years(db, book_id)
    # 年度进度**直接复用 list_books 那一份**（``api/books.py::_years_of``）。
    #
    # 为什么要复用：年度进度是「期间总数 / 已结账期数 / 凭证数 / 是否已结完」
    # 这一组事实，HTTP 层与 MCP 层若各算一份，就是同一个口径两份实现 ——
    # 而这两个工具报的是**同一个事实**，字段名还不一样（一边 closed，一边
    # locked_periods），agent 对着看会以为它们是两回事。
    progress = {row["year"]: row
                for row in _years_of(db, [book_id]).get(book_id, [])}
    year_rows: list[dict[str, Any]] = []
    for year in years:
        subjects = subjects_of(db, book_id, year)
        row = dict(progress.get(year) or {
            "year": year, "total": 0, "closed": 0, "vouchers": 0,
            "carried": False,
        })
        # 本工具额外多给一个科目数 —— agent 判断「这一年的账能不能查」
        # 需要知道这一年的科目表里有多少个科目。
        row["subject_count"] = len(subjects)
        # **记账进度**：「哪些年没记账」是本次评估里最关键、也最难自己
        # 拼出来的信息 —— 以前要逐期读 list_periods 的几十行才拼得出
        # 「2022–2025 已记账、2026 未记账」这个结论。
        y_total, y_posted = _year_voucher_counts(
            db, book_id, periods_by_year.get(year, []))
        row["posted_vouchers"] = y_posted
        row["unposted_vouchers"] = max(0, y_total - y_posted)
        year_rows.append(row)

    current_year = current_accounting_year(db, book)
    current_subjects = subjects_of(db, book_id, current_year)
    current_vouchers, _posted = _year_voucher_counts(
        db, book_id, periods_by_year.get(current_year, []))

    now = current_period(db, book_id)
    now_out = None
    if now is not None:
        now_out = {"code": now.code, "id": now.id, "year": now.year,
                   "month": now.month, "locked": bool(now.locked)}

    closed = latest_closed_period(db, book_id)
    closed_out = None
    if closed is not None:
        closed_out = {"code": closed.code, "id": closed.id,
                      "locked": bool(closed.locked), "closed_at": iso(closed.closed_at)}

    cash_subjects = [
        {"id": s.id, "code": s.code, "name": s.name, "year": s.year}
        for s in current_subjects if is_cash_code(s.code)
    ]

    # 出参是一个快照，不是一组行，所以「读了多少」要自己说：记当前年度的
    # 科目数（现金科目只是它的子集，拿子集当读量是误导）。
    tc.count(len(current_subjects))
    return envelope(AccountingContextResult, 
        caliber="context",
        notes=[
            f"「今年」指这个账套当前在记的那一个会计年度（{current_year}），"
            "不是服务器日历年 —— 账是账套的属性。",
            "current_period 是当前在记的那一期；账上一张凭证都没有时取最后一个期间，"
            "所以「还没有凭证」不等于「没有当前期」。",
            "latest_closed_period 是最近一期已结账的期间；还没结过账时为 null。",
            "cash_subjects 只含现金类科目（编码前缀 1001 库存现金 / 1002 银行存款 / "
            "1012 其他货币资金），取的是当前年度的科目表。",
            "years[] 里 posted_vouchers / unposted_vouchers 给出**记账进度**："
            "unposted_vouchers > 0 ⇒ 这一年的账还没记完，任何只读已记账的"
            "工具在它上面看到的都是「已记账的那一部分」。",
            "years[] 只列**有科目表**的年度；每行的年度进度（total 期间总数、"
            "closed 已结账期数、vouchers 凭证数、carried 是否已全部结完）与 "
            "list_books 的年度进度**同一口径**（同一份实现算出来的）；"
            "subject_count 是该年的科目数。",
        ],
        book={
            "id": book.id,
            "name": book.name,
            "company_name": book.company_name,
            "start_year": book.start_year,
            "start_month": book.start_month,
            "subject_code_rule": book.subject_code_rule,
        },
        years=year_rows,
        current_accounting_year=current_year,
        current_period=now_out,
        latest_closed_period=closed_out,
        subject_count=len(current_subjects),
        voucher_count=current_vouchers,
        cash_subjects=cash_subjects,
    )


def register(server) -> None:
    """把本模块的工具注册到 MCP 服务上。"""
    for fn in (list_books, list_periods, find_subjects, accounting_context):
        server.add_tool(fn, name=fn.__name__, description=fn.__doc__ or "")


__all__ = [
    "accounting_context", "find_subjects", "list_books", "list_periods",
    "register",
]
