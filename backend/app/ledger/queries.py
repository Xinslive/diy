"""账簿查询：总账、明细账、科目余额表、试算平衡。

全部基于 app.ledger.engine 的派生量，不存在第二套余额计算。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.ledger.engine import ZERO, Figures, LedgerEngine, display_side
from app.models import Book, Entry, Period, Subject, Voucher
from app.models.book import book_first_ord

CATEGORY_ORDER = {"ASSET": 0, "LIABILITY": 1, "EQUITY": 2, "COST": 3, "PROFIT": 4}


def _entry_stmt(book_id: int, include_unposted: bool) -> Select:
    stmt = (
        select(Entry, Voucher, Subject)
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .join(Subject, Subject.id == Entry.subject_id)
        .where(Entry.book_id == book_id)
    )
    if not include_unposted:
        stmt = stmt.where(Voucher.status == "POSTED")
    return stmt


def subject_tree(db: Session, book_id: int) -> list[Subject]:
    """按会计科目表顺序返回所有科目（含非明细级、**含停用**）。

    刻意不过滤 ``active``：停用的语义是「不让再选它」，不是「删掉它」——
    老凭证还指着它，账还得算得出来、还得看得见。过滤掉的话，历史余额
    在界面上直接消失，用户会以为账丢了。

    写路径另有闸门（``accounting/vouchers.py`` 建凭证时校验科目可用），
    这里是只读展示，不该再拦一道。
    """
    return list(
        db.scalars(
            select(Subject)
            .where(Subject.book_id == book_id)
            .order_by(Subject.category, Subject.code)
        ).all()
    )


# --------------------------------------------------------------------- 明细账


def detail_ledger(
    db: Session,
    book_id: int,
    subject_id: int,
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
) -> dict:
    """明细账：逐笔列出分录并滚动计算余额。"""
    eng = LedgerEngine(db, book_id, include_unposted)
    subject = db.get(Subject, subject_id)
    rows = []

    # 区间期初 = 起始期间的期初
    opening_before = eng.opening(subject_id, from_period.ord)
    side, amount = display_side(subject, opening_before)
    running = opening_before
    rows.append(
        {
            "kind": "OPENING",
            "voucher_no": "期初余额",
            "voucher_date": None,
            "period": f"{from_period.year}-{from_period.month:02d}",
            "summary": "期初余额",
            "debit": ZERO,
            "credit": ZERO,
            "direction": side,
            "balance": amount,
        }
    )

    period_labels = {
        p.id: f"{p.year}-{p.month:02d}"
        for p in db.scalars(select(Period).where(Period.book_id == book_id)).all()
    }

    stmt = (
        _entry_stmt(book_id, include_unposted)
        .where(Entry.subject_id == subject_id)
        .where(Entry.period_ord >= from_period.ord, Entry.period_ord <= to_period.ord)
        .order_by(Voucher.voucher_date, Voucher.voucher_no, Entry.line_no)
    )
    dr_sum = cr_sum = ZERO
    for entry, voucher, _subj in db.execute(stmt).all():
        running += entry.amount if entry.direction == "DEBIT" else -entry.amount
        dr_sum += entry.amount if entry.direction == "DEBIT" else ZERO
        cr_sum += entry.amount if entry.direction == "CREDIT" else ZERO
        side, amount = display_side(subject, running)
        rows.append(
            {
                "kind": "ENTRY",
                "voucher_id": voucher.id,
                "voucher_no": voucher.voucher_no,
                "voucher_date": voucher.voucher_date.isoformat(),
                "period": period_labels.get(voucher.period_id, ""),
                "summary": entry.summary,
                "debit": entry.amount if entry.direction == "DEBIT" else ZERO,
                "credit": entry.amount if entry.direction == "CREDIT" else ZERO,
                "direction": side,
                "balance": amount,
                "status": voucher.status,
            }
        )

    side, amount = display_side(subject, running)
    rows.append(
        {
            "kind": "TOTAL",
            "voucher_no": "本期合计",
            "voucher_date": None,
            "period": "",
            "summary": "",
            "debit": dr_sum,
            "credit": cr_sum,
            "direction": side,
            "balance": amount,
        }
    )

    return {
        "subject": {"id": subject.id, "code": subject.code, "name": subject.name,
                    "direction": subject.direction, "category": subject.category},
        "from_period": f"{from_period.year}-{from_period.month:02d}",
        "to_period": f"{to_period.year}-{to_period.month:02d}",
        "rows": rows,
    }


# --------------------------------------------------------------------- 总账


def general_ledger(
    db: Session,
    book_id: int,
    subjects: list[Subject],
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
) -> list[dict]:
    """总账：每个科目一行，含区间期初、本期发生、累计发生、期末。"""
    eng = LedgerEngine(db, book_id, include_unposted)
    ids = [s.id for s in subjects]
    fmap = eng.compute(ids, to_period.ord)
    # 区间发生额单独算，不能拿本年累计相减：区间跨年时两边的累计口径不同
    pmap = eng.range_movements(ids, from_period.ord, to_period.ord)

    book = eng._book()
    has_prev = from_period.ord > book_first_ord(book)
    if has_prev:
        prev_map = eng.compute(ids, from_period.ord - 1)

    out = []
    for s in subjects:
        f = fmap[s.id]
        p_dr, p_cr = pmap[s.id]
        if has_prev:
            opening = prev_map[s.id].closing
        else:
            opening = f.opening

        closing = opening + p_dr - p_cr
        side, amount = display_side(s, closing)
        out.append(
            {
                "subject_id": s.id,
                "code": s.code,
                "name": s.name,
                "level": s.level,
                "category": s.category,
                "is_leaf": s.is_leaf,
                "opening_direction": display_side(s, opening)[0],
                "opening": display_side(s, opening)[1],
                "period_debit": p_dr,
                "period_credit": p_cr,
                "ytd_debit": f.dr_cum,
                "ytd_credit": f.cr_cum,
                "direction": side,
                "closing": amount,
            }
        )
    return out


# --------------------------------------------------------------------- 科目余额表 / 试算平衡


def balance_sheet_of_accounts(
    db: Session,
    book_id: int,
    subjects: list[Subject],
    period: Period,
    include_unposted: bool = False,
) -> tuple[list[dict], dict]:
    """科目余额表。返回 (行, 试算平衡校验结果)。

    试算平衡：
        Σ 借方发生额 == Σ 贷方发生额
        Σ 借方余额    == Σ 贷方余额
    """
    eng = LedgerEngine(db, book_id, include_unposted)
    fmap = eng.compute([s.id for s in subjects], period.ord)

    rows = []
    total_open_dr = total_open_cr = total_dr = total_cr = total_close_dr = total_close_cr = ZERO

    for s in subjects:
        f = fmap[s.id]
        o_side, o_amt = display_side(s, f.opening)
        c_side, c_amt = display_side(s, f.closing)
        rows.append(
            {
                "subject_id": s.id,
                "code": s.code,
                "name": s.name,
                "level": s.level,
                "category": s.category,
                "is_leaf": s.is_leaf,
                "opening_direction": o_side,
                "opening": o_amt,
                "period_debit": f.period_dr,
                "period_credit": f.period_cr,
                "ytd_debit": f.dr_cum,
                "ytd_credit": f.cr_cum,
                "direction": c_side,
                "closing": c_amt,
            }
        )
        # 只累计明细级科目，否则父级汇总行会重复计数
        if s.is_leaf:
            total_open_dr += f.opening if f.opening > 0 else ZERO
            total_open_cr += -f.opening if f.opening < 0 else ZERO
            total_dr += f.period_dr
            total_cr += f.period_cr
            total_close_dr += f.closing if f.closing > 0 else ZERO
            total_close_cr += -f.closing if f.closing < 0 else ZERO

    check = {
        "period_balanced": total_dr == total_cr,
        "closing_balanced": total_close_dr == total_close_cr,
        "opening_balanced": total_open_dr == total_open_cr,
        "total_debit": total_dr,
        "total_credit": total_cr,
        "total_closing_debit": total_close_dr,
        "total_closing_credit": total_close_cr,
    }
    return rows, check
