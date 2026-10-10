"""账套、会计期间、结账/反结账、结转。"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import periods as svc
from app.api.schemas import BookIn, OpeningIn, UncloseIn, YearEndIn
from app.core.db import get_db
from app.core.deps import get_book_link, get_current_user, get_my_book
from app.models import Book, Period, PeriodOpening, Subject, SysCompany, SysUser, UserBook, Voucher

router = APIRouter(prefix="/api/books", tags=["账套与期间"])


@router.get("")
def list_my_books(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    closed_counts = dict(
        db.execute(
            select(Period.book_id, func.count())
            .where(Period.locked.is_(True)).group_by(Period.book_id)
        ).all()
    )
    rows = db.execute(
        select(Book, UserBook.role, SysCompany.name)
        .join(UserBook, UserBook.book_id == Book.id)
        .join(SysCompany, SysCompany.id == Book.company_id)
        .where(UserBook.user_id == user.id)
        .order_by(Book.year, Book.name)
    ).all()
    return [
        {
            "id": b.id, "name": b.name, "year": b.year, "start_month": b.start_month,
            "company": company, "role": role, "status": b.status,
            "closed_periods": db.scalar(
                select(func.count()).select_from(Period).where(
                    Period.book_id == b.id, Period.locked.is_(True))
            ),
        }
        for b, role, company in rows
    ]


@router.post("", status_code=201)
def create_book(payload: BookIn, user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    company = None
    if payload.company_id:
        company = db.get(SysCompany, payload.company_id)
    if company is None:
        company = db.scalar(select(SysCompany).where(SysCompany.name == payload.company_name))
    if company is None:
        company = SysCompany(name=payload.company_name)
        db.add(company)
        db.flush()

    exists = db.scalar(
        select(Book).where(Book.company_id == company.id, Book.name == payload.name, Book.year == payload.year)
    )
    if exists:
        raise HTTPException(400, f"账套「{payload.name} {payload.year}」已存在")

    book = Book(company_id=company.id, name=payload.name, year=payload.year,
                start_month=payload.start_month)
    db.add(book)
    db.flush()

    svc.create_periods(db, book)

    from app.seed.chart_of_accounts import install_chart

    subjects = install_chart(db, book.id)

    db.add(UserBook(user_id=user.id, book_id=book.id, role="OWNER"))
    db.commit()
    db.refresh(book)
    return {"id": book.id, "name": book.name, "year": book.year, "subjects_installed": subjects}


@router.get("/{book_id}")
def get_book(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    company = db.get(SysCompany, book.company_id)
    return {
        "id": book.id, "name": book.name, "year": book.year,
        "start_month": book.start_month, "status": book.status,
        "company": {"id": company.id, "name": company.name},
    }


@router.get("/{book_id}/periods")
def list_periods(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from sqlalchemy import func
    from app.models import Voucher

    periods = db.scalars(select(Period).where(Period.book_id == book.id).order_by(Period.ord)).all()
    counts = dict(
        db.execute(
            select(Voucher.period_id, func.count()).where(Voucher.book_id == book.id).group_by(Voucher.period_id)
        ).all()
    )
    posted = dict(
        db.execute(
            select(Voucher.period_id, func.count())
            .where(Voucher.book_id == book.id, Voucher.status == "POSTED")
            .group_by(Voucher.period_id)
        ).all()
    )
    return [
        {
            "id": p.id, "code": p.code, "year": p.year, "month": p.month, "ord": p.ord,
            "locked": p.locked, "closed_by": p.closed_by,
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "vouchers": counts.get(p.id, 0),
            "posted": posted.get(p.id, 0),
            "unposted": counts.get(p.id, 0) - posted.get(p.id, 0),
        }
        for p in periods
    ]


@router.post("/{book_id}/periods")
def set_openings(book_id: int, payload: list[OpeningIn], book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """录入账套期初余额（只在启用月份之前允许一次）。"""
    first = db.scalar(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord).limit(1)
    )
    if db.scalar(select(Voucher).where(Voucher.book_id == book.id).limit(1)):
        raise HTTPException(400, "账套已有业务数据，不能再修改期初余额")

    # 期初快照有数据库触发器保护（禁止 UPDATE），这里整体重建
    for row in db.scalars(select(PeriodOpening).where(PeriodOpening.period_id == first.id)).all():
        db.delete(row)
    db.flush()

    for item in payload:
        subject = db.get(Subject, item.subject_id)
        if subject is None or subject.book_id != book.id:
            raise HTTPException(400, f"科目不存在: {item.subject_id}")
        # 内部统一用「借正贷负」存储；用户录入的是该科目的自然方向余额
        amount = Decimal(str(item.amount))
        if subject.direction == "CREDIT":
            amount = -abs(amount)
        else:
            amount = abs(amount)

        db.add(PeriodOpening(book_id=book.id, period_id=first.id,
                             subject_id=subject.id, amount=amount))
    db.commit()
    return {"ok": True, "count": len(payload)}


@router.post("/{book_id}/periods/{period_id}/carry-forward")
def carry_forward(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.carry_forward_profit(db, book.id, period, user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{book_id}/periods/{period_id}/close")
def close_period(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.close_period(db, period, user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{book_id}/periods/{period_id}/unclose-preview")
def unclose_preview(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db)):
    period = svc.get_period(db, book.id, period_id)
    return svc.preview_unclose(db, book.id, period)


@router.post("/{book_id}/periods/{period_id}/unclose")
def unclose_period(book_id: int, period_id: int, payload: UncloseIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.batch_unclose(db, book.id, period, user, payload.revert_posting)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{book_id}/year-end")
def year_end(book_id: int, payload: YearEndIn, book: Book = Depends(get_my_book),
             db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    try:
        return svc.year_end_carry(db, book, user, payload.new_book_name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{book_id}/audit-logs")
def audit_logs(book_id: int, limit: int = Query(100, le=1000), book: Book = Depends(get_my_book),
               db: Session = Depends(get_db)):
    from app.models import AuditLog

    rows = db.scalars(
        select(AuditLog).where(AuditLog.book_id == book.id)
        .order_by(AuditLog.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id, "action": r.action, "entity": r.entity, "entity_id": r.entity_id,
            "username": r.username, "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "before": r.before, "after": r.after,
        }
        for r in rows
    ]
