"""管理员管理面、用户 manifest 和公开 immutable 静态代码资产。"""
from __future__ import annotations

import hashlib
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
from app.core.deps import get_current_user, require_admin
from app.core.uploads import UploadTooLarge, read_upload_sync
from app.models.system import SysUser
from app.plugins import audit
from app.plugins.errors import PluginError, not_found
from app.plugins.installer import install, installation_lock, make_backup, root_path
from app.plugins.manifest import DIGEST_RE, safe_path, version_table
from app.plugins.migrations import validate_tables
from app.plugins.models import PluginRegistry, PluginUserAccess, PluginWorkerRuntime
from app.plugins.security import verify_directory
from app.plugins.workers import status, require_stopped, live_cutoff

router = APIRouter()
PRIVATE = {"Cache-Control": "private, no-store"}


def management(request: Request, response: Response, user=Depends(get_current_user)):
    response.headers.update(PRIVATE)
    try:
        return require_admin(user)
    except HTTPException:
        if request.method not in {"GET", "HEAD"}:
            audit.failure("management_denied", request.path_params.get("plugin_id", "unknown"), user, "ADMIN_REQUIRED")
        raise


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
def install_plugin(file: UploadFile = File(...), user=Depends(management)):
    raw = read_upload_sync(file, limit=min(settings.plugin_max_bytes, settings.import_max_bytes))
    return install(raw, user)


@router.post("/api/system/plugins/reconcile")
@managed("reconcile")
def reconcile_plugins(user=Depends(management)):
    from app.plugins.reconcile import reconcile
    result = reconcile()
    with SessionLocal.begin() as db:
        audit.event(db, "reconcile", "host.plugins", user=user)
    return result


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
def enable(plugin_id: str, user=Depends(management), db: Session = Depends(get_db)):
    return set_enabled(db, plugin_id, user, True)


@router.post("/api/system/plugins/{plugin_id}/disable")
@managed("disable")
def disable(plugin_id: str, user=Depends(management), db: Session = Depends(get_db)):
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
def replace_users(plugin_id: str, payload: UserSet, user=Depends(management), db: Session = Depends(get_db)):
    return change_users(db, plugin_id, user, payload.user_ids)


@router.post("/api/system/plugins/{plugin_id}/users/{user_id}")
@managed("grant")
def grant(plugin_id: str, user_id: int, user=Depends(management), db: Session = Depends(get_db)):
    return change_users(db, plugin_id, user, add=user_id)


@router.delete("/api/system/plugins/{plugin_id}/users/{user_id}")
@managed("revoke")
def revoke(plugin_id: str, user_id: int, user=Depends(management), db: Session = Depends(get_db)):
    return change_users(db, plugin_id, user, remove=user_id)


@router.post("/api/system/plugins/{plugin_id}/upgrade")
@managed("upgrade")
def upgrade(plugin_id: str, file: UploadFile = File(...), user=Depends(management)):
    raw = read_upload_sync(file, limit=min(settings.plugin_max_bytes, settings.import_max_bytes))
    return install(raw, user, upgrade_id=plugin_id)


@router.delete("/api/system/plugins/{plugin_id}")
@managed("delete")
def delete_plugin(plugin_id: str, user=Depends(management), db: Session = Depends(get_db)):
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
def cleanup(plugin_id: str, payload: Cleanup, user=Depends(management), db: Session = Depends(get_db)):
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
            base = f"/api/plugins/assets/{p.plugin_id}/{p.plugin_digest}/"
            result.append({"id": p.plugin_id, "name": p.manifest_metadata["name"], "version": p.version,
                "digest": p.plugin_digest, "menu": c["menu"], "entry": base + c["frontend_entry"],
                "styles": [base + f["path"] for f in p.manifest_metadata["files"] if f["path"].startswith("frontend/") and f["path"].endswith(".css")]})
    return {"plugins": result}


@router.get("/api/plugins/assets/{plugin_id}/{digest}/{path:path}")
def asset(plugin_id: str, digest: str, path: str, request: Request, db: Session = Depends(get_db)):
    try:
        if not settings.plugin_enabled or request.url.query or not DIGEST_RE.fullmatch(digest):
            raise not_found()
        path = safe_path(path)
        p = db.get(PluginRegistry, plugin_id)
        if not p or p.deleted_at or p.plugin_digest != digest or not path.startswith("frontend/"):
            raise not_found()
        directory = root_path() / plugin_id / digest
        package = verify_directory(directory, digest)
        if path not in package.files:
            raise not_found()
        suffix = Path(path).suffix.lower()
        media = {".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
        if not media:
            raise not_found()
        return FileResponse(directory / path, media_type=media, headers={"X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=31536000, immutable", "ETag": '"' + hashlib.sha256(package.files[path]).hexdigest() + '"',
            "Cross-Origin-Resource-Policy": "same-origin"})
    except (PluginError, OSError):
        raise not_found()
