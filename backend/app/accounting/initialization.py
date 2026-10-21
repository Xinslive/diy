"""账套初始化：两步的**状态**与**闸门**，**按年度**。

初始化被显式拆成有先后的两步：

    1. 导入科目（或确认沿用系统装好的标准科目表）
    2. 导入期初余额

**为什么顺序不能反。** 期初余额是挂在科目上的：账上一张科目表都没有时，
录进来的数挂不到任何科目上，只能整批丢掉，而且用户看不出来 ——
界面照样显示「已读入 N 条」，库里的科目却一个都不匹配。
用户自己看不出这个先后关系（科目表和期初余额是两个不相干的页面），
所以状态记在**年度**上（``book_year.init_*``，迁移 0032），由接口层拦。

**为什么状态必须是年度级而不是账套级。** 每个年度有**自己的**科目表，
也就各有各的「第 1 步做没做」。2026 年导入了科目表、2027 年还没导 ——
账套级的状态会把 2027 一起标成「已完成」，于是 2027 年可以在**一张科目
都没有**的情况下录期初、记账，期初整批挂空。

**第 1 步有两条路，都算数。** 一是从用友等外部系统导入科目表，
二是确认沿用建账时系统装好的标准科目表。只认第一条会把
「就用标准科目表从零开始记账」的用户挡在门外 —— 建账本来就自动装标准表，
对这类用户来说第 1 步等于「确认一下」。

**为什么是服务层而不是路由里现算。** 同一个判断要在三处用到：
期初解析、期初保存、账套列表/详情/初始化接口下发。散着写三遍，
总有一处会漏 —— 而漏掉的那一处恰好就是用户绕过限制的入口。

**年度解析口径。** 任何一个要问「哪一年」的入口，都用
``app.accounting.years.resolve_year`` 解析：给了年度就用，没给就用
**这个账套当前在记的那一年**。不统一的话，「账套列表说能导入、
点进去说不能」这种不一致迟早出现 —— 而这次它出过一次更糟的：
闸门是**账套级**的，于是 2026 年记了账，2027 年一张凭证都没有也照样
被拦下来（见 ``subject_import_state`` 的 docstring）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import Book, BookYear, Entry, Period, PeriodOpening, Subject, Voucher

# 拒绝时给用户看的整句话。放在常量里而不是各路由各拼一遍：
# 界面弹窗、错误提示、接口文案说的是同一件事就该有一份措辞。
BLOCKED_HINT = "请先完成初始化第 1 步：导入科目，或确认沿用系统标准科目表"

# 第 2 步的两种来源。IMPORT = 导入过余额表；NONE = 本期本来就没有期初余额。
# 分开记是因为这两种「第 2 步已完成」在账面上的含义完全相反：一种账上有数，
# 一种账上一个数都没有，事后翻审计日志必须分得清。
OPENINGS_SOURCE_IMPORT = "IMPORT"
OPENINGS_SOURCE_NONE = "NONE"
# CARRY = 期初是**年度结转**从上一年度搬过来的（迁移 0032 之后新增的第三种）。
#
# 为什么单列一种：结转写下的期初与「导入余额表」写下的期初，来源完全不同 ——
# 前者是系统算出来的（上一年的期末），后者是人给的数据。事后要查
# 「这年的期初到底是搬过来的还是录进来的」，两种不能长得一模一样。
OPENINGS_SOURCE_CARRY = "CARRY"


def subject_import_state(db: Session, book_id: int, year: int) -> dict:
    """**这个年度**现在能不能覆盖导入科目表，以及挡在中间的各有多少数据。

    从 ``api/subjects.py`` 搬到这里：初始化状态也要用到同一份计数
    （能不能导入、还剩多少科目），两边各查一遍的话，
    「账套列表说能导入、点进去说不能」这种不一致迟早出现。

    ---- 为什么必须按年度统计 --------------------------------------------
    这份计数以前是**账套级**的（整个账套的凭证/分录/期初一起数），
    于是 2026 年记了账之后，2027 年那张一张凭证都没有的账也被判成
    「已有业务数据」，覆盖导入科目表被拒 —— 而用户要的正是
    「每个年度有自己的科目表」。

    闸门按账套统计并不是作者忘了加年，而是**当时科目表本身就是账套级的**：
    覆盖导入执行的是「删掉账套的全部科目再重建」，而那些科目正是
    2026 年在用的。只把闸门改成按年、放行 2027 的导入，会当场删掉
    2026 的科目表，2026 的凭证全部指向被删的科目（``entry.subject_id``
    外键）—— 账直接烂掉。

    科目表下沉到年度之后，「删本年度的科目、只数本年度的业务数据」才
    同时成立：**别的年度的科目一行都不动。**

    ---- 期初为什么从「挡路」变成「搬家」 --------------------------------
    闸门以前把期初也算作「挡路的业务数据」。那是因为期初挂在科目上，
    删科目就删期初（``period_opening.subject_id`` 是 RESTRICT 外键，
    被引用的科目删不掉，导入直接失败）—— 除了拒绝没有别的办法。

    现在不一样了：期初快照的归属是**编码**的事。覆盖导入只动本年度的科目，
    本年度的期初按**编码**搬到新科目上即可（见 ``years.detach_openings`` /
    ``years.attach_openings``）。搬不动的（编码在新表里没有、余额非零）
    在导入时拒绝并列出差异 —— 宁可报错，也不让钱凭空消失。
    """
    subject_count = db.scalar(
        select(func.count()).select_from(Subject).where(
            Subject.book_id == book_id, Subject.year == year)
    ) or 0
    voucher_count = db.scalar(
        select(func.count()).select_from(Voucher)
        .join(Period, Period.id == Voucher.period_id)
        .where(Period.book_id == book_id, Period.year == year)
    ) or 0
    entry_count = db.scalar(
        select(func.count()).select_from(Entry)
        .join(Period, and_period_ord(Entry, Period))
        .where(Period.book_id == book_id, Period.year == year)
    ) or 0
    opening_count = db.scalar(
        select(func.count()).select_from(PeriodOpening)
        .join(Period, Period.id == PeriodOpening.period_id)
        .where(Period.book_id == book_id, Period.year == year)
    ) or 0
    # **期初不再硬挡，但要按编码搬家。**
    #
    # 以前期初也挡：期初挂着科目，科目要删，期初就没地方挂。旧模型里
    # 「删整个账套的科目」确实会连带删掉期初（RESTRICT 外键挡着，于是导入
    # 直接失败）—— 那时除了拒绝没有别的办法。
    #
    # 现在科目表每个年度各有一份，覆盖导入只动**本年度**的科目，
    # 而期初快照的归属是**编码**的事，与它挂在哪一行科目上无关：
    # 1002 的期初就是 1002 的期初，哪怕新表里 1002 换了另一个 id。
    # 所以期初可以**按编码搬到新科目表上**，不必拦。
    #
    # 搬不动的怎么办：编码在新表里没有、而余额非零 → 导入时**拒绝**并列出
    # 差异（fail-closed）。静默丢弃的后果是「钱没了，而借贷仍然平衡，
    # 系统自检不会报错」。
    #
    # 凭证与分录仍然硬挡：它们按 ``entry.subject_id`` 指着具体的科目行，
    # 换表 = 换 id，历史凭证就指空了，而期初快照是唯一能按编码搬的东西。
    return {
        "year": year,
        "subject_count": subject_count,
        "voucher_count": voucher_count,
        "entry_count": entry_count,
        "opening_count": opening_count,
        # 有期初时导入会把期初按编码搬到新科目表上（``opening_remap``）。
        "opening_remap": opening_count > 0,
        "can_overwrite": voucher_count == 0 and entry_count == 0,
    }


def and_period_ord(entry, period):
    """``entry`` 的冗余 ``(book_id, period_ord)`` 与 ``period`` 对上的连接条件。

    分录上挂的是「账套 + 连续期间序号」，不是 period_id —— 那是
    ``entry`` 的反规范化设计（``models/accounting.py``）。按 period_id 连
    会把分录全部漏掉，统计出来是 0，而页面上明明有一堆凭证。
    """
    return and_(
        period.book_id == entry.book_id,
        period.ord == entry.period_ord,
    )


def _current_years(db: Session, books: list[Book]) -> dict[int, int]:
    """一批账套各自「当前在记的那一年」。

    口径与 ``accounting.periods.current_accounting_year`` 一致：最近一期
    有凭证的期间所在的年度；一张凭证都没有时退回账套的启用年度。
    """
    if not books:
        return {}
    ids = [b.id for b in books]
    rows = db.execute(
        select(Period.book_id, func.max(Period.year))
        .join(Voucher, Voucher.period_id == Period.id)
        .where(Period.book_id.in_(ids))
        .group_by(Period.book_id)
    ).all()
    active = {bid: int(y) for bid, y in rows if y is not None}
    return {b.id: active.get(b.id, int(b.start_year)) for b in books}


def init_states(db: Session, books: list[Book],
                years: Optional[dict[int, int]] = None) -> dict[int, dict[str, Any]]:
    """一批账套的初始化状态，**几条计数查询打完所有账套**。

    账套列表要一次给每个账套挂一个「待初始化」标记。逐个算就是
    「每个账套 4 条 count」—— 十个账套四十次往返，而列表本身
    已经在按账套逐个查年度了。几个 group by 一次拿全，与账套数无关。

    **按年度算。** 每个账套报的是它**当前在记的那一年**的进度 ——
    2026 年初始化完了、2027 年还没初始化时，账套卡片上要提示的是
    2027 年没做完，不是「这套账初始化完了」。
    """
    if not books:
        return {}
    ids = [b.id for b in books]
    year_of = years if years is not None else _current_years(db, books)
    wanted = set(year_of.values())
    if not wanted:
        return {}

    def grouped(stmt) -> dict[tuple[int, int], int]:
        return {(bid, y): n for bid, y, n in db.execute(stmt).all()}

    subjects = grouped(
        select(Subject.book_id, Subject.year, func.count())
        .where(Subject.book_id.in_(ids), Subject.year.in_(wanted))
        .group_by(Subject.book_id, Subject.year)
    )
    vouchers = grouped(
        select(Period.book_id, Period.year, func.count(Voucher.id))
        .join(Voucher, Voucher.period_id == Period.id)
        .where(Period.book_id.in_(ids), Period.year.in_(wanted))
        .group_by(Period.book_id, Period.year)
    )
    entries = grouped(
        select(Period.book_id, Period.year, func.count(Entry.id))
        .join(Entry, and_period_ord(Entry, Period))
        .where(Period.book_id.in_(ids), Period.year.in_(wanted))
        .group_by(Period.book_id, Period.year)
    )
    openings = grouped(
        select(Period.book_id, Period.year, func.count(PeriodOpening.id))
        .join(PeriodOpening, PeriodOpening.period_id == Period.id)
        .where(Period.book_id.in_(ids), Period.year.in_(wanted))
        .group_by(Period.book_id, Period.year)
    )
    return {
        b.id: _shape(
            db.scalar(select(BookYear).where(
                BookYear.book_id == b.id, BookYear.year == year_of[b.id])),
            year_of[b.id],
            subjects.get((b.id, year_of[b.id]), 0),
            vouchers.get((b.id, year_of[b.id]), 0),
            entries.get((b.id, year_of[b.id]), 0),
            openings.get((b.id, year_of[b.id]), 0),
        )
        for b in books
    }


def _shape(book_year: Optional[BookYear], year: int, subjects: int, vouchers: int,
           entries: int, openings: int) -> dict[str, Any]:
    subjects_ready = book_year is not None and book_year.init_subjects_at is not None
    openings_ready = book_year is not None and book_year.init_openings_at is not None
    if subjects_ready and openings_ready:
        step = 0          # 两步都走完了
    elif subjects_ready:
        step = 2
    else:
        step = 1
    return {
        "book_id": book_year.book_id if book_year is not None else None,
        "year": year,
        "subjects_ready": subjects_ready,
        "subjects_source": book_year.init_subjects_source if subjects_ready else None,
        "subjects_at": _iso(book_year.init_subjects_at) if subjects_ready else None,
        "subject_count": subjects,
        "openings_ready": openings_ready,
        "openings_source": book_year.init_openings_source if openings_ready else None,
        "openings_at": _iso(book_year.init_openings_at) if openings_ready else None,
        "opening_count": openings,
        "step": step,
        # 「沿用系统标准科目表」只在账套确实装着科目时才可点 ——
        # 一个科目都没有的年度点它等于什么都没确认。
        "can_keep_chart": subjects > 0,
        # 与 ``subject_import_state`` 的 ``can_overwrite`` **同一个口径**：
        # 两边各算一次的话，「向导说不能导入、科目表页却能导」这种不一致
        # 迟早出现（这句话不是假设 —— 那正是 ``_subject_import_state``
        # 搬进本模块的原因）。
        #
        # 期初不算「挡路」：它们会按**编码**搬到新科目表上。
        "can_import_subjects": vouchers == 0 and entries == 0,
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def init_state(db: Session, book: Book, year: Optional[int] = None) -> dict[str, Any]:
    """某个年度的初始化进度。下发给初始化向导、账套卡片和期初余额页的是同一份。"""
    from app.accounting.years import resolve_year

    resolved = resolve_year(db, book, year)
    return init_states(db, [book], {book.id: resolved})[book.id]


def blocked_reason(book_year: BookYear) -> Optional[str]:
    """第 1 步没做完时给出的拦路话术；做完了返回 ``None``。"""
    if book_year.init_subjects_at is not None:
        return None
    return (
        f"{book_year.year} 年度还没完成初始化第 1 步（确定科目），"
        f"不能导入或保存期初余额。{BLOCKED_HINT}"
    )


def mark_subjects_ready(db: Session, book_year: BookYear, source: str) -> bool:
    """记下「第 1 步已完成」。返回状态是否**这次**才发生变化。

    幂等是有意义的：用户可能反复点「沿用标准科目表」，
    每次都重写时间戳的话，审计日志里就会留下一串看不出差别的记录，
    「这一步是什么时候完成的」也就没了。所以只在第一次落地时写时间戳。
    """
    if book_year.init_subjects_at is not None:
        return False
    now = datetime.now(timezone.utc)
    book_year.init_subjects_at = now
    book_year.init_subjects_source = source
    db.flush()
    return True


def mark_openings_ready(db: Session, book_year: BookYear,
                        source: str = OPENINGS_SOURCE_IMPORT) -> bool:
    """第 2 步完成时记下时间与来源。返回状态是否**这次**才发生变化。

    ``source`` 两种：``IMPORT``（真的写进了期初余额）与 ``NONE``（本期本来就没有）。
    只记时间不记来源的话，这两种在库里长得一模一样 —— 而事后要查的恰恰
    是「当时账上到底有没有数」。

    口径是**存进库了**，不是「页面上填了数」—— 预览里读进来一堆数、
    用户看一眼没保存就走了，账上并没有期初，那初始化就还没做完。
    """
    if book_year.init_openings_at is not None:
        return False
    book_year.init_openings_at = datetime.now(timezone.utc)
    book_year.init_openings_source = source
    db.flush()
    return True


__all__ = [
    "BLOCKED_HINT", "OPENINGS_SOURCE_CARRY", "OPENINGS_SOURCE_IMPORT",
    "OPENINGS_SOURCE_NONE", "blocked_reason", "init_state", "init_states",
    "mark_openings_ready", "mark_subjects_ready", "subject_import_state",
]
