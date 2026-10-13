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
    # 令牌版本核对（审计 P2-01）。JWT 自签名、服务端不存会话，所以「谁签发的、
    # 什么时候签的」之外，服务端唯一能查到的状态就是库里这一列。
    # 管理员重置口令或停用账号时把它加一，这个账号此前签发的所有令牌同时作废 ——
    # 口令泄露后的标准处置就是「改口令」，过去改完旧令牌还能用满 12 小时，
    # 等于没改。
    #
    # 载荷里没有 ``tv`` 的老令牌按 0 处理：升级前签发的令牌在存量库里
    # 也是 tv=0，仍然可用（改口令/停用会立刻作废它们）。
    if int(payload.get("tv", 0)) != (user.token_version or 0):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "访问令牌已失效（口令或账号状态已变更），请重新登录")
    return user


def get_my_book(book_id: int, db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)) -> Book:
    link = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_id == book_id))
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账套不存在")
    return book


def require_admin(user: SysUser = Depends(get_current_user)) -> SysUser:
    """系统管理员专用依赖。

    操作日志里存着每次删除/修改的完整凭证快照 —— 等同于全量凭证副本，
    普通会计不该看到别人的操作痕迹。
    """
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有系统管理员可以访问")
    return user


def require_write(book: Book, user: SysUser, link: UserBook) -> None:
    if link.role == "VIEWER":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账套为只读权限")


def get_book_link(book_id: int, db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)) -> UserBook:
    link = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_id == book_id))
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    return link


def require_book_write(book_id: int, db: Session = Depends(get_db),
                       user: SysUser = Depends(get_current_user)) -> UserBook:
    """**账套写操作统一依赖**：既要归属这个账套，又要能写。

    为什么抽成依赖：权限校验散在各路由里，历史上就这么漏过。
    ``close_period`` / ``unclose_period`` / ``year_end`` / ``set_openings``
    当时只挂了 ``get_my_book``（它只查「有没有授权」，不查角色），而同一个文件里的
    ``carry_forward`` 查了 —— 于是 VIEWER 能结账、反结账、年度结转、篡改期初。

    **规则：新增写接口一律挂这个依赖**，不要手写 ``if link.role == "VIEWER"``。
    漏挂时 ``tests/test_auth_permissions.py`` 的全量矩阵用例会红 ——
    用测试盯住「每个写接口都挡住了 VIEWER」，而不是靠人记得。

    返回 ``UserBook``：调用方通常已经通过 ``get_my_book`` 拿到账套了，
    两个依赖可以并排挂，不冲突。
    """
    # 直接调函数体而不是再挂一次 Depends：这里三个参数都由外层依赖注入了，
    # 走 get_book_link() 等于再查一次 UserBook。
    link = _link_or_403(db, user.id, book_id)
    require_write(None, user, link)  # type: ignore[arg-type]
    return link


def _link_or_403(db: Session, user_id: int, book_id: int) -> UserBook:
    link = db.scalar(select(UserBook).where(UserBook.user_id == user_id, UserBook.book_id == book_id))
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    return link
