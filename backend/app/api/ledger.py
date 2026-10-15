"""账簿：总账、明细账、科目余额表、试算平衡。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ledger import queries
from app.core.db import get_db
from app.core.deps import get_my_book
from app.models import Book, Period, Voucher

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
def balance(book_id: int, period_id: Optional[int] = None,
            from_period_id: Optional[int] = None,
            book: Book = Depends(get_my_book),
            db: Session = Depends(get_db), include_unposted: bool = Query(False)):
    """科目余额表。带 ``from_period_id`` 就是查**一段连续区间**（止期仍是 period_id）。

    两张表（余额表 / 总账）共用同一份区间实现（queries.balance_sheet_of_accounts），
    所以同一段区间在两边问出来的数必然一致 —— 审计第 7.3 节记过的
    「导出与页面显示对不上」，根子就是同一个口径有两份实现。
    """
    period = _period(db, book.id, period_id)
    from_period = None
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
        # 起始晚于止期：界面把它归位了，直接调接口也照样挡住 ——
        # 否则算出来的是一段「倒着走」的区间，期初比期末还晚，数字能看不能信
        if from_period.ord > period.ord:
            raise HTTPException(400, "起始期间不能晚于结束期间")
    subjects = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(
        db, book.id, subjects, period, include_unposted, from_period=from_period)
    is_range = from_period is not None and from_period.id != period.id
    return {
        "period": period.code, "period_id": period.id,
        "from_period": from_period.code if is_range else None,
        "from_period_id": from_period.id if is_range else None,
        "range": is_range,
        "rows": rows, "check": check, "include_unposted": include_unposted,
    }


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
           db: Session = Depends(get_db), include_unposted: bool = Query(False),
           from_subject_id: Optional[int] = None, to_subject_id: Optional[int] = None):
    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period
    return queries.detail_ledger(
        db, book.id, subject_id, from_period, to_period, include_unposted,
        from_subject_id=from_subject_id, to_subject_id=to_subject_id,
    )


@router.get("/voucher-balances")
def voucher_balances(book_id: int, period_id: Optional[int] = None,
                     voucher_id: Optional[int] = Query(None),
                     book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """凭证录入页每行科目格左下角那个数字。

    传 ``voucher_id`` 是为了把这张自己剔掉 —— 它会边填边自动保存，
    算进去的话余额跟着自己动。口径见 ``queries.voucher_subject_balances``。
    """
    period = _period(db, book.id, period_id)
    if voucher_id:
        v = db.get(Voucher, voucher_id)
        # 跨账套的凭证 id 一律当没传：宁可少剔一张，也不能把别家账的数字端回来
        if v is not None and v.book_id != book.id:
            voucher_id = None
    balances = queries.voucher_subject_balances(db, book.id, period, exclude_voucher_id=voucher_id)
    return {"period_id": period.id, "period": period.code,
            "balances": {str(k): v for k, v in balances.items()}}


# 原来的 `GET /ledger/voucher-no-preview`（只回一个 `next` 号码）撤了：
# 前端一处都没接它，而新开凭证真正需要的是「号码 + 期间最后一张的日期」两个值，
# 那已经在 `GET /books/{id}/vouchers/next` 里了 —— 留在 ledger 底下也放错了地方。
