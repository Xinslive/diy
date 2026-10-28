"""登录后一次性取得用户、账套、期间和当前上下文。"""
from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.books import list_my_books, list_periods
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models import Book, SysUser

router = APIRouter(prefix="/api/bootstrap", tags=["启动上下文"])


def _select_book(books: list[dict], requested: int | None) -> dict | None:
    if requested:
        for book in books:
            if book.get("id") == requested:
                return book
    return books[0] if books else None


def _select_period(periods: list[dict], requested: int | None) -> dict | None:
    if requested:
        for period in periods:
            if period.get("id") == requested:
                return period
    open_period = next((period for period in periods if not period.get("locked")), None)
    return open_period or (periods[-1] if periods else None)


@router.get("")
def bootstrap(
    response: Response,
    book_id: int | None = Query(None, ge=1),
    period_id: int | None = Query(None, ge=1),
    user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    started = perf_counter()
    books = list_my_books(user=user, db=db)
    selected_book = _select_book(books, book_id)
    periods: list[dict] = []
    selected_period = None
    if selected_book:
        book = db.get(Book, selected_book["id"])
        if book is not None:
            periods = list_periods(book=book, db=db)
            selected_period = _select_period(periods, period_id)

    # Keep the user shape identical to /auth/me while avoiding a second DB query.
    payload = {
        "user": {
            "id": user.id,
            "username": user.username,
            "real_name": user.real_name,
            "is_admin": user.is_admin,
            "books": [{"id": book["id"], "role": book["role"]} for book in books],
        },
        "books": books,
        "selected_book_id": selected_book.get("id") if selected_book else None,
        "selected_period_id": selected_period.get("id") if selected_period else None,
        "periods": periods,
    }
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Server-Timing"] = f"bootstrap;dur={(perf_counter() - started) * 1000:.1f}"
    return payload
