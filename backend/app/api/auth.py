from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import LoginIn, TokenOut
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, verify_password
from app.models import SysUser, UserBook

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(SysUser).where(SysUser.username == payload.username))
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    token, expires = create_access_token(user.id, user.username)
    books = db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
    return TokenOut(
        access_token=token,
        expires_at=expires,
        user={
            "id": user.id,
            "username": user.username,
            "real_name": user.real_name,
            "books": [{"id": b.book_id, "role": b.role} for b in books],
        },
    )


@router.get("/me")
def me(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    books = db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
    return {
        "id": user.id,
        "username": user.username,
        "real_name": user.real_name,
        "books": [{"id": b.book_id, "role": b.role} for b in books],
    }
