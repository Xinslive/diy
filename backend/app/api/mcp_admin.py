"""MCP 令牌的管理面（管理员）。

**这是新的权限面，所以它自己也是权限面。**
签发一个令牌等于给外部程序开一个「读这个用户能读的一切」的入口 —— 所以
管理接口全部挂 ``require_admin``（路由级依赖 + 函数体二次判定，与
``api/system.py`` 同一个套路），签发/轮换/吊销**全部写审计**。

**因为令牌永不过期，吊销是唯一的安全阀**，所以这一组接口必须「好用且
留痕」：

* 列表回带 ``last_used_at`` 与 ``unused_days`` —— 管理员能一眼看出哪些
  令牌是僵尸、哪些还在被外部 agent 用着；
* 签发**必须**给 ``name`` —— 一个用户可以有多个令牌（每个 agent 一个），
  便于按 agent 单独吊销而不是一刀切；
* 签发响应**回带该用户可访问的账套清单与各账套角色** —— 绑定一个 OWNER
  的令牌意味着 agent 能读该用户**所有**账套，签发人必须当场知情
  （建议为外部 agent 建**只授权单账套的 VIEWER 用户**）。

明文令牌**只在签发/轮换的响应里返回一次**，库里只有 ``sha256`` 摘要。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.core.db import get_db
from app.core.deps import get_current_user, require_admin
from app.models import Book, McpToken, SysUser, UserBook

from app.mcp import auth as mcp_auth

router = APIRouter(
    prefix="/api/system/mcp-tokens",
    tags=["系统管理"],
    # 路由级管理员门禁（与 ``api/system.py`` 同一个理由）：新增端点时不会
    # 漏挂 —— 门禁进依赖图之后，FastAPI 在**校验请求体之前**就先求依赖。
    dependencies=[Depends(require_admin)],
)


class IssueIn(BaseModel):
    """签发令牌。"""

    name: str = Field(min_length=1, max_length=64)
    user_id: int

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("令牌用途不能为空")
        return v


def _require_admin(user: SysUser) -> None:
    if not user.is_admin:
        raise HTTPException(403, "只有系统管理员可以执行该操作")


def _books_of(db: Session, user_id: int) -> list[dict]:
    """这个用户能访问的账套 + 各自角色。

    签发响应与列表都回带它 —— 令牌绑定的用户能访问什么，agent 就能访问
    什么，签发人必须当场看见。
    """
    rows = db.execute(
        select(Book, UserBook.role)
        .join(UserBook, UserBook.book_id == Book.id)
        .where(UserBook.user_id == user_id)
        .order_by(UserBook.sort_order.asc().nulls_last(), Book.company_name, Book.name)
    ).all()
    return [
        {
            "id": book.id,
            "name": book.name,
            "company_name": book.company_name,
            "role": role,
        }
        for book, role in rows
    ]


def _token_view(db: Session, row: McpToken, user: Optional[SysUser]) -> dict:
    """一个令牌的出参形状。**永远不含明文**（明文不落库，回不出来的）。"""
    grace_until = row.grace_until
    in_grace = bool(grace_until is not None and row.revoked_at is not None
                    and grace_until > mcp_auth.now_utc())
    return {
        "id": row.id,
        "name": row.name,
        "user_id": row.user_id,
        "user_name": user.username if user is not None else "",
        "enabled": bool(row.enabled),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "grace_until": grace_until.isoformat() if grace_until else None,
        # 宽限期内仍可用 —— 与「已吊销」是两件事，界面上要能分开看。
        "in_grace": in_grace,
        # 从来没用过 ⇒ None；用过但很久没用 ⇒ 一个天数。
        "unused_days": mcp_auth.unused_days(row),
        # 该用户当前可访问的账套与角色（可见性 100% 来自授权表）。
        "books": _books_of(db, row.user_id),
    }


@router.get("")
def list_tokens(db: Session = Depends(get_db),
                user: SysUser = Depends(get_current_user)):
    """列出全部令牌。**不返回明文。**"""
    _require_admin(user)
    rows = list(db.scalars(
        select(McpToken).order_by(McpToken.created_at.desc(), McpToken.id.desc())
    ).all())
    users = {u.id: u for u in db.scalars(select(SysUser)).all()}
    return [_token_view(db, row, users.get(row.user_id)) for row in rows]


@router.post("", status_code=201)
def issue_token(payload: IssueIn, db: Session = Depends(get_db),
                user: SysUser = Depends(get_current_user)):
    """签发一个令牌。

    **明文只在这次响应里返回一次**，之后谁也拿不回来（库里只有摘要）。
    响应同时回带该用户可访问的账套清单 —— 让签发人当场确认 agent 将看到
    什么。
    """
    _require_admin(user)
    target = db.get(SysUser, payload.user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    if not target.active:
        raise HTTPException(400, "该用户已停用，不能签发令牌")

    row, plain = mcp_auth.issue_token(
        db, user=target, name=payload.name, created_by=user.username)
    log(db, action="mcp.token.issue", entity="mcp_token", entity_id=row.id,
        user=user, detail=f"签发 MCP 令牌「{payload.name}」给用户 {target.username}",
        after={"name": payload.name, "user_id": target.id})
    db.commit()
    db.refresh(row)
    view = _token_view(db, row, target)
    # 明文只在这里出现。
    view["token"] = plain
    return view


@router.delete("")
def revoke_user_tokens(user_id: int, db: Session = Depends(get_db),
                       user: SysUser = Depends(get_current_user)):
    """**批量吊销**某个用户的全部令牌。

    改密、离职、疑似泄漏时的标准处置。返回吊销了几条。
    """
    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    count = mcp_auth.revoke_user_tokens(db, user_id)
    log(db, action="mcp.token.revoke_all", entity="mcp_user", entity_id=user_id,
        user=user, detail=f"批量吊销用户 {target.username} 的 MCP 令牌（{count} 条）",
        after={"user_id": user_id, "count": count})
    db.commit()
    return {"revoked": count}


@router.delete("/{token_id}")
def revoke_token(token_id: int, db: Session = Depends(get_db),
                 user: SysUser = Depends(get_current_user)):
    """**立即吊销**：下一次调用就失效。

    不给宽限期 —— 宽限期是「轮换」的语义，立即吊销就是立即。
    """
    _require_admin(user)
    row = db.get(McpToken, token_id)
    if row is None:
        raise HTTPException(404, "令牌不存在")
    mcp_auth.revoke_token(db, row)
    log(db, action="mcp.token.revoke", entity="mcp_token", entity_id=row.id,
        user=user, detail=f"吊销 MCP 令牌「{row.name}」",
        after={"name": row.name, "user_id": row.user_id})
    db.commit()
    return {"ok": True, "id": row.id}


@router.post("/{token_id}/rotate")
def rotate_token(token_id: int, db: Session = Depends(get_db),
                 user: SysUser = Depends(get_current_user)):
    """轮换：签发新令牌，旧的进入**宽限期**。

    宽限期内旧令牌**仍然可用**，到期自动失效 —— 避免 agent 换 token 时
    断线（与 ``scheduler/service.py`` 的 ``rotate_trigger_secret`` 同一个
    做法：保留 previous 一段时间）。
    """
    _require_admin(user)
    row = db.get(McpToken, token_id)
    if row is None:
        raise HTTPException(404, "令牌不存在")
    target = db.get(SysUser, row.user_id)
    if target is None or not target.active:
        raise HTTPException(400, "令牌绑定的用户不存在或已停用")

    new_row, plain = mcp_auth.rotate_token(
        db, row, created_by=user.username)
    log(db, action="mcp.token.rotate", entity="mcp_token", entity_id=row.id,
        user=user, detail=f"轮换 MCP 令牌「{row.name}」",
        after={"name": row.name, "user_id": row.user_id,
               "new_token_id": new_row.id})
    db.commit()
    db.refresh(new_row)
    view = _token_view(db, new_row, target)
    view["token"] = plain
    view["previous_token_id"] = row.id
    view["previous_grace_until"] = row.grace_until.isoformat() if row.grace_until else None
    return view


__all__ = ["IssueIn", "router"]
