"""系统管理：用户、账套授权、外部服务配置、数据安全（OSS 备份）。

内部系统只有几十个人，但「谁能看哪个账套」这件事必须落在数据库里，
不能靠前端藏按钮。因此权限模型只有三个角色：

    OWNER       账套负责人，可改科目/模板/授权
    ACCOUNTANT  可制单、记账、结账、结转
    VIEWER      只读

授权粒度是 (用户, 账套)。一个用户可以有多个账套，每个账套角色独立。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi.responses import JSONResponse
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.accounting.book_deletion import TRIGGER_REVOKE, purge_unowned_books
from app.backup import config as backup_config
from app.backup import oss_client
from app.backup import service as backup_service
from app.core import system_settings
from app.core import lockout
from app.core.db import SessionLocal, get_db
from app.core.deps import get_current_user, require_admin, require_recent_admin_mfa
from app.core.security import (PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH,
                               hash_password, password_policy_error)
from app.models import (REAL_NAME_MAX_LENGTH, SCOPE_SYSTEM, STATUS_FAILED,
                        TIER_DAILY, AuditLog, BackupRun, Book, SysUser,
                        UserBook)
from app.models.scheduler import ScheduledTask, ScheduledTaskRun
from app.notify import config as notify_config
from app.notify import events as notify_events
from app.notify import mailer as notify_mailer
from app.notify import mfa as mfa_service
from app.scheduler import service as scheduler_service
from app.plugins.workers import status as plugin_status
from app.models.plugin import PluginRegistry

router = APIRouter(
    prefix="/api/system", tags=["系统管理"],
    # **路由级管理员门禁**（审计 P3-1）。
    #
    # 这一层的意义是「**新增端点时不会漏挂**」：门禁进依赖图之后，
    # FastAPI 在**校验请求体之前**就会先求依赖，于是非管理员在任何一条
    # 路由上都拿不到 200 —— 哪怕它的请求体根本填不对。
    #
    # 过去这条门禁只写在**函数体第一行**（``_require_admin(user)``），于是：
    #   * 新加一条路由忘了写那一行 ⇒ 门禁整个不存在，CI 全绿；
    #   * 门禁在请求体校验**之后** ⇒ 一个畸形请求体先吃 422，
    #     「非管理员访问」这件事根本走不到判定。
    #
    # 函数体里的 ``_require_admin(user)`` **保留**：它是第二层（有人在
    # 依赖注入上做了手脚时仍然有效），而且它让每条路由**自己**说清
    # 「这条要管理员」。两处的判据是同一个（``core/deps.require_admin``
    # 与 ``_require_admin`` 都只看 ``user.is_admin``）。
    dependencies=[Depends(require_admin)],
)

ROLES = ("OWNER", "ACCOUNTANT", "VIEWER")


def _is_admin(user: SysUser) -> bool:
    return user.is_admin


def _require_admin(user: SysUser) -> None:
    if not _is_admin(user):
        raise HTTPException(403, "只有系统管理员可以执行该操作")


def _user_snapshot(u: SysUser) -> dict:
    """账号快照：**只含身份属性**。

    明文口令不会出现在这里（明文口令压根不落库），口令散列更不能 ——
    ``/api/system/audit-logs`` 是全局可读的，日志里出现散列等于把离线爆破的
    素材抄送给任何有管理员令牌的人。散列变了这件事本身记成
    ``password_changed: true`` 就够了。
    """
    return {"id": u.id, "username": u.username, "real_name": u.real_name,
            "active": u.active, "is_admin": u.is_admin}


def _serialize(u: SysUser, links: dict[int, str]) -> dict:
    return {
        "id": u.id, "username": u.username, "real_name": u.real_name,
        "active": u.active, "is_admin": u.is_admin,
        "books": [{"book_id": bid, "role": role} for bid, role in sorted(links.items())],
        # 锁定状态。**一次算全**（core/lockout.py::lock_summary）——
        # 免得前端自己拿 locked_until 去和浏览器时钟比，那又是一次口径
        # 分裂，而且用的是浏览器时区。
        **lockout.lock_summary(u),
    }


def _links_by_user(db: Session) -> dict[int, dict[int, str]]:
    out: dict[int, dict[int, str]] = {}
    for link in db.scalars(select(UserBook)).all():
        out.setdefault(link.user_id, {})[link.book_id] = link.role
    return out


def _active_admin_count(db: Session, *, exclude_id: Optional[int] = None) -> int:
    """库里还有几个**能登录**的管理员（``is_admin`` 且 ``active``）。

    **判据只收在这里**（审计 R2-1）—— 「能不能删最后一个管理员」这个判断
    只应该有一份实现。散在别处再写一份，迟早会对不上（而对不上的后果是
    「有的路径拦得住、有的路径拦不住」）。

    **为什么只数 ``active`` 的**：``active=False`` 的管理员**登不进去**
    （``core/deps.py::get_current_user`` 会拒），所以它算不上「管理面还有人」。
    把停用的管理员算进去，等于让一个已经锁死的系统看起来还有人能用 ——
    而那正是这条守卫要防的那件事本身。
    """
    stmt = select(func.count()).select_from(SysUser).where(
        SysUser.is_admin.is_(True), SysUser.active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(SysUser.id != exclude_id)
    return int(db.scalar(stmt) or 0)


# --------------------------------------------------------------------- 用户


class BookLinkIn(BaseModel):
    """一条账套授权。**这里必须是带类型的模型，不能是 ``dict``。**

    P3：原来是 ``books: list[dict]``，于是 ``book_id`` 拼错或漏掉都不会在
    入口被拦，一路走到 ``_apply_links`` 里才 ``KeyError`` → 500。
    而那段流程里的 ``_check_roles`` 用的是 ``it.get("role")``，两处对畸形
    输入的写法不一致 —— 同一个请求的缺字段表现成 500，调用方看不出是自己的
    payload 不对。

    改成带类型的模型后，FastAPI/pydantic 在**入口**就返回 422 并指明
    缺哪个字段：那才是这类错误该有的样子，也不用在下游到处防 ``KeyError``。
    """

    book_id: int
    role: str


# --------------------------------------------------------------------- 入参形状
#
# **用户名**：允许字母、数字、下划线、连字符、点，以及**中文等非 ASCII 文字**；
# 拒绝**空白与控制字符**。
#
# 拒绝控制字符（尤其 ``\r`` / ``\n``）是**安全要求**，不是洁癖：用户名会原样
# 进审计行的正文与告警邮件的正文（下面 ``create_user`` 的 detail、``app/
# notify/events.py::account_locked``），一个换行就能把一行撑成两行 —— 看起来
# 像多了一条记录。审计 P2-7 说的就是这个。
#
# **放行中文**：这套系统里中文姓名是常态，而「用户名必须是 ASCII」没有任何
# 业务依据。把中文挡在门外只会逼管理员起拼音缩写 —— 那是把「防日志注入」
# 做成了「防中文」。
#
# **不放行空格**：它同样没有业务用途，却会让人分不清「张三」与「张三 」。
USERNAME_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f]+$")

USERNAME_SHAPE_NOTE = (
    "账号不能包含空格、换行或控制字符"
    "（账号会写进审计日志与告警邮件，换行会伪造出多出来的记录）"
)

# --------------------------------------------------------------------- 口令策略
#
# **口径收在 app/core/security.py 一处**（审计 P2-1）：建号、重置与
# 「本人自助改密」问的是同一个问题 —— 「这个口令算不算一个可用的口令」。
# 过去这三处各写一份（建号 6 位、重置**没有**下限、自助注册 8 位），
# 而最危险的那条（管理员重置）恰恰是唯一一条没有下限的：把账号口令重置成
# 1 个字符，系统照收，而且那个口令**立即可用**。
#
# 搬家的理由不是「想让它更整齐」，而是**自助改密这条新路必须与建号用同一把
# 尺** —— 两处各判一次的话，迟早会对不上，而对不上的那一次一定是「更松」
# 的那处（新建的那条路径没人记得它的规矩）。
#
# 顶层那句 import 让 `app.api.system.password_policy_error` 仍然解析得到：
# 口径搬了家，但**别人引用它的路径不该跟着搬家**。


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    real_name: str = Field(default="", max_length=REAL_NAME_MAX_LENGTH)
    active: bool = True
    books: list[BookLinkIn] = Field(default_factory=list)

    @field_validator("username")
    @classmethod
    def _check_username_shape(cls, v: str) -> str:
        if not USERNAME_PATTERN.match(v):
            raise ValueError(USERNAME_SHAPE_NOTE)
        return v

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, v: str) -> str:
        problem = password_policy_error(v)
        if problem:
            raise ValueError(problem)
        return v


class UserPatch(BaseModel):
    real_name: Optional[str] = Field(default=None, max_length=REAL_NAME_MAX_LENGTH)
    password: Optional[str] = Field(default=None, min_length=PASSWORD_MIN_LENGTH,
                                    max_length=PASSWORD_MAX_LENGTH)
    active: Optional[bool] = None

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, v: Optional[str]) -> Optional[str]:
        """**只在真的要设新口令时**才校验（``None`` ⇒ 这一条没传）。

        「改个姓名也要重设口令」是把存量用户逼着改口令的最坏理由 ——
        历史口令可能只有 6 位，那种账号在管理员想改它别的字段时必须还能被改。
        """
        if v is None:
            return v
        problem = password_policy_error(v)
        if problem:
            raise ValueError(problem)
        return v


class SchedulerTaskPatch(BaseModel):
    enabled: StrictBool


def _apply_links(db: Session, user: SysUser, books: list[BookLinkIn]) -> None:
    # 入参现在是带类型的 ``BookLinkIn``（见上面那段），``book_id`` 缺失会
    # 在入口被 pydantic 拦成 422，不再有 KeyError → 500 的路。
    # 这里仍保留 ``.get`` 的写法：role 的合法性由 _check_roles 统一管，
    # 而账套不存在（合法 int 但库里没有）在这里静默跳过 —— 那是「授权一个
    # 不存在的账套」，与「payload 结构错了」不是一回事。
    valid = {
        b.book_id: b.role for b in books
        if db.get(Book, b.book_id) is not None
    }
    for link in db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all():
        if link.book_id not in valid:
            db.delete(link)
        else:
            link.role = valid.pop(link.book_id)
    for book_id, role in valid.items():
        db.add(UserBook(user_id=user.id, book_id=book_id, role=role))


def _check_roles(items: list[BookLinkIn]) -> None:
    for it in items:
        if it.role not in ROLES:
            raise HTTPException(400, f"未知角色「{it.role}」，可选：{'、'.join(ROLES)}")


@router.get("/users")
def list_users(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    # 这里能看到所有人的密码状态和账套授权，必须限管理员
    _require_admin(user)
    links = _links_by_user(db)
    rows = db.scalars(select(SysUser).order_by(SysUser.id)).all()
    return [_serialize(u, links.get(u.id, {})) for u in rows]


@router.post("/users", status_code=201)
def create_user(payload: UserIn, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db),
                _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """建号。**高危动作**，要求「近期重新认证」。

    **为什么建号也要二次验证**（审查 §3.2）：契约写在
    ``core/config.py:225`` —— 「管理员验过二次因子之后，这个窗口内做高危
    动作（改他人口令 / **装插件** / 读 OSS 凭据 / 轮换调度密钥 / 删用户）
    不再重复要求走邮箱」，而这一条**漏在契约之外**了。

    漏掉它的代价不是「权限更大」，而是**逃逸令牌有效期**：
    ``require_recent_admin_mfa`` 的全部意义就是把危险动作与「令牌刚被偷」
    解耦。一枚**没有 ``mfa_at``** 的管理员令牌（MFA 开关打开之前签发的，
    最长还有 4 小时有效）本来只能做「当次请求里的事」，但 ``POST /users``
    能**铸出一个比令牌活得久的新凭据** —— 新账号登录后不经过二次验证，
    于是那次偷来的令牌在 4 小时窗口之后**照样能进系统**。

    而**同一份权限**（账套角色）在 ``PUT /users/{id}/books`` 上是被正确
    拦住的：同一个洞的两条路，只堵了一条。

    二次验证开关关着时这个依赖**不拦人**（见
    ``core/deps.py::require_recent_admin_mfa``），所以挂着它不影响现有用户；
    开关开着而通道坏了时它**一律拒绝**（fail-closed）。
    """
    _require_admin(user)
    if db.scalar(select(SysUser).where(SysUser.username == payload.username)):
        raise HTTPException(400, f"用户名「{payload.username}」已存在")
    _check_roles(payload.books)
    u = SysUser(username=payload.username, password_hash=hash_password(payload.password),
                real_name=payload.real_name or payload.username, active=payload.active)
    db.add(u)
    db.flush()
    _apply_links(db, u, payload.books)
    # P2-03：建号过去不留痕。这是整个身份体系的起点动作 ——
    # 事后要能回答「这个人是谁、什么时候开的、一开始给了哪些账套」，
    # 而 `set_books` 那条路由是逐账套记授权变更的，等于「改了权限有日志、
    # 开号时给的初始权限没有」。这里把初始授权一并记进 after。
    db.flush()
    granted = {str(link.book_id): link.role for link in db.scalars(
        select(UserBook).where(UserBook.user_id == u.id)).all()}
    log(db, action="CREATE_USER", entity="user", entity_id=u.id, user=user,
        detail=f"创建用户 {u.username}（{'启用' if u.active else '停用'}）",
        after={**_user_snapshot(u), "books": granted})
    db.commit()
    db.refresh(u)
    return _serialize(u, {b.book_id: b.role for b in payload.books})


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserPatch, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db),
                _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """改他人口令 / 改账号状态 —— **高危动作**，要求「近期重新认证」。

    二次验证开关关着时这个依赖**不拦人**（见
    ``core/deps.py::require_recent_admin_mfa``），所以挂着它不影响现有用户；
    开关开着而通道坏了时它**一律拒绝**（fail-closed）—— 那时管理员本来就
    换不到新令牌，高危动作同样不该放行。
    """
    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    before = _user_snapshot(target)
    # R2-1：**停用**一个管理员与删掉它一样能锁死管理面 ——
    # 「最后一个能登录的管理员」被停用 ⇒ 没有人能开号、改口令、授权，
    # 而停用账号**登不进去**（``core/deps.py::get_current_user`` 会拒）。
    #
    # **这一条才是真正可达的那条路**：删除时执行者自己就是一个在用的管理员，
    # 算上他永远还剩至少一个，所以删除路径上那个守卫是**第二层**
    # （见 ``delete_user``）；而「把自己停用」可以把系统推到 0 个在用的
    # 管理员 —— 就在这一条路上。
    #
    # 判据与删除路径**共用同一个**（:func:`_active_admin_count`，只收一处）。
    if payload.active is False and target.is_admin and target.active:
        if _active_admin_count(db, exclude_id=target.id) == 0:
            raise HTTPException(
                409,
                "不能停用最后一个系统管理员：停用之后没有人能开号、改密码与授权，"
                "系统管理面将无人可用（唯一出口是运维跑脚本）。"
                "请先启用另一个管理员，再停用这一个。",
            )
    if payload.real_name is not None:
        target.real_name = payload.real_name
    if payload.active is not None:
        target.active = payload.active
    # P2-01：改密和停用都必须让**已签发的**令牌立刻作废。
    #
    # 过去这里只改 password_hash，而 JWT 是自签名的、服务端不存会话，
    # 鉴权只看签名 / 有效期 / active 三样 —— 于是「口令泄露后重置口令」这条
    # 最常用的处置实际上没生效：攻击者手里的旧令牌照常读写，最长再活 12 小时。
    # 停用之所以能挡，是因为 active 每次都被查；改密没有对应的那个字段，
    # 这次给它补上（token_version）。
    #
    # P1-8：**撤销与留痕必须成对出现**。原来的条件是
    # ``payload.active is not None`` 就 token_version += 1，而审计只在
    # 字段真的变了时才写 —— 于是 ``PATCH {"active": <当前值>}``（前端表单
    # 原样回传、或脚本里把现值当默认值带上）会静默作废该用户**所有**在用令牌，
    # 日志里一条没有。用户表现是「突然被踢下线」，运维查日志查不到任何东西。
    # 现在只要发生撤销就必写一条，下面的 revoked 就是这个开关。
    password_changed = bool(payload.password)
    if password_changed:
        target.password_hash = hash_password(payload.password)
    revoked = password_changed or payload.active is not None
    if revoked:
        target.token_version = (target.token_version or 0) + 1

    # P2-03：停用 / 启用 / 改密 / 改姓名过去全部直接提交，一条日志都没有。
    # 这类动作的影响面比授权还大（谁能进这个系统），却比授权更难追溯：
    # 「这个人昨天还好好的，今天登不进去了」在日志里没有任何线索。
    # 改密只记「变了」不记值 —— 见 _user_snapshot 的说明。
    did: list[str] = []
    if payload.active is not None and payload.active != before["active"]:
        did.append("启用账号" if payload.active else "停用账号")
    if password_changed:
        did.append("重置密码")
    if payload.real_name is not None and payload.real_name != before["real_name"]:
        did.append("修改姓名")
    if revoked:
        # 撤销无条件入账：哪怕这次提交一个字段都没变，**后果**也已经发生了。
        did.append(f"撤销该账号全部在用令牌（token_version → {target.token_version}）")
    if did:
        log(db, action="UPDATE_USER", entity="user", entity_id=target.id, user=user,
            detail=f"{'、'.join(did)}：{target.username}",
            before=before,
            after={**_user_snapshot(target), "password_changed": password_changed,
                   **({"token_version": target.token_version} if revoked else {})})
    db.commit()
    links = _links_by_user(db)
    return _serialize(target, links.get(target.id, {}))


@router.put("/users/{user_id}/books")
def set_books(user_id: int, books: list[BookLinkIn], user: SysUser = Depends(get_current_user),
              db: Session = Depends(get_db),
              _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """整份替换某用户的账套授权列表。**高危动作**，要求「近期重新认证」。"""

    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    _check_roles(books)
    # P3-10：授权变更过去不留痕。它是整个权限体系的入口动作 ——
    # 出了「谁看到了不该看的数据」这种事，第一条要查的就是授权什么时候、
    # 被谁、升到了什么角色。这份表按账套逐条记，且**逐个账套写一条**：
    # 授权的粒度是 (用户, 账套)，合并成一条就看不出是哪个账套被动了。
    before = {str(link.book_id): link.role for link in db.scalars(
        select(UserBook).where(UserBook.user_id == user_id)).all()}
    _apply_links(db, target, books)
    # 会话是 autoflush=False（app/core/db.py），_apply_links 刚 add 的
    # UserBook 在 flush 之前对查询不可见 —— 不 flush 的话 after 永远是空的，
    # 于是「新增授权」这种最该留痕的情况反而一条审计都不写。
    db.flush()
    after = {str(link.book_id): link.role for link in db.scalars(
        select(UserBook).where(UserBook.user_id == user_id)).all()}

    for book_id in set(before) | set(after):
        old_role, new_role = before.get(book_id), after.get(book_id)
        if old_role == new_role:
            continue
        if old_role and new_role:
            what = f"变更 {target.username} 对账套 {book_id} 的授权：{old_role} → {new_role}"
        elif new_role:
            what = f"授予 {target.username} 账套 {book_id} 的 {new_role} 权限"
        else:
            what = f"收回 {target.username} 对账套 {book_id} 的 {old_role} 权限"
        log(db, action="UPDATE_BOOK_ACCESS", entity="user_book",
            entity_id=f"{user_id}:{book_id}", user=user, book_id=int(book_id),
            detail=what,
            before={"user": target.username, "role": old_role},
            after={"user": target.username, "role": new_role})
    # **收回最后一份授权的账套，随这次变更一并删掉。**
    #
    # 一个授权用户都没有的账套，谁也打不开、谁也删不了，只能烂在库里 ——
    # 而它是**曾经有过**一本账（凭证、期初、导出过的报表）。留着它，
    # 账面上的历史数据和界面上能看到的账套列表就对不上了。
    #
    # 与授权变更在**同一个事务**里：删不掉就整体回滚，
    # 不会出现「授权已经收了、账套还在」的中间态。
    purge_unowned_books(db, user, [int(b) for b in set(before) - set(after)],
                         trigger=TRIGGER_REVOKE)
    db.commit()
    return _serialize(target, {b.book_id: b.role for b in books})


@router.delete("/users/{user_id}")
def delete_user(user_id: int, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db),
                _mfa: SysUser = Depends(require_recent_admin_mfa)):
    _require_admin(user)
    if user_id == user.id:
        raise HTTPException(400, "不能删除当前登录的用户")
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    # R2-1：**最后一个管理员不许删**。
    #
    # 删掉之后，建号、改口令、授权、读全量审计全部要管理员权限 ——
    # 而管理员已经没有了，**系统管理面就此锁死**：界面点不动、接口 403，
    # 唯一出口是运维跑脚本直接改库。
    #
    # **只挡管理员**：删一个**普通**账号不影响管理员的数量，那是允许的
    # （「删到只剩一个管理员」与「这个账号是不是管理员」是两件事）。
    # 判据在 :func:`_active_admin_count`（只收一处）。
    if target.is_admin and _active_admin_count(db, exclude_id=target.id) == 0:
        raise HTTPException(
            409,
            "不能删除最后一个系统管理员：删掉之后没有人能开号、改密码与授权，"
            "系统管理面将无人可用（唯一出口是运维跑脚本）。"
            "请先新建另一个管理员并给它授权，再删这一个。",
        )
    # P2-03：删除过去不留痕。删完之后连「曾经有过这么一个账号」都没地方查 ——
    # 而账号名册是要用来回答「这个账上的人都有谁」的。快照必须在 delete 之前取。
    before = _user_snapshot(target)
    before["books"] = {str(link.book_id): link.role for link in db.scalars(
        select(UserBook).where(UserBook.user_id == user_id)).all()}
    log(db, action="DELETE_USER", entity="user", entity_id=user_id, user=user,
        detail=f"删除用户 {target.username}", before=before)
    db.delete(target)
    # 这个人一删，他名下每个账套的授权也一并没了（sys_user_book 随用户级联）。
    # 「这个账套一个授权用户都没有了」这个事实要到这时候才成立 ——
    # 所以清理必须排在 delete 之后、commit 之前，与删用户在同一事务里。
    db.flush()
    purge_unowned_books(db, user, [int(b) for b in before["books"]],
                        trigger=TRIGGER_REVOKE)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: int, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db),
                _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """**提前解除锁定**，不等锁定自然到期。**高危动作**，要求「近期重新认证」。

    为什么必须有这个口子：递进式锁定本身是**可被主动触发的拒绝服务** ——
    知道用户名的人连错三次就能把账号挡在门外，而锁定时长会随轮次递增。
    审计 P2-3 把这条点成了"账号级锁定走的是和 IP 硬锁同一条路"：
    IP 硬锁会变成 DoS 放大器，账号硬锁也一样，只是范围小一点。

    所以锁定不能是"没人能解的状态"。这一条就是那个出口：
    管理员看一眼就知道是谁被锁了，一键解开，不必等 10 分钟 / 20 分钟 / …。

    **解封会把递进轮次清零**（``lock_level = 0``）：管理员明确介入过了，
    意味着「这个账号回到了正常状态」，下一轮该从 10 分钟重新算。
    不清零的话，被反复锁的账号会一路涨到上限，管理员每解一次都要重新
    走一遍 10→20→30 的阶梯。

    **为什么这一条要挂二次验证 —— 它是「放松」而不是「收紧」。**

    这一条是本组管理员写路由里**唯一**一个**解除安全限制**的动作（其余
    要么铸造凭据、要么改写凭据、要么改权限面）。它把**别人**账号上的
    「失败计数 + 锁定」清零，也就是把**针对那个账号的撞库防线**拆掉。

    于是：``POST /api/system/users/{id}/unlock`` 在 MFA 开着时若不设闸，
    一枚**刚被偷**的管理员令牌就能反复给某个受害者账号「解锁」，让另一个人
    对该账号的撞库**不受锁定限制**地继续下去 —— 而账号锁定是那条路径上
    **唯一**的防线（IP 维度只挡同一个来源，分布式喷洒挡不住）。

    与「吊销类路由不挂」并不矛盾：吊销是**收紧**（杀掉一个凭据），
    解锁是**放松**（拆掉一道防线）。方向相反，处置就该相反。

    **不会造成新的死锁**：被自己的锁定挡在门外的管理员**今天**也解不开
    自己（这条路由要求管理员身份，而管理员身份要先登录）—— 所以挂这道闸
    只是把「解封」这一步从「一次普通请求」变成「一次近期验证过的请求」，
    没有关掉任何原本能走通的出路。真正的逃生口仍然是
    ``scripts/mfa_admin.py --disable`` 与 ``ADMIN_MFA_EMERGENCY_CODE``。

    留痕：解锁是「谁在什么时候放行了谁」—— 这条在事后是要看的。
    """
    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    was_locked = lockout.is_locked(target)
    before = {**lockout.lock_summary(target)}
    target.locked_until = None
    target.failed_logins = 0
    target.lock_level = 0
    log(db, action="UNLOCK_USER", entity="user", entity_id=target.id, user=user,
        detail=f"提前解除锁定：{target.username}"
               + ("" if was_locked else "（该账号当时并未处于锁定中）"),
        before=before, after={**lockout.lock_summary(target)})
    db.commit()
    return {"ok": True, "was_locked": was_locked,
            "user": _serialize(target, _links_by_user(db).get(target.id, {}))}


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
    """**全局**审计流水，仅管理员。

    原注释写的是「普通用户只能看自己账套的（/books/{id}/audit-logs）」，
    读起来像是这个接口对普通用户是开放的、只是按范围过滤 —— 与实际行为不符：
    本行下面就是 ``_require_admin(user)``，非管理员一律 403。
    （``/books/{id}/audit-logs`` 那个接口同样挂着 ``require_admin``，
    「普通用户能看自己账套的审计」这件事**从来没实现过**，别照着注释去承诺。）

    这里坚持管理员而不是放宽给普通用户：审计快照是每次删除/修改的
    **完整凭证副本**，跨账套的全局流水更不用说 —— 会计本来就能读自己账套的
    数据，没道理再读走别人账套的操作痕迹。
    """
    _require_admin(user)
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc())
                      .limit(min(max(limit, 1), 500))).all()
    return [{"id": r.id, "username": r.username, "action": r.action, "entity": r.entity,
             "entity_id": r.entity_id, "detail": r.detail,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "before": r.before, "after": r.after} for r in rows]


# ------------------------------------------------------------ 管理员二次验证
#
# 底座在 ``app/notify/mfa.py``（开关 / 通道 / 挑战状态机），判定在
# ``core/deps.py::require_recent_admin_mfa``。本节只做两件事：
#
# 1. **让管理员能看见当前状态并开关它**（``GET/PUT /api/system/mfa``）。
# 2. **把高危动作挂上「近期重新认证」**（见下面各路由的 ``_mfa`` 依赖）。
#
# **响应里绝不含任何口令/密钥**：只有「开没开」「窗口多少分钟」「SMTP 通道
# 就绪没有」「收件人数量」「脱敏后的收件地址」这类可展示信息。


class MfaConfigPatch(BaseModel):
    """二次验证开关。``StrictBool`` —— 与自助注册开关同一口径。"""

    enabled: StrictBool


def _mfa_view(db: Session) -> dict:
    """二次验证的**可展示状态**。

    三个判定口径不要混用（见 ``app/notify/mfa.py``）：
    ``enabled`` 是**开关**，``smtp_ready`` 是**通道**，
    ``active`` 是「此刻真的生效」（开关 **且** 通道）。
    """
    enabled = mfa_service.mfa_enabled(db)
    ready = mfa_service.smtp_ready(db)
    recipients = notify_config.recipients(db)
    return {
        "enabled": enabled,
        "active": bool(enabled and ready),
        "grace_minutes": mfa_service.grace_seconds() // 60,
        "smtp_ready": ready,
        "recipient_count": len(recipients),
        # 脱敏后的第一个收件地址（``a***@x.com``）—— 只为让管理员确认
        # 「信会发到哪儿」，不泄露完整地址。
        "recipient_hint": mfa_service.email_hint(db),
        # 「开关开着但通道没配好」时的一段人话；一切正常时是空串。
        "channel_error": mfa_service.channel_error(db),
    }


@router.get("/mfa")
def get_mfa_config(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """二次验证的当前状态。**只读，不含任何凭据。**"""
    _require_admin(user)
    return _mfa_view(db)


@router.put("/mfa")
def put_mfa_config(payload: MfaConfigPatch, user: SysUser = Depends(get_current_user),
                    db: Session = Depends(get_db),
                    _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """开关管理员二次验证。

    **开关打开而 SMTP 通道没就绪 ⇒ 明确拒绝（409）**，不让管理员点一下开关
    就把所有管理员锁在门外：二次验证的邮件发不出去 ⇒ 管理员登不进来 ⇒
    管理面锁死 —— 与 R2-1 是同一种后果，而且发作得更快（当场）。
    界面据此劝阻管理员「先配 SMTP」。

    **关掉在业务规则上永远允许** —— 那是从「上锁」回到「不上锁」，是安全
    方向的退让，不该被任何**通道**条件挡住。但「近期重新认证」那道依赖
    仍然在：刚登录完（那次登录带二次验证）就能关；超过验证窗口还想关，
    走 ``scripts/mfa_admin.py --disable``（明确关掉，而不是假装开着）。
    """
    _require_admin(user)
    before = _mfa_view(db)
    if payload.enabled and not mfa_service.smtp_ready(db):
        raise HTTPException(
            409,
            "SMTP 通道还没配好，开不了二次验证：验证链接要发到管理员邮箱，"
            "而现在「" + (before["channel_error"] or "SMTP 不可用") + "」。"
            "请先到「邮件通知」配好 SMTP 服务器与收件人，再来开这个开关。",
        )
    mfa_service.set_mfa_enabled(db, payload.enabled, updated_by=user.username)
    # 必须 flush 之后才能取到改后的值（见 put_ocr_config 里的同一段）。
    db.flush()
    after = _mfa_view(db)
    log(
        db,
        action="SYSTEM_MFA_CONFIG_UPDATE",
        entity="system_setting",
        entity_id=mfa_service.MFA_ENABLED_KEY,
        user=user,
        detail=("开启" if payload.enabled else "关闭") + "管理员二次验证",
        before={"enabled": before["enabled"], "smtp_ready": before["smtp_ready"]},
        after={"enabled": after["enabled"], "smtp_ready": after["smtp_ready"]},
    )
    db.commit()
    return after


# ------------------------------------------------------- 登录防护状态（可观测）
#
# 审计 P2-3：「``TRUSTED_PROXIES`` 没配 ⇒ IP 维度节流退化成全站一个桶」。
# 那个状态**本来就存在**（代码是 fail-closed 的：不配就不信代理头），问题在于
# 它是**隐形的** —— 没人知道当前部署里「所有请求都算作同一个来源」。
#
# 这里把它**显式化**：管理员在界面上能直接看到「当前节流按什么分桶」，
# 以及「是不是全站一个桶」。口径在 ``core/lockout.py``（只收一处）。


@router.get("/login-protection")
def get_login_protection(user: SysUser = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """登录防护的当前形态（分桶口径、账号锁定阶梯、是否共享一个桶）。

    **只读**，不含任何凭据。它回答的是「这套系统现在防得住什么、防不住什么」。
    """
    _require_admin(user)
    return lockout.protection_summary()


# ------------------------------------------------------------ 自助注册开关


class RegistrationPatch(BaseModel):
    """自助注册开关。

    ``StrictBool``：这里不接受 ``"false"`` 这种字符串。布尔开关最常见的事故
    不是写错值，而是**写了个非布尔值然后被当成真**（``bool("false") is True``）。
    用严格布尔把它挡在入口，报错也比事后发现「明明关了却还能注册」清楚。
    """

    enabled: StrictBool


def _registration_view(db: Session) -> dict:
    return {"registration_enabled": system_settings.registration_enabled(db)}


@router.get("/registration")
def get_registration(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    return _registration_view(db)


@router.put("/registration")
def put_registration(payload: RegistrationPatch, user: SysUser = Depends(get_current_user),
                     db: Session = Depends(get_db),
                     _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """改自助注册开关。**留痕**：这个开关决定「陌生人能不能往 sys_user 里加行」，
    打开它的那一次必须有据可查（谁、何时、从关到开）。

    **这是一个高危动作**（审计 P2-2），所以要「近期重新认证」：开关一开，
    任何人都能往账号表里加行，而注册接口**目前没有独立的节流**（节流与
    「用户名已存在 / 注册成功」的文案抹平都要落在 ``api/auth.py`` 上，不在
    本文件里）。一个被别人拿到的管理员令牌就能把这扇门打开。

    **开关关着时，注册端点应当根本不存在而不是拒绝** —— 那一半在
    ``api/auth.py``（路由注册与否），本文件只负责把开关的口径收紧到这里。
    """
    _require_admin(user)
    before = system_settings.registration_enabled(db)
    system_settings.set_registration_enabled(db, payload.enabled, updated_by=user.username)
    log(
        db,
        action="SYSTEM_REGISTRATION_UPDATE",
        entity="system_setting",
        entity_id=system_settings.REGISTRATION_KEY,
        user=user,
        detail=("开放" if payload.enabled else "关闭") + "自助注册",
        before={"registration_enabled": before},
        after={"registration_enabled": payload.enabled},
    )
    db.commit()
    return _registration_view(db)


# ------------------------------------------------------------ 外部服务（OCR）


class OcrConfigPatch(BaseModel):
    """OCR 服务配置。

    ``api_token`` 是**只写**字段：读接口永远不回明文。这里也不允许「传空串 =
    清空」—— 想清空要走 ``clear_token=True``，否则界面上一个没填内容的输入框
    一次普通保存就把别人的 token 悄悄抹掉了，而界面上看不出任何区别。
    """

    api_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=64)
    api_token: Optional[str] = Field(default=None, max_length=512)
    timeout_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    poll_seconds: Optional[float] = Field(default=None, ge=0.5, le=60)
    optional_payload: Optional[str] = Field(default=None, max_length=1024)
    clear_token: bool = False


@router.get("/ocr")
def get_ocr_config(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """返回 OCR 配置，**但没有 token 明文** —— 只有 ``token_set`` 与末 4 位。

    管理员要能回答「到底配没配上」，但不需要把 token 再读出来贴到别处；
    能读出来就意味着它会出现在浏览器历史、网络面板和一次误开的调试窗口里。
    """
    _require_admin(user)
    return system_settings.ocr_config_public(db)


@router.put("/ocr")
def put_ocr_config(payload: OcrConfigPatch, user: SysUser = Depends(get_current_user),
                   db: Session = Depends(get_db),
                   _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """改外部识别服务配置（**含第三方 api_token**）。**高危动作**。

    **为什么挂「近期重新认证」**：``api_token`` 是**第三方服务凭据** ——
    能花钱、能调用别人的 OCR 服务、能被换掉（改掉之后所有识别请求都发到
    攻击者的服务上去，而发票数据也随之而去）。改写凭据与「铸造凭据」
    同档（§3.2 的口径：「危险动作与『令牌刚被偷』解耦」）。

    与 ``PUT /api/system/backup``（写 OSS AccessKey）同一档 —— 那条早
    就挂着这道依赖；这里是对同一条判据的补齐。

    **读接口不挂**：``GET /api/system/ocr`` 只回「配没配」与末 4 位，
    管理员在没做二次验证时也该能看见配置状态。
    """
    _require_admin(user)
    before = system_settings.ocr_config(db)
    updates: dict[str, object] = {}
    for field, key in (("api_url", "ocr_api_url"), ("model", "ocr_model"),
                       ("timeout_seconds", "ocr_timeout_seconds"),
                       ("poll_seconds", "ocr_poll_seconds"),
                       ("optional_payload", "ocr_optional_payload")):
        value = getattr(payload, field)
        if value is not None:
            updates[key] = value

    if payload.clear_token:
        system_settings.set_value(db, "ocr_api_token", "", updated_by=user.username)
    elif payload.api_token:
        system_settings.set_value(db, "ocr_api_token", payload.api_token, updated_by=user.username)

    for key, value in updates.items():
        system_settings.set_value(db, key, str(value), updated_by=user.username)

    # 必须 flush 之后才能取 after 快照。本工程的 session 是 autoflush=False
    # （见 app/core/db.py），而 Session.get() 找不到还没 flush 的 pending 行 ——
    # 于是这里读到的是「改之前」的值，审计日志会记一条 before == after 的假变更，
    # 恰好毁掉「谁把 token 换成什么」这条唯一可查的线索。
    db.flush()
    after = system_settings.ocr_config(db)
    # 留痕，但不记值：审计日志页所有管理员都能翻，明文进日志等于二次泄露。
    log(
        db,
        action="SYSTEM_OCR_CONFIG_UPDATE",
        entity="system_setting",
        entity_id="ocr",
        user=user,
        detail="更新外部识别服务配置" + ("（含 token）" if (payload.api_token or payload.clear_token) else ""),
        before={"token": system_settings.mask_secret(before["token"]),
                "api_url": before["api_url"], "model": before["model"]},
        after={"token": system_settings.mask_secret(after["token"]),
               "api_url": after["api_url"], "model": after["model"]},
    )
    db.commit()
    return system_settings.ocr_config_public(db)


# ------------------------------------------------- 邮件通知（SMTP）


class NotifyConfigPatch(BaseModel):
    """邮件通知配置。

    口令是**只写**的，语义与 OCR token / OSS AccessKey 一致：
    想清空要走 ``clear_password=True``。否则界面上一个没填内容的输入框
    一次普通保存就把凭据悄悄抹掉了，而界面上看不出任何区别 ——
    而抹掉之后通知会静默地发不出去（那正是最该被发现的故障）。
    """

    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, max_length=255)
    from_address: Optional[str] = Field(default=None, max_length=255)
    use_ssl: Optional[bool] = None
    use_tls: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=60)
    recipients: Optional[str] = Field(default=None, max_length=2000)
    enabled: Optional[bool] = None
    clear_password: bool = False

    @field_validator("recipients")
    @classmethod
    def _check_recipients(cls, v: Optional[str]) -> Optional[str]:
        """收件人逐个校验，但**不因为有一个不合法就整份拒绝**。

        这里与别处的严格校验不同：收件人是一串地址，其中一个写错不该
        让另外几个也配不上（那等于「一个人填错，全组人收不到」）。
        不合法的项在真正发送时被 :func:`notify_config.parse_recipients`
        跳过，界面上会显示有效收件人数，让管理员自己看出少了谁。

        但**换行是硬拒** —— 收件人字段会被拼进邮件头，换行等于邮件头注入
        （「一封信发到别处去」）。这一条不能靠"管理员不会这么填"来防。
        """
        if v is None:
            return None
        if "\r" in v or "\n" in v:
            raise ValueError("收件人列表不能包含换行")
        return v.strip()


@router.get("/notify")
def get_notify_config(user: SysUser = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """返回通知配置，**但没有口令明文** —— 只有 ``password_set`` 与末 4 位。

    与 OCR / OSS 那两个接口同一条口径：管理员要能回答「到底配没配上」，
    但不需要把口令再读出来贴到别处。
    """
    _require_admin(user)
    return notify_config.smtp_config_public(db)


@router.put("/notify")
def put_notify_config(payload: NotifyConfigPatch,
                      user: SysUser = Depends(get_current_user),
                      db: Session = Depends(get_db),
                      _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """改邮件通知配置（SMTP 服务器/账号/口令 + **收件人**）。**高危动作**。

    **为什么它必须是高危动作 —— 这一条就是二次验证本身的开关。**

    管理员二次验证邮件**没有 per-user 收件人**（``sys_user`` 表里没有邮箱列），
    全部统一发给全局 ``notify_config.recipients``。于是「谁能改收件人」就等于
    「谁能收到下一次二次验证邮件」。

    实测跑通的完整链条（复核方 2026-10-25，**修复前**）::

        [旁路 0] 建号(受管控)              -> 403「该操作需要近期完成邮箱二次验证…」
        [旁路 1] PUT /api/system/notify     -> 200（这条路由没挂二次验证）
        [旁路 1] 现在的收件人 = ['attacker@evil.test']
        [旁路 2] POST /api/auth/mfa/reauth  -> 200，邮件真的发给了攻击者
        [旁路 3] 点真链接                     -> 200
        [旁路 4] POST /api/auth/mfa/poll     -> 200，拿到一枚带 mfa_at 的令牌
        [旁路 5] 建号(受管控)              -> 201，账号真的建出来了

    也就是说：``require_recent_admin_mfa`` 挡住的那条路，可以从「改收件人」
    这条侧翼整个绕过去 —— 而「改收件人」恰恰是**让二次验证生效的那个开关**。

    **两道锁，缺一不可**：

    1. **本路由挂上「近期重新认证」** —— 没有 ``mfa_at`` 的令牌改不了收件人
       （上面前两步就被 403 打断）；
    2. **投递目标一变，作废所有未完成的挑战**（见
       :func:`app.notify.mfa.expire_open_challenges`）—— 挡住「先改收件人、
       再等一个旧挑战被点开」这条路径。

    **口令与 SMTP 服务器也在同一条路由上**：改 ``smtp_password`` 就是
    **改写第三方凭据**，改 ``smtp_host`` 就是**换掉二次验证的投递通道** ——
    两者都与「铸造/改写凭据」同档，所以一起挂这道闸。

    **可用性上的取舍（刻意选 fail-closed）**：二次验证开着而 SMTP 通道坏了
    时，这条路由也会被挡下（403），管理员**改不了** SMTP 来修通道。此时的
    逃生口与其它高危路由一致 —— ``scripts/mfa_admin.py --disable``（明确关掉）
    与进程环境里的 ``ADMIN_MFA_EMERGENCY_CODE``。理由是：这条路由一旦在
    「通道坏了」的状态下放开，攻击者就能用它把通道**配到自己的服务器上**，
    然后整道 §3.2 的闸门从侧翼被击穿 —— 那正是本次修掉的那个洞。

    审计：``SYSTEM_NOTIFY_CONFIG_UPDATE`` 记 before/after（含 ``recipients``
    与 ``password_hint``）；投递目标变更导致的挑战作废另记一条
    ``SYSTEM_MFA_CHALLENGES_INVALIDATED``。
    """
    _require_admin(user)
    before = notify_config.smtp_config_public(db)
    updates: dict[str, str] = {}
    for field, key in (("host", "smtp_host"), ("port", "smtp_port"),
                       ("username", "smtp_username"), ("from_address", "smtp_from"),
                       ("use_ssl", "smtp_use_ssl"), ("use_tls", "smtp_use_tls"),
                       ("timeout_seconds", "smtp_timeout_seconds")):
        value = getattr(payload, field)
        if value is not None:
            updates[key] = str(value)

    if payload.clear_password:
        system_settings.set_value(db, "smtp_password", "", updated_by=user.username)
    elif payload.password:
        system_settings.set_value(db, "smtp_password", payload.password,
                                  updated_by=user.username)

    for key, value in updates.items():
        system_settings.set_value(db, key, value, updated_by=user.username)

    if payload.recipients is not None:
        notify_config.set_recipients(db, payload.recipients, updated_by=user.username)
    if payload.enabled is not None:
        notify_config.set_notify_enabled(db, payload.enabled, updated_by=user.username)

    # 必须 flush 之后才能取 after 快照。本工程的 session 是 autoflush=False
    # （见 app/core/db.py），而 Session.get() 找不到还没 flush 的 pending 行 ——
    # 于是这里读到的是「改之前」的值，审计日志会记一条 before == after 的假变更。
    db.flush()
    after = notify_config.smtp_config_public(db)
    log(
        db,
        action="SYSTEM_NOTIFY_CONFIG_UPDATE",
        entity="system_setting",
        entity_id="notify",
        user=user,
        detail="更新邮件通知配置"
               + ("（含密码）" if (payload.password or payload.clear_password) else ""),
        before={k: before.get(k) for k in
                ("host", "port", "username", "password_hint",
                 "from_address", "use_ssl", "use_tls", "recipients", "enabled")},
        after={k: after.get(k) for k in
               ("host", "port", "username", "password_hint",
                "from_address", "use_ssl", "use_tls", "recipients", "enabled")},
    )

    # ---- 投递目标变了 ⇒ 此前发出去的一切二次验证邮件都不再可信 ----------
    #
    # 收件人换了、或者换掉了发信服务器（SMTP host）：此前**已经发出**的
    # 验证邮件是按旧投递目标投出去的 —— 截获了它的人仍然能点开那条链接。
    # 所以这里把**所有**未完成的挑战（PENDING 与 VERIFIED）作废。
    #
    # 只在「投递目标真的变了」时做：改个端口、改个超时不该把别人正在
    # 进行的登录打断掉。
    delivery_changed = (
        before.get("recipients") != after.get("recipients")
        or before.get("host") != after.get("host")
    )
    if delivery_changed:
        expired = mfa_service.expire_open_challenges(
            db, reason="邮件通知投递目标变更")
        log(
            db,
            action="SYSTEM_MFA_CHALLENGES_INVALIDATED",
            entity="system_setting",
            entity_id="notify",
            user=user,
            detail=f"邮件通知投递目标变更，作废未完成的二次验证挑战 {expired} 条",
            before={"recipients": before.get("recipients"), "host": before.get("host")},
            after={"recipients": after.get("recipients"), "host": after.get("host"),
                   "expired_challenges": expired},
        )
    db.commit()
    return after


@router.post("/notify/test")
def test_notify(user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db),
                _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """发一封测试信。**高危动作**，要求「近期重新认证」。

    **收件人就是真实的收件人** —— 收不到信的地址就是配错了，而
    「配错了却显示测试成功」是最没用的测试结果。

    **为什么它也要挂二次验证**：它用的是**当前**收件人 —— 也就是「改完
    收件人之后，验证一下信能不能发出去」。这正是上面那条旁路链条里的
    「投递通道可用性确认」那一环：没有这道闸，一个刚改完收件人的攻击者
    就能确认自己的地址真的收得到信。
    """
    _require_admin(user)
    config = notify_config.smtp_config(db)
    targets = notify_config.recipients(db)
    if not targets:
        raise HTTPException(400, "还没有配置收件人邮箱")
    if not (config.get("host") or "").strip():
        raise HTTPException(400, "还没有配置 SMTP 服务器地址")
    try:
        count = notify_mailer.test_connection(config, targets)
    except notify_mailer.MailError as exc:
        # 发不出去要说清原因，但**不能把口令回显**。MailError 的消息里
        # 本来就不含口令（mailer.py 里刻意不拼它），这里原样透出即可。
        raise HTTPException(400, f"测试发送失败：{exc}") from exc
    log(db, action="SYSTEM_NOTIFY_TEST", entity="system_setting", entity_id="notify",
        user=user, detail=f"发送测试邮件给 {count} 个收件人")
    db.commit()
    return {"ok": True, "recipients": count}


# ------------------------------------------------- 数据安全（OSS 备份）


class BackupConfigPatch(BaseModel):
    """备份配置。两个 AccessKey 项都是**只写**的，语义与 OCR 的 token 一致。

    ``access_key_secret`` / ``access_key_id`` 不接受「传空串 = 清空」：
    想清空要走 ``clear_access_key=True``。否则界面上一个没填内容的输入框
    一次普通保存就把凭据悄悄抹掉了，而界面上看不出任何区别 ——
    而抹掉之后，下一次定时备份会失败，而失败要等到第二天才被发现。
    """

    endpoint: Optional[str] = Field(default=None, max_length=256)
    bucket: Optional[str] = Field(default=None, max_length=128)
    prefix: Optional[str] = Field(default=None, max_length=128)
    access_key_id: Optional[str] = Field(default=None, max_length=256)
    access_key_secret: Optional[str] = Field(default=None, max_length=256)
    enabled: Optional[bool] = None
    include_system: Optional[bool] = None
    keep_daily_days: Optional[int] = Field(default=None, ge=1, le=3650)
    keep_weekly_days: Optional[int] = Field(default=None, ge=1, le=36500)
    clear_access_key: bool = False

    @field_validator("prefix")
    @classmethod
    def _clean_prefix(cls, v: Optional[str]) -> Optional[str]:
        # 前缀去掉首尾斜杠：``/a/`` 与 ``a`` 在 OSS 上是两个不同的目录，
        # 而用户多半随手打个斜杠。与其在清理时两处都判，不如在存的时候就定死。
        if v is None:
            return None
        return v.strip().strip("/")


def _backup_view(db: Session) -> dict:
    view = backup_config.oss_config_public(db)
    view["enabled"] = backup_config.is_enabled(db)
    view["include_system"] = backup_config.include_system(db)
    view["keep_days"] = backup_config.retention_days(db)
    # 月度恒为永久（0），页面据此把「永久」显示成不可编辑 —— 见 retention.py。
    view["monthly_forever"] = True
    return view


@router.get("/backup")
def get_backup_config(user: SysUser = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    _require_admin(user)
    return _backup_view(db)


@router.put("/backup")
def put_backup_config(payload: BackupConfigPatch, user: SysUser = Depends(get_current_user),
                      db: Session = Depends(get_db),
                      _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """写 OSS 配置（含 AccessKey）。**高危动作**，要求「近期重新认证」。

    **为什么这一条是高危**：AccessKey 能写桶，而桶里放着**全部**备份制品
    —— 拿到它就等于拿到全公司账务数据。写错一个字符也足以把备份导到别处去。
    """
    _require_admin(user)
    # P1-1：**保存时就拒掉明文 endpoint**，不要等到凌晨三点备份失败才发现。
    # 归一化只管「形状」，「能不能这么用」在这里判 —— 报错文案自带改成什么。
    if payload.endpoint is not None:
        try:
            oss_client.ensure_secure_endpoint(payload.endpoint)
        except oss_client.OssError as exc:
            raise HTTPException(400, str(exc)) from exc
    before = _backup_view(db)
    updates: dict[str, str] = {}
    for field, key in (("endpoint", "oss_endpoint"), ("bucket", "oss_bucket"),
                       ("prefix", "oss_prefix")):
        value = getattr(payload, field)
        if value is not None:
            updates[key] = value
    if payload.keep_daily_days is not None:
        updates["backup_keep_daily_days"] = str(payload.keep_daily_days)
    if payload.keep_weekly_days is not None:
        updates["backup_keep_weekly_days"] = str(payload.keep_weekly_days)
    if payload.enabled is not None:
        updates["backup_enabled"] = "1" if payload.enabled else "0"
    if payload.include_system is not None:
        updates["backup_include_system"] = "1" if payload.include_system else "0"

    if payload.clear_access_key:
        for key in ("oss_access_key_id", "oss_access_key_secret"):
            system_settings.set_value(db, key, "", updated_by=user.username)
    else:
        if payload.access_key_id:
            system_settings.set_value(db, "oss_access_key_id", payload.access_key_id,
                                      updated_by=user.username)
        if payload.access_key_secret:
            system_settings.set_value(db, "oss_access_key_secret", payload.access_key_secret,
                                      updated_by=user.username)

    for key, value in updates.items():
        system_settings.set_value(db, key, value, updated_by=user.username)

    db.flush()   # 见 put_ocr_config 里的同一段：flush 之后才能取到改后的值
    after = _backup_view(db)
    log(
        db,
        action="SYSTEM_BACKUP_CONFIG_UPDATE",
        entity="system_setting",
        entity_id="backup",
        user=user,
        detail="更新数据安全配置"
               + ("（含 AccessKey）" if (payload.access_key_id or payload.access_key_secret
                                          or payload.clear_access_key) else ""),
        before={k: before.get(k) for k in
                ("endpoint", "bucket", "prefix", "access_key_id_hint",
                 "access_key_secret_hint", "enabled", "keep_days")},
        after={k: after.get(k) for k in
               ("endpoint", "bucket", "prefix", "access_key_id_hint",
                "access_key_secret_hint", "enabled", "keep_days")},
    )
    db.commit()
    return after


@router.post("/backup/test")
def test_backup_connection(user: SysUser = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """写一个探针对象再删掉 —— 验的是「能不能写」，那才是备份唯一要紧的事。

    **这里只验连通，不要求加密密钥。** 「能不能写」与「备份跑不跑得起来」
    是两件事：正在配 OSS 的人会先测连通性，这时让他先配一把密钥才能测，
    是在流程中间横插一道与本按钮无关的关卡。

    但**「备份跑不起来」这件事必须在这里就说出来** —— 连通性测试通过、
    第二天早上备份因为缺密钥而整轮失败，是最坏的一种组合
    （界面说「连接正常」，运维就以为退路是好的）。
    所以响应里带上 ``encryption_ready``，并在消息里说清楚。
    """
    _require_admin(user)
    try:
        cfg = backup_config.require_ready(db)
        message = oss_client.check_connection(cfg)
    except (backup_config.ConfigIncomplete, oss_client.OssError) as exc:
        raise HTTPException(400, str(exc)) from exc

    encryption_ready = backup_config.encryption_ready()
    if not encryption_ready:
        message = (f"{message}；但**备份加密密钥没配**（BACKUP_ENCRYPTION_KEY）—— "
                   "此时点「立即备份」会被拒绝，备份制品也不会是密文。"
                   "配一把：python -c \"import base64,os;"
                   "print(base64.b64encode(os.urandom(32)).decode())\"")
    return {"ok": True, "message": message, "encryption_ready": encryption_ready}


@router.post("/backup/run")
def trigger_backup(
    background: BackgroundTasks,
    user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """立刻跑一轮备份。**立即返回，后台跑。**

    同步跑会占住一个 HTTP 连接直到全部账套传完 —— 几本书几分钟，而这期间
    页面上的「备份中」是靠超时猜的。后台任务 + 轮询 ``/backup/runs`` 才能让
    界面如实反映进度（跑几个了、哪本失败了），而不是转圈转到超时。

    后台任务里**另开一个会话**：请求结束时会话就被关掉了，继续用它写
    ``sys_backup_run`` 会在提交时抛 ``Session is closed``。
    """
    _require_admin(user)
    username = user.username
    user_id = user.id
    try:
        # **要求制品一定是密文**（按**生效配置**判，界面上配 OSS 的那条路也在内）。
        # 启动期那道闸只看得到 env 层 —— 启动时没有库连接。
        backup_config.require_encrypted_ready(db)
    except backup_config.ConfigIncomplete as exc:
        raise HTTPException(400, str(exc)) from exc

    def _work():
        session = SessionLocal()
        try:
            actor = session.get(SysUser, user_id)
            backup_service.run_backup(session, actor=actor or username)
        except Exception as exc:                     # noqa: BLE001
            # 后台任务里的异常会被 Starlette 吞掉，只写进 stderr；界面上会一直
            # 停在「没有新记录」而看不出为什么。落一行失败记录，让它可见。
            #
            # **记录里必须带真实原因。** 原来这里写的是「后台任务异常，见服务端
            # 日志」—— 那九个字让排障从「读记录」变成「考古日志」：出过一次真实
            # 事故（数据全传上去了、界面说整轮失败），真实原因就藏在那句话后面。
            # 一个只在服务端日志里的原因，等于没有原因。
            session.add(BackupRun(
                book_id=None, scope=SCOPE_SYSTEM, book_label="（整轮）",
                tier=TIER_DAILY, status=STATUS_FAILED,
                message=f"后台任务异常：{type(exc).__name__}: {exc}",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            ))
            session.commit()
            raise
        finally:
            session.close()

    background.add_task(_work)
    return {"ok": True, "message": "已开始备份，完成后可在下方记录里看到结果。"}


@router.get("/backup/runs")
def list_backup_runs(limit: int = 50, user: SysUser = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    _require_admin(user)
    return [_run_view(r) for r in backup_service.recent_runs(db, limit)]


_SCHEDULER_BLOCK_REASONS = {
    "PLATFORM_DISABLED": "扩展平台未开启",
    "STALE_GENERATION": "服务正在切换插件代次，请稍后刷新",
    "OLD_WORKERS_ACTIVE": "仍有旧进程持有插件，请稍后刷新",
    "WORKER_BARRIER_PENDING": "插件尚未就绪，请稍后刷新",
    "RESTART_REQUIRED": "插件安装包已变更，需重启服务后重新启用",
    "PACKAGE_MISSING": "插件安装包缺失",
    "LOAD_FAILED": "插件加载失败",
}


def _task_switchable(db, row, runtime):
    """这个任务的开关现在能不能点，以及不能点的**原因**。

    提前算好是给界面用的：让人在按钮上直接看到「为什么灰着」，
    而不是点下去吃一个 409 再去猜哪里不对。真正的准入判断仍在
    ``set_scheduler_plugin_task`` 里按当刻状态重做一遍 —— 这里只是把
    结论提前告诉前端，不构成放行。
    """
    if row.owner_type != "PLUGIN":
        return False, "系统任务，不能在此开关"
    plugin = db.get(PluginRegistry, row.plugin_id)
    if plugin is None or plugin.deleted_at is not None:
        return False, "插件已卸载"
    if not plugin.desired_enabled:
        return False, "插件本身未启用，请先在扩展管理里启用"
    if runtime is None:
        return False, "等待服务加载插件"
    current, code = plugin_status(db, plugin, runtime.generation)
    if current != "READY":
        return False, _SCHEDULER_BLOCK_REASONS.get(code, "插件当前不可用（%s）" % (code or current))
    return True, ""


@router.get("/scheduler/tasks")
def list_scheduler_tasks(request: Request, user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    now = scheduler_service.utcnow()
    runtime = getattr(request.app.state, "plugin_runtime", None)
    scheduler_service.sync_tasks(db, runtime, now)
    db.commit()
    result = []
    for row in db.scalars(select(ScheduledTask).order_by(ScheduledTask.task_key)):
        available, reason = _task_switchable(db, row, runtime)
        result.append({"task_key": row.task_key, "owner_type": row.owner_type, "plugin_id": row.plugin_id,
                       "task_name": row.task_name, "schedule": row.schedule, "enabled": row.enabled,
                       "next_run_at": row.next_run_at.isoformat(),
                       "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
                       "available": available, "unavailable_reason": reason})
    return result


@router.put("/scheduler/tasks/{plugin_id}/{task_name}")
def set_scheduler_plugin_task(plugin_id: str, task_name: str, payload: SchedulerTaskPatch,
                              request: Request, user: SysUser = Depends(get_current_user),
                              db: Session = Depends(get_db),
                              _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """开关一个**插件**的定时任务。**高危动作**，要求「近期重新认证」。

    **为什么挂这道闸**：插件任务跑在**宿主进程里**（除非开了插件隔离，
    见 ``PLUGIN_ISOLATION``），所以「让某个插件任务开始/停止跑」=「让一段
    外部代码在宿主里开始/停止执行」。它改的是**执行面**，与「铸造凭据」
    同属一类需要与「令牌刚被偷」解耦的动作。

    对比 ``POST /api/system/scheduler/secret/rotate``（轮换外部调度触发
    密钥）—— 那条早挂着这道依赖；这里是同一条判据的补齐。

    **只开关插件自己的任务**：系统任务（``owner_type != "PLUGIN"``）在这一条
    路由上根本改不动（404），所以这里不会变成「关掉全站备份计划」的后门。
    """
    _require_admin(user)
    now = scheduler_service.utcnow()
    scheduler_service.sync_tasks(db, getattr(request.app.state, "plugin_runtime", None), now)
    row = db.get(ScheduledTask, scheduler_service.task_key(plugin_id, task_name))
    if not row or row.owner_type != "PLUGIN":
        raise HTTPException(404, "插件定时任务不存在或 worker 尚未加载最新版本")
    was_enabled = row.enabled
    if payload.enabled:
        plugin = db.get(PluginRegistry, plugin_id)
        runtime = getattr(request.app.state, "plugin_runtime", None)
        if not plugin or not plugin.desired_enabled or runtime is None:
            raise HTTPException(409, "插件当前未启用")
        current, code = plugin_status(db, plugin, runtime.generation)
        if current != "READY":
            raise HTTPException(409, {"code": code or current, "message": "插件 worker 尚未就绪"})
        task = runtime.tasks.get(plugin_id, {}).get(task_name)
        if task is None or task[2] != row.plugin_digest:
            raise HTTPException(409, "当前插件版本没有注册该定时任务")
        if not row.enabled:
            row.next_run_at = scheduler_service._next(row.schedule, now, include_current_minute=True)
    row.enabled = payload.enabled
    # **entity_id 必须收敛长度**（审查 §3.21）。
    #
    # ``audit_log.entity_id`` 是 ``String(64)``，而 ``row.task_key`` 最长可达
    # **152 字符**（``scheduler/service.py`` 里是 ``f"plugin:{plugin_id}:{name}"``，
    # plugin_id 上限 96、任务名上限 48）。原来把 task_key 整个塞进去 →
    # 库层 sqlstate **22001** → **整个事务回滚**：开关的修改被撤销，
    # 审计行也一并消失（连「有人试图改过」都没留下），而全局异常处理器
    # 把它翻成 400「提交的内容超出了字段允许的长度」—— 与真正的病因
    # 毫无关系。
    #
    # 口径与全仓其余写入点一致（``scheduler/service.py`` 的 ``_marker_id``、
    # ``plugins/audit.py`` 的 ``plugin_id[:64]``、``api/auth.py`` 的
    # ``username[:64]``）：**散列到 32 位**。完整的 task_key 放进
    # ``detail`` 与 ``after`` —— 一个字都不丢，只是换了个字段放。
    entity_id = hashlib.sha256(row.task_key.encode()).hexdigest()[:32]
    log(db, action="SCHEDULER_TASK_UPDATE", entity="scheduled_task", entity_id=entity_id,
        user=user, detail=f"更新插件定时任务开关：{row.task_key}",
        before={"enabled": was_enabled, "task_key": row.task_key},
        after={"enabled": row.enabled, "task_key": row.task_key})
    db.commit()
    return {"task_key": row.task_key, "enabled": row.enabled,
            "next_run_at": row.next_run_at.isoformat()}


@router.get("/scheduler/runs")
def list_scheduler_runs(limit: int = 50, user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    limit = min(max(limit, 1), 200)
    return [{"run_id": row.run_id, "task_key": row.task_key, "status": row.status,
             "scheduled_for": row.scheduled_for.isoformat(), "started_at": row.started_at.isoformat(),
             "finished_at": row.finished_at.isoformat() if row.finished_at else None,
             "error_code": row.error_code, "message": row.message, "user_results": row.user_results}
            for row in db.scalars(select(ScheduledTaskRun).order_by(ScheduledTaskRun.started_at.desc()).limit(limit))]


@router.post("/scheduler/secret/rotate")
def rotate_scheduler_secret(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db),
                             _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """轮换外部调度触发密钥。**高危动作**，要求「近期重新认证」。

    轮换之后旧的密钥立刻失效，外部调度服务在改配置之前**全部调不动** ——
    这是一个「按错了就整条链路停摆」的动作，所以要一道二次验证。
    """
    _require_admin(user)
    scheduler_service.lock_scheduler_sync(db)
    value = scheduler_service.rotate_trigger_secret(db, updated_by=user.username)
    log(db, action="SCHEDULER_SECRET_ROTATE", entity="system_setting", entity_id="scheduler_trigger_secret",
        user=user, detail="轮换外部调度触发密钥")
    db.commit()
    return JSONResponse({"secret": value, "warning": "只显示本次生成的明文；请放入外部调用方的安全配置"},
                        headers={"Cache-Control": "no-store"})


@router.delete("/scheduler/secret/previous")
def retire_previous_scheduler_secret(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    scheduler_service.lock_scheduler_sync(db)
    scheduler_service.clear_previous_trigger_secret(db, updated_by=user.username)
    log(db, action="SCHEDULER_SECRET_RETIRE", entity="system_setting", entity_id="scheduler_trigger_secret_previous",
        user=user, detail="停用旧的外部调度触发密钥")
    db.commit()
    return {"ok": True}


def _run_view(r: BackupRun) -> dict:
    return {
        "id": r.id, "book_id": r.book_id, "scope": r.scope,
        "book_label": r.book_label, "tier": r.tier, "status": r.status,
        "oss_key": r.oss_key, "size_bytes": r.size_bytes, "row_count": r.row_count,
        "checksum": r.checksum, "message": r.message,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
