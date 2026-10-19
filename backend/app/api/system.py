"""系统管理：用户、账套授权、外部服务配置、数据安全（OSS 备份）。

内部系统只有几十个人，但「谁能看哪个账套」这件事必须落在数据库里，
不能靠前端藏按钮。因此权限模型只有三个角色：

    OWNER       账套负责人，可改科目/模板/授权
    ACCOUNTANT  可制单、记账、结账、结转
    VIEWER      只读

授权粒度是 (用户, 账套)。一个用户可以有多个账套，每个账套角色独立。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi.responses import JSONResponse
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.backup import config as backup_config
from app.backup import oss_client
from app.backup import service as backup_service
from app.core import system_settings
from app.core import lockout
from app.core.db import SessionLocal, get_db
from app.core.deps import get_current_user
from app.core.security import hash_password
from app.models import (SCOPE_SYSTEM, STATUS_FAILED, TIER_DAILY, AuditLog,
                        BackupRun, Book, SysUser, UserBook)
from app.models.scheduler import ScheduledTask, ScheduledTaskRun
from app.notify import config as notify_config
from app.notify import events as notify_events
from app.notify import mailer as notify_mailer
from app.scheduler import service as scheduler_service
from app.plugins.workers import status as plugin_status
from app.models.plugin import PluginRegistry

router = APIRouter(prefix="/api/system", tags=["系统管理"])

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


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    real_name: str = ""
    active: bool = True
    books: list[BookLinkIn] = Field(default_factory=list)


class UserPatch(BaseModel):
    real_name: Optional[str] = None
    password: Optional[str] = None
    active: Optional[bool] = None


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
                db: Session = Depends(get_db)):
    _require_admin(user)
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    before = _user_snapshot(target)
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
        did.append("重置口令")
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
              db: Session = Depends(get_db)):
    """整份替换某用户的账套授权列表。"""

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
    db.commit()
    return _serialize(target, {b.book_id: b.role for b in books})


@router.delete("/users/{user_id}")
def delete_user(user_id: int, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_admin(user)
    if user_id == user.id:
        raise HTTPException(400, "不能删除当前登录的用户")
    target = db.get(SysUser, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    # P2-03：删除过去不留痕。删完之后连「曾经有过这么一个账号」都没地方查 ——
    # 而账号名册是要用来回答「这个账上的人都有谁」的。快照必须在 delete 之前取。
    before = _user_snapshot(target)
    before["books"] = {str(link.book_id): link.role for link in db.scalars(
        select(UserBook).where(UserBook.user_id == user_id)).all()}
    log(db, action="DELETE_USER", entity="user", entity_id=user_id, user=user,
        detail=f"删除用户 {target.username}", before=before)
    db.delete(target)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: int, user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """**提前解除锁定**，不等锁定自然到期。

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
                     db: Session = Depends(get_db)):
    """改自助注册开关。**留痕**：这个开关决定「陌生人能不能往 sys_user 里加行」，
    打开它的那一次必须有据可查（谁、何时、从关到开）。
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
                   db: Session = Depends(get_db)):
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
                      db: Session = Depends(get_db)):
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
               + ("（含口令）" if (payload.password or payload.clear_password) else ""),
        before={k: before.get(k) for k in
                ("host", "port", "username", "password_hint",
                 "from_address", "use_ssl", "use_tls", "recipients", "enabled")},
        after={k: after.get(k) for k in
               ("host", "port", "username", "password_hint",
                "from_address", "use_ssl", "use_tls", "recipients", "enabled")},
    )
    db.commit()
    return after


@router.post("/notify/test")
def test_notify(user: SysUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """发一封测试信。

    **收件人就是真实的收件人** —— 收不到信的地址就是配错了，而
    「配错了却显示测试成功」是最没用的测试结果。
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
                      db: Session = Depends(get_db)):
    _require_admin(user)
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
    """写一个探针对象再删掉 —— 验的是「能不能写」，那才是备份唯一要紧的事。"""
    _require_admin(user)
    try:
        cfg = backup_config.require_ready(db)
        message = oss_client.check_connection(cfg)
    except (backup_config.ConfigIncomplete, oss_client.OssError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": message}


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
        backup_config.require_ready(db)
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
                              db: Session = Depends(get_db)):
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
    log(db, action="SCHEDULER_TASK_UPDATE", entity="scheduled_task", entity_id=row.task_key,
        user=user, detail="更新插件定时任务开关", before={"enabled": was_enabled}, after={"enabled": row.enabled})
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
def rotate_scheduler_secret(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
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
