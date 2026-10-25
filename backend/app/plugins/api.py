"""管理员管理面、用户 manifest 和公开 immutable 静态代码资产。"""
from __future__ import annotations

import mimetypes
import re
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import MetaData, Table, func, select, text
from sqlalchemy.orm import Session
from starlette.responses import FileResponse
from app.core.config import settings
from app.core.db import get_db, SessionLocal, engine
from app.core.deps import get_current_user, require_admin, require_recent_admin_mfa
from app.core.uploads import UploadTooLarge, read_upload_sync
from app.models.plugin import PluginInstallAttempt
from app.models.system import SysUser
from app.plugins import audit
from app.plugins.errors import PluginError, not_found
from app.plugins.installer import install, installation_lock, make_backup, prune_reserved, root_path
from app.plugins.manifest import DIGEST_RE, safe_path, version_table
from app.plugins.migrations import validate_tables
from app.plugins.models import PluginRegistry, PluginUserAccess, PluginWorkerRuntime
from app.plugins.security import ASSET_TICKET_SEGMENT, add_revoked_key, asset_token, verify_asset_token, verify_directory
from app.plugins.workers import status, require_stopped, live_cutoff

router = APIRouter()
PRIVATE = {"Cache-Control": "private, no-store"}
#: 已核验包的资产清单缓存：(插件 id, 包摘要) -> (目录状态, {路径: (摘要, 大小, mtime)})
_ASSET_CACHE: dict[tuple[str, str], tuple] = {}
_ASSET_CACHE_LIMIT = 16


def _admin_audited(request: Request, user=Depends(get_current_user)):
    """管理员判定，**并在被拒时留痕**。

    单独拆出来是因为它要**先于**二次验证判定执行：不是管理员的人
    访问写接口时，留下的应当是 ``management_denied``（历史上就有这条
    审计），而不是「二次验证没做」—— 后者会掩盖真正的原因。
    """
    try:
        return require_admin(user)
    except HTTPException:
        if request.method not in {"GET", "HEAD"}:
            audit.failure("management_denied", request.path_params.get("plugin_id", "unknown"), user, "ADMIN_REQUIRED")
        raise


def management(request: Request, response: Response, user=Depends(_admin_audited)):
    """**读**侧依赖：只要求管理员。

    GET 类路由挂这个 —— 「看一眼插件列表」不是高危动作，不该被二次
    验证挡住（否则管理员在没做二次验证时连界面都打不开）。
    """
    response.headers.update(PRIVATE)
    return user


def management_write(request: Request, response: Response,
                     user=Depends(_admin_audited),
                     db: Session = Depends(get_db),
                     _mfa: SysUser = Depends(require_recent_admin_mfa)):
    """**写**侧依赖：管理员 **且** 近期完成过邮箱二次验证。

    **为什么插件的写操作要二次验证**（审查 §3.2）：契约写在
    ``core/config.py:225`` —— 「管理员验过二次因子之后，这个窗口内做高危
    动作（改他人口令 / **装插件** / 读 OSS 凭据 / 轮换调度密钥 / 删用户）
    不再重复要求走邮箱」。装插件原来**完全不在**这道闸门里 ——
    ``grep require_recent_admin_mfa app/`` 的命中只有 ``api/system.py``
    与 ``core/deps.py``，``app/plugins/`` 一处都没有。

    它的代价不是「权限更大」，而是**逃逸令牌有效期**：插件代码跑在
    **API 宿主进程内**（``app/plugins/loader.py``），一份装上去的插件能
    改库、能提权。于是「拿到一枚管理员令牌」这条路上，装插件是**唯一**
    能把临时凭据变成**永久后门**的动作 —— 而闸门恰恰就是为了防这个。

    二次验证开关关着时这个依赖**不拦人**（见
    ``core/deps.py::require_recent_admin_mfa``），所以挂着它不影响现有部署；
    开关开着而通道坏了时它**一律拒绝**（fail-closed）。
    """
    response.headers.update(PRIVATE)
    return user


def managed(action):
    def decorator(fn):
        @wraps(fn)
        def call(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (PluginError, HTTPException, UploadTooLarge) as exc:
                if action not in {"install", "upgrade"} or isinstance(exc, UploadTooLarge):
                    audit.failure(action, kwargs.get("plugin_id", "unknown"), kwargs.get("user"),
                                  getattr(exc, "code", "REQUEST_REJECTED"))
                if isinstance(exc, PluginError):
                    raise exc.http()
                if isinstance(exc, UploadTooLarge):
                    raise HTTPException(413, str(exc))
                raise
            except Exception:
                audit.failure(action, kwargs.get("plugin_id", "unknown"), kwargs.get("user"), "OPERATION_FAILED")
                raise HTTPException(500, {"code": "PLUGIN_OPERATION_FAILED", "message": "插件操作失败，请检查审计记录"})
        return call
    return decorator


def find(db, plugin_id, *, include_deleted=False):
    row = db.get(PluginRegistry, plugin_id)
    if not row or (row.deleted_at and not include_deleted):
        raise PluginError("PLUGIN_NOT_FOUND", "插件不存在", 404)
    return row


@router.get("/api/system/plugins")
def list_plugins(request: Request, user=Depends(management), db: Session = Depends(get_db)):
    runtime = getattr(request.app.state, "plugin_runtime", None)
    results = []
    for p in db.scalars(select(PluginRegistry).order_by(PluginRegistry.plugin_id)):
        current, code = status(db, p, runtime.generation if runtime else None)
        results.append({"id": p.plugin_id, "name": p.manifest_metadata["name"], "version": p.version,
            "digest": p.plugin_digest, "desired_enabled": p.desired_enabled, "runtime_status": current,
            "generation": db.scalar(text("SELECT generation FROM plugin_cohort WHERE id=1")) or "",
            "error_code": code, "migration_head": p.migration_head, "table_count": len(p.tables),
            "deleted": p.deleted_at is not None, "backup_path": p.backup_path,
            "user_count": db.scalar(select(func.count()).select_from(PluginUserAccess).where(
                PluginUserAccess.plugin_id == p.plugin_id, PluginUserAccess.revoked_at.is_(None)))})
    return {"enabled": settings.plugin_enabled, "plugins": results}


@router.get("/api/system/plugins/eligible-users")
def eligible_users(query: str = Query("", max_length=128), page: int = Query(1, ge=1),
                   user=Depends(management), db: Session = Depends(get_db)):
    stmt = select(SysUser).where(SysUser.active.is_(True))
    if query:
        value = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(SysUser.username.ilike(value, escape="\\") | SysUser.real_name.ilike(value, escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(SysUser.id).offset((page - 1) * 30).limit(30))
    return {"users": [{"id": u.id, "username": u.username, "real_name": u.real_name, "active": u.active} for u in rows], "total": total}



@router.post("/api/system/plugins/install")
@managed("install")
def install_plugin(file: UploadFile = File(...), user=Depends(management_write)):
    raw = read_upload_sync(file, limit=min(settings.plugin_max_bytes, settings.import_max_bytes))
    return install(raw, user)


@router.post("/api/system/plugins/reconcile")
@managed("reconcile")
def reconcile_plugins(user=Depends(management_write)):
    from app.plugins.reconcile import reconcile
    result = reconcile()
    with SessionLocal.begin() as db:
        audit.event(db, "reconcile", "host.plugins", user=user)
    return result


class KeyRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


@router.post("/api/system/plugins/revoke-key")
@managed("revoke_key")
def revoke_key(payload: KeyRevoke, user=Depends(management_write), db: Session = Depends(get_db)):
    """按 key_id 一键封禁签名密钥，并停用这把钥匙签出来的插件。

    撤销名单在**安装期和加载期**都判定：把 key_id 写进运行时封禁名单后，
    用它签发的已安装插件在下次 worker 启动时会被拒绝加载（``KEY_REVOKED``），
    新包也装不进来。已经跑起来的代码不会被远程停掉 —— 停用需要重启 worker。
    """
    key_id = payload.key_id.strip()
    revoked = add_revoked_key(key_id)
    affected = []
    for p in db.scalars(select(PluginRegistry).where(PluginRegistry.deleted_at.is_(None))):
        signature = (p.manifest_metadata or {}).get("signature") or {}
        if signature.get("key_id") != key_id:
            continue
        p.desired_enabled = False
        p.desired_revision = uuid.uuid4().hex
        p.runtime_status = "PENDING_RESTART"
        p.error_code = "KEY_REVOKED"
        affected.append(p.plugin_id)
        audit.event(db, "revoke_key", p.plugin_id, user=user, key_id=key_id)
    audit.event(db, "revoke_key", "host.plugins", user=user, key_id=key_id)
    db.commit()
    return {"status": "revoked", "key_id": key_id, "revoked_keys": sorted(revoked),
            "disabled_plugins": affected, "restart_required": True}


@router.get("/api/system/plugins/attempts")
@managed("attempts_read")
def attempts(user=Depends(management), db: Session = Depends(get_db)):
    """安装尝试与隔离现场：管理员排查「装到一半中断」时只看这一张表。"""
    rows = db.scalars(select(PluginInstallAttempt).order_by(PluginInstallAttempt.created_at.desc()).limit(50)).all()
    return {"attempts": [{"attempt_id": row.attempt_id, "plugin_id": row.plugin_id, "operation": row.operation,
        "status": row.status, "error_code": row.error_code, "migration_head": row.migration_head,
        "old_digest": row.old_digest, "new_digest": row.new_digest, "backup_path": row.backup_path,
        "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}


class Prune(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


@router.post("/api/system/plugins/maintenance/prune")
@managed("prune")
def prune_reserved_route(payload: Prune, user=Depends(management_write)):
    """清理备份区与隔离区。

    默认遵守保留期；``force=True`` 立即删除**没有任何引用**的孤儿备份。
    仍被 registry 或未失败的安装尝试引用的备份一律保留。
    """
    with installation_lock("host.maintenance"):
        removed = prune_reserved(root_path(), force=payload.force)
    return {"status": "pruned", "removed": removed}


def set_enabled(db, plugin_id, user, enabled):
    with installation_lock(plugin_id):
        row = find(db, plugin_id)
        if row.desired_enabled != enabled:
            row.desired_enabled, row.desired_revision = enabled, uuid.uuid4().hex
            row.generation = db.scalar(text("SELECT generation FROM plugin_cohort WHERE id=1")) or ""
        row.runtime_status = "PENDING_RESTART"
        audit.event(db, "enable" if enabled else "disable", plugin_id, user=user, version=row.version, digest=row.plugin_digest)
        db.commit()
    return {"status": "pending_restart", "plugin_id": plugin_id}


@router.post("/api/system/plugins/{plugin_id}/enable")
@managed("enable")
def enable(plugin_id: str, user=Depends(management_write), db: Session = Depends(get_db)):
    return set_enabled(db, plugin_id, user, True)


@router.post("/api/system/plugins/{plugin_id}/disable")
@managed("disable")
def disable(plugin_id: str, user=Depends(management_write), db: Session = Depends(get_db)):
    return set_enabled(db, plugin_id, user, False)


@router.get("/api/system/plugins/{plugin_id}/users")
@managed("users_read")
def users(plugin_id: str, user=Depends(management), db: Session = Depends(get_db)):
    find(db, plugin_id)
    rows = db.execute(select(SysUser, PluginUserAccess).join(PluginUserAccess, PluginUserAccess.user_id == SysUser.id).where(
        PluginUserAccess.plugin_id == plugin_id, PluginUserAccess.revoked_at.is_(None)).order_by(SysUser.id)).all()
    return {"users": [{"id": u.id, "username": u.username, "real_name": u.real_name, "active": u.active,
                       "grant_id": a.grant_id, "granted_at": a.granted_at} for u, a in rows]}


class UserSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[StrictInt] = Field(max_length=10000)


def change_users(db, plugin_id, user, ids=None, *, add=None, remove=None):
    row = db.scalar(select(PluginRegistry).where(PluginRegistry.plugin_id == plugin_id).with_for_update())
    if not row or row.deleted_at:
        raise PluginError("PLUGIN_NOT_FOUND", "插件不存在", 404)
    records = {a.user_id: a for a in db.scalars(select(PluginUserAccess).where(PluginUserAccess.plugin_id == plugin_id))}
    current = {ident for ident, a in records.items() if a.revoked_at is None}
    wanted = set(ids) if ids is not None else (current | ({add} if add is not None else set())) - ({remove} if remove is not None else set())
    valid = set(db.scalars(select(SysUser.id).where(SysUser.id.in_(wanted), SysUser.active.is_(True))))
    if wanted != valid:
        raise PluginError("INVALID_PLUGIN_USER", "授权名单包含不存在或已停用的用户")
    for ident in wanted - current:
        access = records.get(ident)
        if access is None:
            access = PluginUserAccess(plugin_id=plugin_id, user_id=ident)
            db.add(access)
        access.grant_id, access.granted_by = uuid.uuid4().hex, user.id
        access.granted_at, access.revoked_at = datetime.now(timezone.utc), None
        audit.event(db, "grant", plugin_id, user=user, target_user_id=ident)
    for ident in current - wanted:
        records[ident].revoked_at = datetime.now(timezone.utc)
        audit.event(db, "revoke", plugin_id, user=user, target_user_id=ident)
    db.commit()
    return {"status": "applied", "user_count": len(wanted)}


@router.put("/api/system/plugins/{plugin_id}/users")
@managed("users_replace")
def replace_users(plugin_id: str, payload: UserSet, user=Depends(management_write), db: Session = Depends(get_db)):
    return change_users(db, plugin_id, user, payload.user_ids)


@router.post("/api/system/plugins/{plugin_id}/users/{user_id}")
@managed("grant")
def grant(plugin_id: str, user_id: int, user=Depends(management_write), db: Session = Depends(get_db)):
    return change_users(db, plugin_id, user, add=user_id)


@router.delete("/api/system/plugins/{plugin_id}/users/{user_id}")
@managed("revoke")
def revoke(plugin_id: str, user_id: int, user=Depends(management_write), db: Session = Depends(get_db)):
    return change_users(db, plugin_id, user, remove=user_id)


@router.post("/api/system/plugins/{plugin_id}/upgrade")
@managed("upgrade")
def upgrade(plugin_id: str, file: UploadFile = File(...), user=Depends(management_write)):
    raw = read_upload_sync(file, limit=min(settings.plugin_max_bytes, settings.import_max_bytes))
    return install(raw, user, upgrade_id=plugin_id)


@router.delete("/api/system/plugins/{plugin_id}")
@managed("delete")
def delete_plugin(plugin_id: str, user=Depends(management_write), db: Session = Depends(get_db)):
    from app.plugins.reconcile import quarantine
    with installation_lock(plugin_id):
        p = find(db, plugin_id)
        require_stopped(db, p)
        backup = make_backup(plugin_id, uuid.uuid4().hex)
        # 先登记删除/撤权，commit uncertain 由 reconcile 保留目录，不会加载它。
        for access in db.scalars(select(PluginUserAccess).where(PluginUserAccess.plugin_id == plugin_id, PluginUserAccess.revoked_at.is_(None))):
            access.revoked_at = datetime.now(timezone.utc)
            audit.event(db, "revoke", plugin_id, user=user, target_user_id=access.user_id)
        p.deleted_at, p.runtime_status, p.backup_path = datetime.now(timezone.utc), "ORPHANED", backup
        audit.event(db, "delete", plugin_id, user=user, version=p.version, digest=p.plugin_digest)
        db.commit()
        quarantine(root_path() / plugin_id, root_path())
    return {"status": "orphaned", "retained_tables": [t["name"] for t in p.tables], "backup_path": backup}


@router.get("/api/system/plugins/{plugin_id}/tables")
@managed("tables_read")
def tables(plugin_id: str, user=Depends(management), db: Session = Depends(get_db)):
    p = find(db, plugin_id, include_deleted=True)
    metadata = [] if p.error_code == "TABLES_CLEANED" else validate_tables(db.connection(), p.manifest_metadata, expected=p.tables)
    return {"tables": [{**meta, "row_count": db.scalar(select(func.count()).select_from(
        Table(meta["name"], MetaData(), autoload_with=db.connection(), schema="public")))} for meta in metadata], "backup_path": p.backup_path}


class Cleanup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


@router.post("/api/system/plugins/{plugin_id}/tables/cleanup")
@managed("cleanup")
def cleanup(plugin_id: str, payload: Cleanup, user=Depends(management_write), db: Session = Depends(get_db)):
    with installation_lock(plugin_id):
        p = find(db, plugin_id, include_deleted=True)
        if payload.confirmation != plugin_id or not p.deleted_at:
            raise PluginError("CLEANUP_CONFIRMATION_REQUIRED", "只允许在保留表卸载后输入完整插件 id 确认清理", 409)
        if db.scalar(select(PluginWorkerRuntime.worker_id).where(PluginWorkerRuntime.plugin_id == plugin_id,
            PluginWorkerRuntime.lease_at > live_cutoff(), PluginWorkerRuntime.status != "DISABLED").limit(1)):
            raise PluginError("DISABLE_AND_RESTART_REQUIRED", "仍存在旧插件 worker", 409)
        backup = make_backup(plugin_id, uuid.uuid4().hex)
        metadata = validate_tables(db.connection(), p.manifest_metadata, expected=p.tables)
        reflected = MetaData()
        for meta in metadata:
            Table(meta["name"], reflected, autoload_with=db.connection(), schema="public")
        for table in reversed(reflected.sorted_tables):
            if table.name in {m["name"] for m in metadata}:
                table.drop(db.connection())
        db.execute(text(f'DROP TABLE IF EXISTS public."{version_table(plugin_id)}"'))
        p.tables, p.migration_head, p.backup_path = [], "", backup
        p.error_code = "TABLES_CLEANED"
        audit.event(db, "cleanup", plugin_id, user=user, version=p.version, digest=p.plugin_digest)
        db.commit()
    return {"status": "tables_removed", "backup_path": backup}


@router.get("/api/plugins/manifest")
def manifest(request: Request, response: Response, user=Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers.update(PRIVATE)
    runtime = getattr(request.app.state, "plugin_runtime", None)
    result = []
    if settings.plugin_enabled and runtime:
        rows = db.scalars(select(PluginRegistry).join(PluginUserAccess).where(
            PluginUserAccess.user_id == user.id, PluginUserAccess.revoked_at.is_(None), PluginRegistry.deleted_at.is_(None)))
        for p in rows:
            if status(db, p, runtime.generation)[0] != "READY":
                continue
            c = p.manifest_metadata["contributes"]
            if "frontend_entry" not in c:
                continue
            # 资产 URL 带票据：浏览器原生 import() 不带 Bearer，票据是「只覆盖
            # 这一个包的静态资产」的短期凭据，由已登录用户从本接口取得。
            token = asset_token(p.plugin_id, p.plugin_digest)
            result.append({"id": p.plugin_id, "name": p.manifest_metadata["name"], "version": p.version,
                "digest": p.plugin_digest, "menu": c["menu"], "entry": _asset_url(p.plugin_id, p.plugin_digest, c["frontend_entry"], token),
                "styles": [_asset_url(p.plugin_id, p.plugin_digest, f["path"], token) for f in p.manifest_metadata["files"]
                           if f["path"].startswith("frontend/") and f["path"].endswith(".css")]})
    return {"plugins": result}


def _asset_url(plugin_id: str, digest: str, relative: str, token: str) -> str:
    """把插件内的相对路径变成带票据的资产 URL。

    票据段放在 ``frontend/`` **之后**：前端的资产校验要求路径以
    ``/api/plugins/assets/{id}/{digest}/frontend/`` 开头，票据必须留在其后。
    """
    if not isinstance(relative, str) or not relative.startswith("frontend/"):
        return ""
    return (f"/api/plugins/assets/{plugin_id}/{digest}/frontend/{ASSET_TICKET_SEGMENT}/{token}/"
            + relative[len("frontend/"):])


def _verified_assets(plugin_id: str, digest: str):
    """核验已安装包并缓存资产清单。

    资产路由过去每请求一次都重算**整包** SHA-256 并重做一次 Ed25519 验签；
    一个 20 MB / 512 文件的包，插件页面加载 N 个资源就是 N 倍全包哈希。
    这里按 digest 缓存核验结果，缓存键含包内容（digest）与目录状态；被替换
    （重装、reconcile 隔离）时目录状态变化，缓存自动失效并重新核验。
    """
    directory = root_path() / plugin_id / digest
    try:
        state = directory.stat()
    except OSError:
        _ASSET_CACHE.pop((plugin_id, digest), None)
        raise not_found()
    key = (plugin_id, digest)
    cached = _ASSET_CACHE.get(key)
    if cached and cached[0] == (state.st_mtime_ns, state.st_size):
        return directory, cached[1]
    package = verify_directory(directory, digest)
    files = {}
    for item in package.manifest["files"]:
        target = directory / item["path"]
        try:
            info = target.stat()
        except OSError:
            continue
        files[item["path"]] = (item["sha256"], item["size"], info.st_mtime_ns)
    if len(_ASSET_CACHE) >= _ASSET_CACHE_LIMIT:
        _ASSET_CACHE.pop(next(iter(_ASSET_CACHE)))
    _ASSET_CACHE[key] = ((state.st_mtime_ns, state.st_size), files)
    return directory, files


@router.get("/api/plugins/assets/{plugin_id}/{digest}/{path:path}")
def asset(plugin_id: str, digest: str, path: str, request: Request, db: Session = Depends(get_db)):
    try:
        if not settings.plugin_enabled or request.url.query or not DIGEST_RE.fullmatch(digest):
            raise not_found()
        path = safe_path(path)
        if not path.startswith("frontend/"):
            raise not_found()
        rest = path[len("frontend/"):]
        parts = rest.split("/")
        if len(parts) < 3 or parts[0] != ASSET_TICKET_SEGMENT:
            # 没有票据段：匿名请求一律拒绝。
            raise HTTPException(401, {"code": "PLUGIN_ASSET_UNAUTHORIZED", "message": "插件资产需要有效票据"},
                                headers={"Cache-Control": "private, no-store"})
        token, relative = parts[1], "/".join(parts[2:])
        if not verify_asset_token(plugin_id, digest, token):
            raise HTTPException(401, {"code": "PLUGIN_ASSET_UNAUTHORIZED", "message": "插件资产票据无效或已过期"},
                                headers={"Cache-Control": "private, no-store"})
        p = db.get(PluginRegistry, plugin_id)
        if not p or p.deleted_at or p.plugin_digest != digest:
            raise not_found()
        directory, files = _verified_assets(plugin_id, digest)
        relative = "frontend/" + relative
        entry = files.get(relative)
        if not entry:
            raise not_found()
        target = directory / relative
        try:
            info = target.stat()
        except OSError:
            raise not_found()
        if (info.st_size, info.st_mtime_ns) != (entry[1], entry[2]):
            # 文件在核验之后被改动：作废缓存，重新走完整核验。
            _ASSET_CACHE.pop((plugin_id, digest), None)
            directory, files = _verified_assets(plugin_id, digest)
            entry = files.get(relative)
            if not entry:
                raise not_found()
            info = target.stat()
        suffix = Path(relative).suffix.lower()
        media = {".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
        if not media:
            raise not_found()
        return FileResponse(target, media_type=media, headers={"X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=31536000, immutable", "ETag": '"' + entry[0] + '"',
            "Cross-Origin-Resource-Policy": "same-origin"})
    except (PluginError, OSError):
        raise not_found()
