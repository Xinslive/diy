"""账簿：总账、明细账、科目余额表、试算平衡。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ledger import queries
from app.core.db import get_db
from app.core.deps import get_my_book
from app.models import Book, Period

router = APIRouter(prefix="/api/books/{book_id}/ledger", tags=["账簿"])


def _period(db: Session, book_id: int, period_id: int | None) -> Period:
    stmt = select(Period).where(Period.book_id == book_id)
    if period_id:
        p = db.scalar(stmt.where(Period.id == period_id))
    else:
        p = db.scalar(stmt.order_by(Period.ord.desc()).limit(1))
    if p is None:
        raise HTTPException(404, "会计期间不存在")
    return p


@router.get("/balance")
def balance(book_id: int, period_id: Optional[int] = None, book: Book = Depends(get_my_book),
            db: Session = Depends(get_db), include_unposted: bool = Query(False)):
    period = _period(db, book.id, period_id)
    subjects = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(db, book.id, subjects, period, include_unposted)
    return {"period": period.code, "period_id": period.id, "rows": rows, "check": check,
            "include_unposted": include_unposted}


@router.get("/general")
def general(book_id: int, period_id: Optional[int] = None, from_period_id: Optional[int] = None,
            book: Book = Depends(get_my_book), db: Session = Depends(get_db),
            include_unposted: bool = Query(False), leaf_only: bool = Query(False)):
    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period

    subjects = queries.subject_tree(db, book.id)
    if leaf_only:
        subjects = [s for s in subjects if s.is_leaf]
    rows = queries.general_ledger(db, book.id, subjects, from_period, to_period, include_unposted)
    return {
        "from_period": from_period.code, "to_period": to_period.code,
        "from_period_id": from_period.id, "to_period_id": to_period.id,
        "rows": rows, "include_unposted": include_unposted,
    }


@router.get("/detail")
def detail(book_id: int, subject_id: int, period_id: Optional[int] = None,
           from_period_id: Optional[int] = None, book: Book = Depends(get_my_book),
           db: Session = Depends(get_db), include_unposted: bool = Query(False)):
    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period
    return queries.detail_ledger(db, book.id, subject_id, from_period, to_period, include_unposted)


# 原来的 `GET /ledger/voucher-no-preview`（只回一个 `next` 号码）撤了：
# 前端一处都没接它，而新开凭证真正需要的是「号码 + 期间最后一张的日期」两个值，
# 那已经在 `GET /books/{id}/vouchers/next` 里了 —— 留在 ledger 底下也放错了地方。
