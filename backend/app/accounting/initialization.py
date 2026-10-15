"""账套初始化：两步的**状态**与**闸门**。

初始化被显式拆成有先后的两步：

    1. 导入科目（或确认沿用系统装好的标准科目表）
    2. 导入期初余额

**为什么顺序不能反。** 期初余额是挂在科目上的：账上一张科目表都没有时，
录进来的数挂不到任何科目上，只能整批丢掉，而且用户看不出来 ——
界面照样显示「已读入 N 条」，库里的科目却一个都不匹配。
用户自己看不出这个先后关系（科目表和期初余额是两个不相干的页面），
所以状态记在账套上（``book.init_*``，迁移 0018），由接口层拦。

**第 1 步有两条路，都算数。** 一是从用友等外部系统导入科目表，
二是确认沿用建账时系统装好的标准科目表。只认第一条会把
「就用标准科目表从零开始记账」的用户挡在门外 —— 建账本来就自动装标准表，
对这类用户来说第 1 步等于「确认一下」。

**为什么是服务层而不是路由里现算。** 同一个判断要在三处用到：
期初解析、期初保存、账套列表/详情/初始化接口下发。散着写三遍，
总有一处会漏 —— 而漏掉的那一处恰好就是用户绕过限制的入口。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Book, Entry, Period, PeriodOpening, Subject, Voucher

# 拒绝时给用户看的整句话。放在常量里而不是各路由各拼一遍：
# 界面弹窗、错误提示、接口文案说的是同一件事就该有一份措辞。
BLOCKED_HINT = "请先完成初始化第 1 步：导入科目，或确认沿用系统标准科目表"

# 第 2 步的两种来源。IMPORT = 导入过余额表；NONE = 本期本来就没有期初余额。
# 分开记是因为这两种「第 2 步已完成」在账面上的含义完全相反：一种账上有数，
# 一种账上一个数都没有，事后翻审计日志必须分得清。
OPENINGS_SOURCE_IMPORT = "IMPORT"
OPENINGS_SOURCE_NONE = "NONE"


def subject_import_state(db: Session, book_id: int) -> dict:
    """这个账套现在能不能覆盖导入科目表，以及挡在中间的各有多少数据。

    从 ``api/subjects.py`` 搬到这里：初始化状态也要用到同一份计数
    （能不能导入、还剩多少科目），两边各查一遍的话，
    「账套列表说能导入、点进去说不能」这种不一致迟早出现。
    """
    subject_count = db.scalar(
        select(func.count()).select_from(Subject).where(Subject.book_id == book_id)
    ) or 0
    voucher_count = db.scalar(
        select(func.count()).select_from(Voucher).where(Voucher.book_id == book_id)
    ) or 0
    entry_count = db.scalar(
        select(func.count()).select_from(Entry).where(Entry.book_id == book_id)
    ) or 0
    opening_count = db.scalar(
        select(func.count()).select_from(PeriodOpening)
        .join(Period, Period.id == PeriodOpening.period_id)
        .where(Period.book_id == book_id)
    ) or 0
    return {
        "subject_count": subject_count,
        "voucher_count": voucher_count,
        "entry_count": entry_count,
        "opening_count": opening_count,
        "can_overwrite": voucher_count == 0 and entry_count == 0 and opening_count == 0,
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _shape(book: Book, subjects: int, vouchers: int, entries: int, openings: int) -> dict[str, Any]:
    subjects_ready = book.init_subjects_at is not None
    openings_ready = book.init_openings_at is not None
    if subjects_ready and openings_ready:
        step = 0          # 两步都走完了
    elif subjects_ready:
        step = 2
    else:
        step = 1
    return {
        "book_id": book.id,
        "subjects_ready": subjects_ready,
        "subjects_source": book.init_subjects_source if subjects_ready else None,
        "subjects_at": _iso(book.init_subjects_at),
        "subject_count": subjects,
        "openings_ready": openings_ready,
        "openings_source": book.init_openings_source if openings_ready else None,
        "openings_at": _iso(book.init_openings_at),
        "opening_count": openings,
        "step": step,
        # 「沿用系统标准科目表」只在账套确实装着科目时才可点 ——
        # 一个科目都没有的账套点它等于什么都没确认。
        "can_keep_chart": subjects > 0,
        "can_import_subjects": vouchers == 0 and entries == 0 and openings == 0,
    }


def init_states(db: Session, books: list[Book]) -> dict[int, dict[str, Any]]:
    """一批账套的初始化状态，**四条计数查询打完所有账套**。

    账套列表要一次给每个账套挂一个「待初始化」标记。逐个算就是
    「每个账套 4 条 count」—— 十个账套四十次往返，而列表本身
    已经在按账套逐个查年度了。四个 group by 一次拿全，与账套数无关。
    """
    if not books:
        return {}
    ids = [b.id for b in books]

    def grouped(stmt) -> dict[int, int]:
        return {bid: n for bid, n in db.execute(stmt).all()}

    subjects = grouped(
        select(Subject.book_id, func.count())
        .where(Subject.book_id.in_(ids)).group_by(Subject.book_id)
    )
    vouchers = grouped(
        select(Voucher.book_id, func.count())
        .where(Voucher.book_id.in_(ids)).group_by(Voucher.book_id)
    )
    entries = grouped(
        select(Entry.book_id, func.count())
        .where(Entry.book_id.in_(ids)).group_by(Entry.book_id)
    )
    openings = grouped(
        select(Period.book_id, func.count(PeriodOpening.id))
        .join(PeriodOpening, PeriodOpening.period_id == Period.id)
        .where(Period.book_id.in_(ids)).group_by(Period.book_id)
    )
    return {
        b.id: _shape(b, subjects.get(b.id, 0), vouchers.get(b.id, 0),
                     entries.get(b.id, 0), openings.get(b.id, 0))
        for b in books
    }


def init_state(db: Session, book: Book) -> dict[str, Any]:
    """初始化进度。下发给初始化向导、账套卡片和期初余额页的是同一份。"""
    return init_states(db, [book])[book.id]



def blocked_reason(book: Book) -> Optional[str]:
    """第 1 步没做完时给出的拦路话术；做完了返回 ``None``。"""
    if book.init_subjects_at is not None:
        return None
    return (
        f"账套《{book.name}》还没完成初始化第 1 步（确定科目），不能导入或保存期初余额。"
        f"{BLOCKED_HINT}"
    )


def mark_subjects_ready(db: Session, book: Book, source: str) -> bool:
    """记下「第 1 步已完成」。返回状态是否**这次**才发生变化。

    幂等是有意义的：用户可能反复点「沿用标准科目表」，
    每次都重写时间戳的话，审计日志里就会留下一串看不出差别的记录，
    「这一步是什么时候完成的」也就没了。所以只在第一次落地时写时间戳。
    """
    if book.init_subjects_at is not None:
        return False
    now = datetime.now(timezone.utc)
    book.init_subjects_at = now
    book.init_subjects_source = source
    db.flush()
    return True


def mark_openings_ready(db: Session, book: Book,
                        source: str = OPENINGS_SOURCE_IMPORT) -> bool:
    """第 2 步完成时记下时间与来源。返回状态是否**这次**才发生变化。

    ``source`` 两种：``IMPORT``（真的写进了期初余额）与 ``NONE``（本期本来就没有）。
    只记时间不记来源的话，这两种在库里长得一模一样 —— 而事后要查的恰恰
    是「当时账上到底有没有数」。

    口径是**存进库了**，不是「页面上填了数」—— 预览里读进来一堆数、
    用户看一眼没保存就走了，账上并没有期初，那初始化就还没做完。
    """
    if book.init_openings_at is not None:
        return False
    book.init_openings_at = datetime.now(timezone.utc)
    book.init_openings_source = source
    db.flush()
    return True


__all__ = [
    "BLOCKED_HINT", "OPENINGS_SOURCE_IMPORT", "OPENINGS_SOURCE_NONE",
    "blocked_reason", "init_state", "init_states", "mark_openings_ready",
    "mark_subjects_ready", "subject_import_state",
]
