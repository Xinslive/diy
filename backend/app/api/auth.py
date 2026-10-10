from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import LoginIn, RegisterIn, TokenOut
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models import SysUser, UserBook

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", status_code=201)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    """自助注册：**只建账号，不授予任何账套**。

    刻意不在这里发 token：没有账套授权的用户登录后是一片空白，
    让他自己走一遍登录、看到「请联系管理员分配账套」，比自动进来更清楚。
    """
    if not settings.registration_enabled:
        raise HTTPException(403, "系统未开放自助注册，请联系管理员开号")
    if db.scalar(select(SysUser).where(SysUser.username == payload.username)):
        raise HTTPException(400, f"用户名「{payload.username}」已被占用")
    u = SysUser(username=payload.username, password_hash=hash_password(payload.password),
                real_name=payload.real_name or payload.username, active=True)
    db.add(u)
    db.commit()
    return {"ok": True, "username": u.username}


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
            "is_admin": user.is_admin,
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
        "is_admin": user.is_admin,
        "books": [{"id": b.book_id, "role": b.role} for b in books],
    }
