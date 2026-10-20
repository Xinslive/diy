"""FastAPI 依赖：当前用户、账套授权。"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
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
    request: Request = None,  # type: ignore[assignment]
) -> SysUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少访问令牌")
    try:
        payload = decode_access_token(creds.credentials)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访问令牌无效或已过期")
    # 把载荷挂到 request.state 上，供下游依赖读取（见
    # require_recent_admin_mfa）。**不要重新解一遍 JWT** —— 那既慢，
    # 又会出现「解两次得到两份载荷」的不一致窗口。
    if request is not None:
        request.state.auth_payload = payload
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


def get_my_book(book_id: int, db: Session = Depends(get_db),
                user: SysUser = Depends(get_current_user),
                request: Request = None) -> Book:  # type: ignore[assignment]
    _link_or_403(request, db, user.id, book_id)
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


def require_recent_admin_mfa(
    user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
) -> SysUser:
    """高危动作的「近期重新认证」依赖。

    **语义**：当前用户必须是管理员，**且**令牌的 ``mfa_at`` 落在
    ``admin_mfa_grace_minutes`` 分钟内 —— 否则 403，并明确告诉用户
    「需要在 X 分钟内完成邮箱二次验证」以及去哪儿做（``POST
    /api/auth/mfa/reauth``）。

    **为什么令牌里没有 ``mfa_at`` 就一律拒绝**（fail-closed）：那意味着
    这个会话是「没经过二次验证」来的 —— 老令牌、或者二次验证开着但
    这个会话是关着的时候签发的。两种都不该被当成「刚验过」。

    **开关关着时这个依赖不拦人**。理由是可用性：二次验证是**可选的
    组织决策**，默认关闭（见 ``admin_mfa_enabled`` 的注释）—— 若「没开
    二次验证」就等于「所有管理员都做不了高危动作」，那么这个功能的
    默认状态会变成一次全站事故。

    但**开关开着而通道坏了的时候一律拒绝**（fail-closed）—— 那时管理员
    本来就换不到新令牌（连登录都过不去），高危动作同样不该放行。
    真正的逃生口是 ``scripts/mfa_admin.py --disable``（明确关掉）与
    进程环境里的 ``ADMIN_MFA_EMERGENCY_CODE``。

    **父会话接线点**：``api/system.py`` 的高危子集挂这个依赖即可，
    不必再判断开关 —— 判定在这里。
    """
    from app.notify import mfa as mfa_service

    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有系统管理员可以访问")
    if not mfa_service.mfa_enabled(db):
        return user

    grace = mfa_service.grace_seconds()
    payload = None
    if request is not None:
        payload = getattr(request.state, "auth_payload", None)
    mfa_at = (payload or {}).get("mfa_at")
    if not mfa_at:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"该操作需要近期完成邮箱二次验证：请在 {grace // 60} 分钟内通过 "
            "POST /api/auth/mfa/reauth 重新验证后再试")
    try:
        age = time.time() - float(mfa_at)
    except (TypeError, ValueError):
        age = None
    if age is None or age > grace:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"该操作需要近期完成邮箱二次验证：请在 {grace // 60} 分钟内通过 "
            "POST /api/auth/mfa/reauth 重新验证后再试")
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


def get_book_link(book_id: int, db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user),
                  request: Request = None) -> UserBook:  # type: ignore[assignment]
    return _link_or_403(request, db, user.id, book_id)


def require_book_write(book_id: int, db: Session = Depends(get_db),
                       user: SysUser = Depends(get_current_user),
                       request: Request = None) -> UserBook:  # type: ignore[assignment]
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

    这里**不再自己查一遍** ``UserBook``（审计中等 4）：``get_my_book`` /
    ``get_book_link`` / ``require_book_write`` 发的是同一条查询，而几乎所有写接口
    把它们并排挂着（``vouchers.py:119``、``printing.py:236``），于是每个写请求在
    业务逻辑开始前就有 4 次串行往返。现在统一走 :func:`_link_or_403`，
    同一个请求内只发一条。
    """
    # 直接调函数体而不是再挂一次 Depends：这里三个参数都由外层依赖注入了，
    # 走 get_book_link() 等于再查一次 UserBook。
    link = _link_or_403(request, db, user.id, book_id)
    require_write(None, user, link)  # type: ignore[arg-type]
    return link


def _link_or_403(request: Optional[Request], db: Session,
                 user_id: int, book_id: int) -> UserBook:
    """查「这个用户在这个账套里的授权」，没有就 403。**同一个请求内只查一次。**

    复用是**请求内**的，不是跨请求缓存（审计中等 4 明确要求不做跨请求缓存：
    改密、停用、换角色必须立刻生效）。载体是 ``request.state`` —— 它随这一个请求
    的生命周期存在，下一个请求重新查。
    查询的键是 ``(user_id, book_id)`` 而不是只按账套，所以一个请求里碰多个账套
    也不会串。

    没有 ``request``（比如被当成普通函数直接调用）时退化成每次都查：
    少一次优化，但不会因为拿不到 request 就崩。
    """
    cache = None
    if request is not None:
        cache = getattr(request.state, "_cwq_book_links", None)
        if cache is None:
            cache = {}
            request.state._cwq_book_links = cache
    key = (user_id, book_id)
    if cache is not None and key in cache:
        link = cache[key]
    else:
        link = db.scalar(select(UserBook).where(
            UserBook.user_id == user_id, UserBook.book_id == book_id))
        if cache is not None:
            # None 也记下来：「没有授权」同样是这个请求里已经查明的事实，
            # 第二个依赖再问一遍不会得到别的答案。
            cache[key] = link
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    return link
