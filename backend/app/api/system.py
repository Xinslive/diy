"""系统管理：用户、账套授权、外部服务配置。

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

from app.accounting.audit_helper import log
from app.core import system_settings
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
    return _serialize(u, {b["book_id"]: b["role"] for b in payload.books})


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
def set_books(user_id: int, books: list[dict], user: SysUser = Depends(get_current_user),
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
