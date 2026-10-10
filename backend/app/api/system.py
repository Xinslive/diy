"""系统管理：用户、账套授权。

内部系统只有几十个人，但「谁能看哪个账套」这件事必须落在数据库里，
不能靠前端藏按钮。因此权限模型只有三个角色：

    OWNER       账套负责人，可改科目/模板/授权
    ACCOUNTANT  可制单、记账、结账、结转
    VIEWER      只读

授权粒度是 (用户, 账套)。一个用户可以有多个账套，每个账套角色独立。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import hash_password
from app.models import AuditLog, Book, SysUser, UserBook

router = APIRouter(prefix="/api/system", tags=["系统管理"])

ROLES = ("OWNER", "ACCOUNTANT", "VIEWER")


def _is_admin(user: SysUser) -> bool:
    return user.is_admin


def _require_admin(user: SysUser) -> None:
    if not _is_admin(user):
        raise HTTPException(403, "只有系统管理员可以执行该操作")


def _serialize(u: SysUser, links: dict[int, str]) -> dict:
    return {
        "id": u.id, "username": u.username, "real_name": u.real_name,
        "active": u.active, "is_admin": u.is_admin,
        "books": [{"book_id": bid, "role": role} for bid, role in sorted(links.items())],
    }


def _links_by_user(db: Session) -> dict[int, dict[int, str]]:
    out: dict[int, dict[int, str]] = {}
    for link in db.scalars(select(UserBook)).all():
        out.setdefault(link.user_id, {})[link.book_id] = link.role
    return out


# --------------------------------------------------------------------- 用户


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    real_name: str = ""
    active: bool = True
    books: list[dict] = Field(default_factory=list)   # [{"book_id":1,"role":"ACCOUNTANT"}]


class UserPatch(BaseModel):
    real_name: Optional[str] = None
    password: Optional[str] = None
    active: Optional[bool] = None


def _apply_links(db: Session, user: SysUser, books: list[dict]) -> None:
    valid = {b["book_id"]: b["role"] for b in books
             if db.get(Book, b["book_id"]) is not None}
    for link in db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all():
        if link.book_id not in valid:
            db.delete(link)
        else:
            link.role = valid.pop(link.book_id)
    for book_id, role in valid.items():
        db.add(UserBook(user_id=user.id, book_id=book_id, role=role))


def _check_roles(items: list[dict]) -> None:
    for it in items:
        if it.get("role") not in ROLES:
            raise HTTPException(400, f"未知角色「{it.get('role')}」，可选：{'、'.join(ROLES)}")


@router.get("/users")
def list_users(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    # 这里能看到所有人的密码状态和账套授权，必须限管理员
    _require_admin(user)
    links = _links_by_user(db)
    rows = db.scalars(select(SysUser).order_by(SysUser.id)).all()
    return [_serialize(u, links.get(u.id, {})) for u in rows]


@router.post("/users", status_code=201)
def create_user(payload: UserIn, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_admin(user)
    if db.scalar(select(SysUser).where(SysUser.username == payload.username)):
        raise HTTPException(400, f"用户名「{payload.username}」已存在")
    _check_roles(payload.books)
    u = SysUser(username=payload.username, password_hash=hash_password(payload.password),
                real_name=payload.real_name or payload.username, active=payload.active)
    db.add(u)
    db.flush()
    _apply_links(db, u, payload.books)
    db.commit()
    db.refresh(u)
    return _serialize(u, {b["book_id"]: b["role"] for b in payload.books})


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserPatch, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    if payload.real_name is not None:
        target.real_name = payload.real_name
    if payload.active is not None:
        target.active = payload.active
    if payload.password:
        target.password_hash = hash_password(payload.password)
    db.commit()
    links = _links_by_user(db)
    return _serialize(target, links.get(target.id, {}))


@router.put("/users/{user_id}/books")
def set_books(user_id: int, books: list[dict], user: SysUser = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """整份替换某用户的账套授权列表。"""
    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    _check_roles(books)
    _apply_links(db, target, books)
    db.commit()
    return _serialize(target, {b["book_id"]: b["role"] for b in books})


@router.delete("/users/{user_id}")
def delete_user(user_id: int, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_admin(user)
    if user_id == user.id:
        raise HTTPException(400, "不能删除当前登录的用户")
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    db.delete(target)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------- 账套


@router.get("/books")
def all_books(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """授权界面需要看到全部账套，而不只是自己有权的那几个。"""
    _require_admin(user)
    rows = db.execute(
        select(Book, func.count(UserBook.id))
        .outerjoin(UserBook, UserBook.book_id == Book.id)
        .group_by(Book.id).order_by(Book.company_name, Book.name)
    ).all()
    return [{"id": b.id, "company_name": b.company_name, "name": b.name,
             "start_year": b.start_year, "member_count": n} for b, n in rows]


# --------------------------------------------------------------------- 全局审计


@router.get("/audit-logs")
def audit_logs(limit: int = 100, user: SysUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    # 全局审计跨所有账套，普通用户只能看自己账套的（/books/{id}/audit-logs）
    _require_admin(user)
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc())
                      .limit(min(max(limit, 1), 500))).all()
    return [{"id": r.id, "username": r.username, "action": r.action, "entity": r.entity,
             "entity_id": r.entity_id, "detail": r.detail,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "before": r.before, "after": r.after} for r in rows]
