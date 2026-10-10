"""凭证服务：新增 / 修改 / 删除 / 记账 / 反记账 / 查询 / 批量。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log, voucher_snapshot
from app.accounting.util import find_period, money, period_label
from app.models import Book, Entry, Period, Subject, SysUser, Voucher


# --------------------------------------------------------------------- 编号


def next_voucher_no(db: Session, period: Period, prefix: str = "记") -> str:
    """期间内顺序编号。凭证号允许回收复用，因此取「当前最大值 + 1」即可。"""
    last = db.scalar(
        select(func.max(func.substring(Voucher.voucher_no, 3)))
        .where(Voucher.period_id == period.id)
    )
    seq = (int(last) + 1) if last and str(last).strip().isdigit() else 1
    return f"{prefix}{seq:04d}"


# --------------------------------------------------------------------- 校验


def _validate_entries(db: Session, book_id: int, entries: list[dict]) -> None:
    if not entries:
        raise ValueError("凭证至少需要一条分录")
    dr = sum(money(e["amount"]) for e in entries if e["direction"] == "DEBIT")
    cr = sum(money(e["amount"]) for e in entries if e["direction"] == "CREDIT")
    if dr != cr:
        raise ValueError(f"借贷不平衡：借方 {dr} / 贷方 {cr}")
    if dr <= 0:
        raise ValueError("凭证金额必须大于 0")

    subject_ids = {e["subject_id"] for e in entries}
    found = set(
        db.scalars(
            select(Subject.id).where(
                Subject.book_id == book_id,
                Subject.id.in_(subject_ids),
                Subject.active.is_(True),
            )
        ).all()
    )
    missing = subject_ids - found
    if missing:
        raise ValueError(f"科目不存在或已停用: {sorted(missing)}")


# --------------------------------------------------------------------- 写操作


def create_voucher(
    db: Session,
    *,
    book_id: int,
    user: SysUser,
    voucher_date: date,
    entries: list[dict],
    remark: str = "",
    attachments: int = 0,
    source: str = "MANUAL",
    voucher_no: str | None = None,
    period_id: int | None = None,
    commit: bool = True,
) -> Voucher:
    period = db.get(Period, period_id) if period_id else find_period(db, book_id, voucher_date)
    if period is None:
        raise ValueError("会计期间不存在")
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能新增凭证")

    payload = [
        {
            "subject_id": int(e["subject_id"]),
            "direction": e["direction"],
            "amount": money(e["amount"]),
            "summary": e.get("summary", "") or "",
            "aux_ref": e.get("aux_ref") or {},
        }
        for e in entries
    ]
    _validate_entries(db, book_id, payload)

    voucher = Voucher(
        book_id=book_id,
        period_id=period.id,
        voucher_no=voucher_no or next_voucher_no(db, period),
        voucher_date=voucher_date,
        status="UNPOSTED",
        source=source,
        remark=remark,
        attachments=attachments,
        created_by=user.username,
    )
    db.add(voucher)
    db.flush()

    for i, e in enumerate(payload, start=1):
        db.add(Entry(voucher_id=voucher.id, line_no=i, **e))

    db.flush()
    log(
        db,
        action="CREATE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=book_id,
        detail=f"新增凭证 {voucher.voucher_no}",
        after=voucher_snapshot(voucher),
    )
    if commit:
        db.commit()
        db.refresh(voucher)
    return voucher


def update_voucher(
    db: Session, voucher: Voucher, user: SysUser, payload: dict
) -> Voucher:
    before = voucher_snapshot(voucher)
    period = db.get(Period, voucher.period_id)
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能修改凭证")

    if "voucher_date" in payload and payload["voucher_date"]:
        new_date = payload["voucher_date"]
        new_period = find_period(db, voucher.book_id, new_date)
        voucher.voucher_date = new_date
        voucher.period_id = new_period.id

    if "entries" in payload:
        entries = [
            {
                "subject_id": int(e["subject_id"]),
                "direction": e["direction"],
                "amount": money(e["amount"]),
                "summary": e.get("summary", "") or "",
                "aux_ref": e.get("aux_ref") or {},
            }
            for e in payload["entries"]
        ]
        _validate_entries(db, voucher.book_id, entries)
        voucher.entries.clear()
        db.flush()
        for i, e in enumerate(entries, start=1):
            voucher.entries.append(Entry(voucher_id=voucher.id, line_no=i, **e))
        # 改动内容后回到未记账，避免「已记账但内容变了」的脏状态
        voucher.status = "UNPOSTED"
        voucher.posted_at = None
        voucher.posted_by = None

    if "remark" in payload:
        voucher.remark = payload["remark"] or ""
    if "attachments" in payload:
        voucher.attachments = int(payload["attachments"] or 0)

    db.flush()
    log(
        db,
        action="UPDATE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"修改凭证 {voucher.voucher_no}",
        before=before,
        after=voucher_snapshot(voucher),
    )
    db.commit()
    db.refresh(voucher)
    return voucher


def delete_voucher(db: Session, voucher: Voucher, user: SysUser) -> None:
    """删除凭证。因为编号可复用，必须把内容快照写进审计日志。"""
    snapshot = voucher_snapshot(voucher)
    log(
        db,
        action="DELETE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"删除凭证 {voucher.voucher_no}",
        before=snapshot,
    )
    db.delete(voucher)
    db.commit()


# --------------------------------------------------------------------- 记账


def post_vouchers(db: Session, voucher_ids: list[int], user: SysUser) -> int:
    if not voucher_ids:
        return 0
    vouchers = list(db.scalars(select(Voucher).where(Voucher.id.in_(voucher_ids))).all())
    period_ids = {v.period_id for v in vouchers}
    locked = db.execute(
        select(Period.year, Period.month).where(Period.id.in_(period_ids), Period.locked.is_(True))
    ).all()
    if locked:
        codes = [period_label(y, m) for y, m in locked]
        raise ValueError(f"会计期间 {', '.join(codes)} 已结账，不能记账")

    count = 0
    for v in vouchers:
        if v.status == "POSTED":
            continue
        dr = sum(money(e.amount) for e in v.entries if e.direction == "DEBIT")
        cr = sum(money(e.amount) for e in v.entries if e.direction == "CREDIT")
        if dr != cr:
            raise ValueError(f"凭证 {v.voucher_no} 借贷不平衡，不能记账")
        v.status = "POSTED"
        v.posted_at = datetime.now(timezone.utc)
        v.posted_by = user.username
        count += 1
    db.flush()
    log(db, action="POST", entity="voucher", entity_id="batch", user=user,
        book_id=vouchers[0].book_id, detail=f"记账 {count} 张凭证", after={"ids": voucher_ids})
    db.commit()
    return count


def unpost_vouchers(db: Session, voucher_ids: list[int], user: SysUser) -> int:
    if not voucher_ids:
        return 0
    vouchers = list(db.scalars(select(Voucher).where(Voucher.id.in_(voucher_ids))).all())
    period_ids = {v.period_id for v in vouchers}
    locked = db.execute(
        select(Period.year, Period.month).where(Period.id.in_(period_ids), Period.locked.is_(True))
    ).all()
    if locked:
        codes = [period_label(y, m) for y, m in locked]
        raise ValueError(f"会计期间 {', '.join(codes)} 已结账，不能反记账")

    count = 0
    for v in vouchers:
        if v.status == "UNPOSTED":
            continue
        v.status = "UNPOSTED"
        v.posted_at = None
        v.posted_by = None
        count += 1
    db.flush()
    log(db, action="UNPOST", entity="voucher", entity_id="batch", user=user,
        book_id=vouchers[0].book_id, detail=f"反记账 {count} 张凭证", before={"ids": voucher_ids})
    db.commit()
    return count


# --------------------------------------------------------------------- 查询


def query_vouchers(
    db: Session,
    book_id: int,
    *,
    from_period: Period | None = None,
    to_period: Period | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    subject_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Voucher], int]:
    stmt = select(Voucher).where(Voucher.book_id == book_id)

    if from_period:
        stmt = stmt.where(
            Voucher.period_id.in_(
                select(Period.id).where(Period.book_id == book_id, Period.ord >= from_period.ord)
            )
        )
    if to_period:
        stmt = stmt.where(
            Voucher.period_id.in_(
                select(Period.id).where(Period.book_id == book_id, Period.ord <= to_period.ord)
            )
        )
    if date_from:
        stmt = stmt.where(Voucher.voucher_date >= date_from)
    if date_to:
        stmt = stmt.where(Voucher.voucher_date <= date_to)
    if status:
        stmt = stmt.where(Voucher.status == status)
    if source:
        stmt = stmt.where(Voucher.source == source)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Voucher.voucher_no.ilike(like),
                Voucher.remark.ilike(like),
                Voucher.id.in_(
                    select(Entry.voucher_id).where(Entry.summary.ilike(like))
                ),
            )
        )
    if subject_id:
        stmt = stmt.where(Voucher.id.in_(select(Entry.voucher_id).where(Entry.subject_id == subject_id)))
    if min_amount is not None or max_amount is not None:
        amt = select(Entry.voucher_id, func.sum(Entry.amount).label("total"))
        amt = amt.where(Entry.book_id == book_id).group_by(Entry.voucher_id).having(
            func.sum(Entry.amount) >= money(min_amount or 0)
        )
        if max_amount is not None:
            amt = amt.having(func.sum(Entry.amount) <= money(max_amount))
        stmt = stmt.where(Voucher.id.in_(amt.subquery().select()))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = (
        stmt.order_by(Voucher.voucher_date.desc(), Voucher.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list(db.scalars(stmt).all())
    for v in rows:
        db.refresh(v)
    return rows, total
