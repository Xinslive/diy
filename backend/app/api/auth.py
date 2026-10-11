from datetime import datetime, timedelta, timezone, timezone

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


def _utcnow() -> datetime:
    """当前 UTC 时间，naive。

    库里 ``locked_until`` 是 naive 的 DateTime，比较时两边必须同口径。
    这里显式转成 UTC 再去掉 tzinfo，而不是用 ``datetime.utcnow()`` ——
    后者在新版 Python 已废弃，而它被悄悄换成「本地时间」的那天，
    东八区部署的锁定时长会凭空变成 8 小时。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lock_left(user: SysUser) -> int:
    """还锁着几秒。没锁返回 0。

    拿服务器当前时间去比，而不是靠「锁定时记一个时间戳」之外的东西 ——
    库里的 ``locked_until`` 是 UTC naive 时间，比较时必须同样按 UTC 处理，
    否则部署在东八区时锁定会**变成 8 小时**（把本地时间当 UTC 比，偏了 8 小时）。
    """
    if user.locked_until is None:
        return 0
    return max(0, int((user.locked_until - _utcnow()).total_seconds()))


def _record_failure(db: Session, user: SysUser) -> None:
    """记一次失败，达阈值就锁。

    计数和锁定时间一起写、一起提交 —— 分两次写的话，中途断电会留下
    「计数加了但没锁」的账，攻击者正好卡在这个窗口里无限试。
    """
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= settings.login_max_failures:
        user.locked_until = _utcnow() + timedelta(minutes=settings.login_lock_minutes)
        user.failed_logins = 0
    db.commit()


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(SysUser).where(SysUser.username == payload.username))
    if user is not None and not user.active:
        raise HTTPException(401, "用户名或密码错误")
    if user is not None:
        left = _lock_left(user)
        if left > 0:
            # 锁定期间也**不**去校验口令：校验了就等于告诉攻击者
            # 「密码猜对了，只是被锁着」，把口令空间又缩掉一层。
            raise HTTPException(423, f"账号已锁定，请 {left // 60 + 1} 分钟后再试")
    if user is None or not verify_password(payload.password, user.password_hash):
        if user is not None:
            _record_failure(db, user)
        raise HTTPException(401, "用户名或密码错误")
    # 登录成功即清零：否则一次误输留下的计数会跟后面几次正常登录叠起来，
    # 最终把一个从没被人猜过的账号锁住。
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
    token, expires = create_access_token(user.id, user.username, is_admin=bool(user.is_admin))
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
