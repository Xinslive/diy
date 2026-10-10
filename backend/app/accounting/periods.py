"""会计期间服务：结账、批量反结账、结转损益、年度结转。

设计说明
--------
期初余额永远是**推导值**：期初(P) = 期末(P-1) = 期初(P-1) + 借方累计(P-1) - 贷方累计(P-1)，
一路回推到账套启用月份那笔录入的期初余额。因此：

* 平时不需要写任何期初快照，改动上游数据余额自动跟随，不存在快照失效的问题。
* period_opening 表只存两处「真·期初」：账套启用时的录入、年度结转时的结转。

「已结账」由 period.locked 保证：锁上的期间任何写入都被数据库触发器拒绝。
反过来，解锁之后重算出来的数字自然与改动一致 —— 这正是反结账要的效果。

结账级联
--------
不允许出现「3 月已结账、1 月还开着」这种状态：它会让已结账期间的累计数随
上游改动而变化。所以关闭第 N 期时，前面未结账的期间一并结账（这正是反结账
的镜像操作）。用户不需要理解规则，只需要在结果里看到「顺带结账了哪几期」。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.accounting.util import ZERO, money, period_label
from app.ledger.engine import LedgerEngine
from app.models import (
    Book,
    Entry,
    Period,
    PeriodOpening,
    Subject,
    SysUser,
    UserBook,
    Voucher,
)

PROFIT_SUBJECT_CODE = "4103"     # 本年利润
RETAINED_SUBJECT_CODE = "4104"   # 利润分配 — 未分配利润


def get_period(db: Session, book_id: int, period_id: int) -> Period:
    p = db.get(Period, period_id)
    if p is None or p.book_id != book_id:
        raise ValueError("会计期间不存在")
    return p


def _subject_by_code(db: Session, book_id: int, code: str) -> Optional[Subject]:
    return db.scalar(select(Subject).where(Subject.book_id == book_id, Subject.code == code))


# --------------------------------------------------------------------- 结账


def close_period(db: Session, period: Period, user: SysUser, auto_post: bool = True) -> dict:
    """关闭指定期间，并顺带关闭其之前所有未结账期间。"""
    target = db.execute(
        select(Period).where(Period.id == period.id).with_for_update()
    ).scalar_one()
    if target.locked:
        raise ValueError(f"{target.code} 已经结账")

    chain = list(
        db.execute(
            select(Period)
            .where(Period.book_id == target.book_id, Period.ord <= target.ord)
            .order_by(Period.ord)
            .with_for_update()
        ).scalars()
    )

    auto_closed, auto_posted = [], 0
    for p in chain:
        if p.locked:
            continue
        posted = _auto_post(db, p, user) if auto_post else _check_unposted(db, p)
        auto_posted += posted
        if p.id != target.id:
            auto_closed.append(p.code)
        p.locked = True
        p.closed_at = func.now()
        p.closed_by = user.username
        p.version += 1

    # 损益科目必须已结平
    eng = LedgerEngine(db, target.book_id)
    subjects = db.scalars(
        select(Subject).where(
            Subject.book_id == target.book_id,
            Subject.carry_forward.is_(True),
            Subject.active.is_(True),
            Subject.is_leaf.is_(True),
        )
    ).all()
    residual = []
    for s in subjects:
        f = eng.figures(s.id, target.ord)
        if abs(f.closing) > ZERO:
            residual.append(f"{s.code} {s.name} 余额 {f.closing}")
    if residual:
        raise ValueError(
            "以下损益科目尚有余额，请先执行「结转损益」：\n" + "\n".join(residual)
        )

    log(
        db, action="CLOSE", entity="period", entity_id=target.id, user=user,
        book_id=target.book_id,
        detail=(
            f"结账 {target.code}"
            + (f"，顺带结账 {'、'.join(auto_closed)}" if auto_closed else "")
            + (f"，自动记账 {auto_posted} 张" if auto_posted else "")
        ),
    )
    db.commit()
    return {
        "period": target.code,
        "auto_closed_periods": auto_closed,
        "auto_posted": auto_posted,
    }


def _auto_post(db: Session, period: Period, user: SysUser) -> int:
    rows = db.scalars(
        select(Voucher).where(Voucher.period_id == period.id, Voucher.status == "UNPOSTED")
    ).all()
    for v in rows:
        v.status = "POSTED"
        v.posted_by = f"{user.username}(结账自动)"
        v.posted_at = func.now()
    db.flush()
    return len(rows)


def _check_unposted(db: Session, period: Period) -> int:
    nos = db.scalars(
        select(Voucher.voucher_no).where(
            Voucher.period_id == period.id, Voucher.status == "UNPOSTED"
        ).order_by(Voucher.voucher_no)
    ).all()
    if nos:
        raise ValueError(f"{period.code} 仍有 {len(nos)} 张未记账凭证：{', '.join(nos[:10])}")
    return 0


# --------------------------------------------------------------------- 批量反结账


def preview_unclose(db: Session, book_id: int, period: Period) -> dict:
    """反结账影响范围预览。"""
    locked = list(
        db.scalars(
            select(Period).where(
                Period.book_id == book_id, Period.locked.is_(True), Period.ord >= period.ord
            ).order_by(Period.ord)
        ).all()
    )
    ids = [p.id for p in locked]
    posted_count = db.scalar(
        select(func.count()).select_from(Voucher).where(
            Voucher.period_id.in_(ids), Voucher.status == "POSTED"
        )
    ) or 0
    voucher_total = db.scalar(
        select(func.count()).select_from(Voucher).where(Voucher.period_id.in_(ids))
    ) or 0
    return {
        "from_period": period.code,
        "periods": [p.code for p in locked],
        "will_unpost_vouchers": posted_count,
        "total_vouchers": voucher_total,
    }


def batch_unclose(
    db: Session, book_id: int, period: Period, user: SysUser, revert_posting: bool = True
) -> dict:
    """批量反结账：从指定期间起，连续解锁到最后一个已锁期间。

    区间内的已记账凭证自动回到未记账 —— 账套已解锁而凭证仍标记「已记账」，
    账簿默认口径下余额会凭空消失，用户改完凭证重新记账时数字会跳变。
    """
    locked = list(
        db.execute(
            select(Period)
            .where(Period.book_id == book_id, Period.locked.is_(True), Period.ord >= period.ord)
            .order_by(Period.ord)
            .with_for_update()
        ).scalars()
    )
    if not locked:
        raise ValueError(f"{period.code} 及其之后的期间都未结账，无需反结账")

    preview = preview_unclose(db, book_id, period)
    period_ids = [p.id for p in locked]

    # 先解锁 —— 凭证的写保护触发器是按期间锁判断的，
    # 顺序反了会把自己锁死在门外。
    for p in locked:
        p.locked = False
        p.closed_at = None
        p.closed_by = None
        p.version += 1
    db.flush()

    reverted = 0
    if revert_posting:
        for v in db.scalars(
            select(Voucher).where(
                Voucher.period_id.in_(period_ids), Voucher.status == "POSTED"
            )
        ).all():
            v.status = "UNPOSTED"
            v.posted_at = None
            v.posted_by = None
            reverted += 1
        db.flush()

    log(
        db, action="UNCLOSE", entity="period", entity_id=period.id, user=user,
        book_id=book_id,
        detail=f"反结账 {preview['from_period']} ~ {locked[-1].code}，{len(locked)} 个期间，"
               f"{reverted} 张凭证转未记账",
        after=preview,
    )
    db.commit()
    return {**preview, "unposted_vouchers": reverted}


# --------------------------------------------------------------------- 结转损益


def carry_forward_profit(db: Session, book_id: int, period: Period, user: SysUser) -> dict:
    """月末结转损益：把损益类科目余额结转到「本年利润」。"""
    period = db.execute(select(Period).where(Period.id == period.id).with_for_update()).scalar_one()
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能结转")

    # 幂等护栏：结转生成的凭证默认是「未记账」，而账簿默认口径只统计已记账，
    # 所以「损益是否已结平」这判断在刚结转完是看不见的 —— 连点两次就会生成
    # 两张结转凭证，把本年利润翻倍。必须显式挡一次。
    existing = list(
        db.scalars(
            select(Voucher).where(
                Voucher.period_id == period.id, Voucher.source == "CARRY_FORWARD"
            ).order_by(Voucher.id)
        ).all()
    )
    if existing:
        nos = "、".join(v.voucher_no for v in existing)
        raise ValueError(
            f"{period.code} 已执行过结转损益（凭证 {nos}）。"
            f"如需重新结转，请先删除这几张凭证再操作。"
        )

    profit = _subject_by_code(db, book_id, PROFIT_SUBJECT_CODE)
    if profit is None:
        raise ValueError(f"账套缺少「本年利润」科目（编码 {PROFIT_SUBJECT_CODE}）")

    subjects = list(
        db.scalars(
            select(Subject).where(
                Subject.book_id == book_id,
                Subject.carry_forward.is_(True),
                Subject.active.is_(True),
                Subject.is_leaf.is_(True),
                Subject.id != profit.id,
            ).order_by(Subject.code)
        ).all()
    )

    eng = LedgerEngine(db, book_id)
    fmap = eng.compute([s.id for s in subjects], period.ord)

    income, expense = [], []
    for s in subjects:
        bal = fmap[s.id].closing
        if bal == ZERO:
            continue
        (expense if bal > 0 else income).append((s, abs(bal)))

    if not income and not expense:
        raise ValueError("本期损益类科目全部为 0，无需结转")

    from app.accounting.vouchers import create_voucher

    last_day = date(period.year, period.month, _days_in_month(period.year, period.month))
    created = []

    if income:
        entries = [{"subject_id": s.id, "direction": "DEBIT", "amount": amt,
                    "summary": f"结转{period.code}损益"} for s, amt in income]
        entries.append({"subject_id": profit.id, "direction": "CREDIT",
                        "amount": sum(a for _, a in income), "summary": f"结转{period.code}损益"})
        created.append(create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（收入类）"))

    if expense:
        entries = [{"subject_id": profit.id, "direction": "DEBIT",
                    "amount": sum(a for _, a in expense), "summary": f"结转{period.code}损益"}]
        entries += [{"subject_id": s.id, "direction": "CREDIT", "amount": amt,
                     "summary": f"结转{period.code}损益"} for s, amt in expense]
        created.append(create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（成本费用类）"))

    return {
        "period": period.code,
        "vouchers": [v.voucher_no for v in created],
        "income": [(s.code, s.name, str(amt)) for s, amt in income],
        "expense": [(s.code, s.name, str(amt)) for s, amt in expense],
    }


def _days_in_month(year: int, month: int) -> int:
    return 31 if month == 12 else (date(year, month + 1, 1) - date(year, month, 1)).days


# --------------------------------------------------------------------- 年度结转


def year_end_carry(db: Session, book: Book, user: SysUser, new_book_name: str | None = None) -> dict:
    """年度结转：生成本年利润结转凭证，并生成下一年度账套及其期初余额。"""
    from app.accounting.vouchers import create_voucher, post_vouchers

    periods = list(
        db.scalars(select(Period).where(Period.book_id == book.id).order_by(Period.ord)).all()
    )
    if not periods:
        raise ValueError("账套尚未初始化会计期间")
    last = periods[-1]

    open_periods = [p.code for p in periods if not p.locked]
    if open_periods:
        raise ValueError(f"年度结转前必须全部结账，仍未结账：{', '.join(open_periods)}")

    # 1. 结转本年利润
    #    这张凭证必须落在本年最后一个月，若该期已结账则先解锁再补做 ——
    #    年度结转是一键动作，不该要求用户手动反结账一次。
    unlocked_for_carry = False
    profit = _subject_by_code(db, book.id, PROFIT_SUBJECT_CODE)
    retained = _subject_by_code(db, book.id, RETAINED_SUBJECT_CODE)
    if profit and retained:
        eng = LedgerEngine(db, book.id)
        bal = eng.figures(profit.id, last.ord).closing
        if bal != ZERO:
            if last.locked:
                batch_unclose(db, book.id, last, user, revert_posting=True)
                last = db.get(Period, last.id)
                unlocked_for_carry = True
            # 无论盈亏，本年利润都是「借方冲平」，未分配利润都是「贷方增加」：
            #   盈利 bal<0 -> 借 本年利润 | 贷 未分配利润（增加留存收益）
            #   亏损 bal>0 -> 借 本年利润 | 贷 未分配利润（冲减留存收益）
            # 早期版本按符号把两个科目的借贷对调了，盈利时会做成
            # 「借 未分配利润 / 贷 本年利润」，等于凭空给留存收益记了一笔借方。
            amt = abs(bal)
            entries = [
                {"subject_id": profit.id, "direction": "DEBIT", "amount": amt,
                 "summary": "结转本年利润"},
                {"subject_id": retained.id, "direction": "CREDIT", "amount": amt,
                 "summary": "结转本年利润"},
            ]
            create_voucher(db, book_id=book.id, user=user,
                           voucher_date=date(book.year, 12, 31), entries=entries,
                           source="CARRY_FORWARD", remark=f"{book.year}年 结转本年利润")
            db.commit()

            # 追加凭证后 12 月又变成未记账，重新记账并结账
            from app.accounting.vouchers import post_vouchers

            new_id = db.scalar(
                select(Voucher.id).where(
                    Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD"
                ).order_by(Voucher.id.desc()).limit(1)
            )
            post_vouchers(db, [new_id], user)
            close_period(db, last, user)

    db.commit()

    # 2. 生成下一年度账套
    new_book = Book(
        company_name=book.company_name,
        name=new_book_name or f"{book.name} {book.year + 1}",
        year=book.year + 1,
        start_month=book.start_month,
    )
    db.add(new_book)
    db.flush()
    # 继承源账套的授权，新账套才对老用户可见
    for link in db.scalars(select(UserBook).where(UserBook.book_id == book.id)).all():
        db.add(UserBook(user_id=link.user_id, book_id=new_book.id, role=link.role))
    _copy_subjects(db, book.id, new_book.id)
    _create_periods(db, new_book)

    # 3. 期初余额 = 本年期末余额
    eng = LedgerEngine(db, book.id)
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.is_leaf.is_(True),
                              Subject.active.is_(True))).all()
    fmap = eng.compute([s.id for s in subjects], last.ord)

    new_leaves = {
        s.code: s for s in db.scalars(
            select(Subject).where(Subject.book_id == new_book.id, Subject.is_leaf.is_(True))
        ).all()
    }
    first_period = db.scalar(
        select(Period).where(Period.book_id == new_book.id).order_by(Period.ord).limit(1)
    )
    count = 0
    for s in subjects:
        amount = fmap[s.id].closing
        if amount == ZERO:
            continue
        target = new_leaves.get(s.code)
        if target is None:
            continue
        db.add(PeriodOpening(book_id=new_book.id, period_id=first_period.id,
                             subject_id=target.id, amount=amount))
        count += 1

    book.status = "CLOSED"
    log(db, action="YEAR_END", entity="book", entity_id=book.id, user=user,
        book_id=book.id,
        detail=f"年度结转：{book.year} → {new_book.year}，结转 {count} 条期初余额")
    db.commit()
    return {"source_book": book.id, "new_book_id": new_book.id,
            "new_year": new_book.year, "carried_subjects": count,
            "unlocked_for_carry": unlocked_for_carry}


def _copy_subjects(db: Session, src_book: int, dst_book: int) -> None:
    rows = db.scalars(
        select(Subject).where(Subject.book_id == src_book).order_by(Subject.code)
    ).all()
    id_map: dict[int, int] = {}
    for s in rows:
        new = Subject(
            book_id=dst_book, code=s.code, name=s.name, parent_id=None, level=s.level,
            category=s.category, direction=s.direction, is_leaf=s.is_leaf,
            carry_forward=s.carry_forward, sort_no=s.sort_no, active=s.active,
            ext_attrs=s.ext_attrs,
        )
        db.add(new)
        db.flush()
        id_map[s.id] = new.id
    for s in rows:
        if s.parent_id and s.parent_id in id_map:
            target = db.get(Subject, id_map[s.id])
            target.parent_id = id_map[s.parent_id]
    db.flush()


def _create_periods(db: Session, book: Book) -> list[Period]:
    out = [Period(book_id=book.id, year=book.year, month=m, ord=m, locked=False)
           for m in range(1, 13)]
    for p in out:
        db.add(p)
    db.flush()
    return out


def create_periods(db: Session, book: Book) -> list[Period]:
    return _create_periods(db, book)
