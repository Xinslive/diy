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

# 账套角色的**唯一**合法取值。
#
# 这三处必须一致，改一处就得改另外两处：
#   core/deps.py（这里，判定）→ models/system.py（CHECK 约束）→ api/system.py::ROLES（入口校验）
# 少一处的后果都是「值能进库但判不出来」，进而退化成放行。
WRITE_ROLES = frozenset({"OWNER", "ACCOUNTANT"})
READ_ONLY_ROLES = frozenset({"VIEWER"})


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


def normalize_role(role: Optional[str]) -> str:
    """把库里存的 role 归一化成判定用的形式。

    大小写与首尾空白都吃掉。**权限判定不能因为「写库的人手滑打了个小写」
    就换一个含义** —— 过去的写法是 ``if link.role == "VIEWER"``，
    于是一个字面量比不中就放行。
    """
    return (role or "").strip().upper()


def is_read_only(role: Optional[str]) -> bool:
    """这个角色能不能写。**白名单语义**（审计 P2-12）。

    黑名单（只挡 "VIEWER"）与白名单（只放行 OWNER/ACCOUNTANT）的差别，
    全部落在「不认识的值」上：'viewer'、'read_only'、''、None、
    'Owner'（没被归一化到的那层）—— 黑名单一律**放行**，于是绕过 API
    直接改库的写入者（psql、脚本、误连的 ETL）拿到完整写权限，
    这与项目通篇 fail-closed 的原则正好相反。

    白名单一律**按只读处理**：配置写错的后果变成「这个人进不去」，
    而不是「这个人拿到全部权限」，并且真出问题时管理员能在审计里
    看到那个非法角色值，改回来即可。
    """
    return normalize_role(role) not in WRITE_ROLES


def require_write(book: Book, user: SysUser, link: UserBook) -> None:
    """只读账号一律拒写，**含一切不认识的角色值**。

    区分两种拒绝文案不是讲究：VIEWER 是正常业务状态（用户知道自己是只读），
    而非法的角色值是**配置事故**，需要有人去修 —— 文案一样的话，
    管理员看到满屏「当前账套为只读权限」会以为是自己配的只读。
    """
    role = (link.role or "")[:32]
    if normalize_role(role) in READ_ONLY_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账套为只读权限")
    if normalize_role(role) not in WRITE_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"账套角色「{role}」不在系统允许的取值内（{'、'.join(sorted(WRITE_ROLES | READ_ONLY_ROLES))}），"
            "已按只读处理，请联系管理员修正")


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
