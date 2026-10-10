"""FastAPI 依赖：当前用户、账套授权。"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import decode_access_token
from app.models import Book, SysUser, UserBook

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> SysUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少访问令牌")
    try:
        payload = decode_access_token(creds.credentials)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访问令牌无效或已过期")
    user = db.get(SysUser, int(payload["sub"]))
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    return user


def get_my_book(book_id: int, db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)) -> Book:
    link = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_id == book_id))
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账套不存在")
    return book


def require_write(book: Book, user: SysUser, link: UserBook) -> None:
    if link.role == "VIEWER":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账套为只读权限")


def get_book_link(book_id: int, db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)) -> UserBook:
    link = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_id == book_id))
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    return link
