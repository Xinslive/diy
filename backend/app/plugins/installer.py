"""验证 → staging → journal/迁移 → rename → registry。文件系统与 DB 不共享事务。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select, text, update
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.uploads import UploadTooLarge
from app.plugins import audit
from app.plugins.errors import PluginError
from app.plugins.manifest import table_prefix
from app.plugins.migrations import migrate
from app.plugins.models import PluginInstallAttempt, PluginRegistry, PluginWorkerRuntime
from app.plugins.security import verify_archive
from app.plugins.workers import require_stopped


def root_path() -> Path:
    root = Path(settings.plugin_root).absolute()
    if root.is_symlink():
        raise PluginError("UNSAFE_PLUGIN_ROOT", "插件根目录不能是符号链接", 503)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (".staging", ".quarantine", ".locks", ".backups"):
        child = root / name
        if child.is_symlink():
            raise PluginError("UNSAFE_PLUGIN_ROOT", "插件运行时目录不能是符号链接", 503)
        child.mkdir(mode=0o700, exist_ok=True)
    return root.resolve()


@contextmanager
def installation_lock(plugin_id: str):
    root = root_path()
    key = int.from_bytes(hashlib.sha256(("cwq.plugin." + plugin_id).encode()).digest()[:8], "big", signed=True)
    with engine.connect() as conn:
        locked = conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        conn.commit()
        if not locked:
            raise PluginError("PLUGIN_BUSY", "插件有未完成操作，请稍后重试", 409)
        try:
            # 全局文件锁也序列化配额与 reconcile；不覆盖上传/解压。
            with (root / ".locks" / "install.lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PluginError("PLUGIN_BUSY", "插件存储正在变更，请稍后重试", 409) from exc
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            conn.commit()


def disk_usage(root: Path):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def make_backup(plugin_id: str, attempt_id: str) -> str:
    from app.backup.export import export_scope, restore, SCOPE_SYSTEM, SCOPE_BOOK
    from app.models.book import Book
    destination = root_path() / ".backups" / attempt_id
    destination.mkdir(mode=0o700)
    records = []
    with SessionLocal() as db:
        scopes = [(SCOPE_SYSTEM, None)] + [(SCOPE_BOOK, ident) for ident in db.scalars(select(Book.id))]
        for scope, ident in scopes:
            path = destination / (f"book-{ident}.json.gz" if ident else "system.json.gz")
            export_scope(db, scope=scope, book_id=ident, book_label="插件变更前备份", out_path=path)
            # 卸载保留表仍须能备份；此模式只核对结构/数据闭包，禁止写入。
            counts = restore(db, path, dry_run=True, _retained_backup_validation=True)
            records.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": sum(counts.values())})
    (destination / "index.json").write_text(json.dumps({"plugin_id": plugin_id, "snapshots": records}), encoding="utf-8")
    for path in destination.iterdir():
        path.chmod(0o600)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    _sync_directory(destination)
    _sync_directory(destination.parent)
    if disk_usage(root_path()) > settings.plugin_disk_quota_bytes:
        raise UploadTooLarge("插件备份后超过存储配额；保留备份，停止变更")
    return str(destination)


def commit_attempt(attempt_id):
    """幂等向前完成；也用于迁移已提交的 crash recovery。"""
    with SessionLocal.begin() as db:
        attempt = db.get(PluginInstallAttempt, attempt_id)
        old = db.get(PluginRegistry, attempt.plugin_id)
        if old and old.plugin_digest == attempt.new_digest and old.pending_attempt_id == attempt_id:
            attempt.status = "COMMITTED"
            return
        if old and old.plugin_digest != attempt.old_digest:
            raise PluginError("RECONCILE_CONFLICT", "登记版本已变化，需要人工恢复", 409)
        manifest = attempt.manifest_metadata
        if old is None:
            old = PluginRegistry(plugin_id=attempt.plugin_id)
            db.add(old)
        old.version, old.plugin_digest = manifest["version"], attempt.new_digest
        old.manifest_version, old.manifest_metadata = manifest["format_version"], manifest
        old.desired_enabled = attempt.operation == "install"
        old.desired_revision = uuid.uuid4().hex
        old.access_default, old.runtime_status = "none", "PENDING_RESTART" if old.desired_enabled else "DISABLED"
        old.generation = db.scalar(text("SELECT generation FROM plugin_cohort WHERE id=1")) or ""
        old.error_code, old.error_message = "", ""
        old.migration_head, old.tables = attempt.migration_head, attempt.tables
        old.installed_by, old.installed_at = attempt.actor_id, datetime.now(timezone.utc)
        old.archive_digest, old.pending_attempt_id = attempt.archive_digest, attempt_id
        old.backup_path, old.deleted_at = attempt.backup_path, None
        if attempt.operation == "upgrade":
            # 全部 worker 已确认禁用；更新其禁用确认，不导入任何新代码。
            db.execute(update(PluginWorkerRuntime).where(
                PluginWorkerRuntime.plugin_id == old.plugin_id,
                PluginWorkerRuntime.status == "DISABLED",
            ).values(plugin_digest=old.plugin_digest, desired_revision=old.desired_revision))
        attempt.status = "COMMITTED"
        from app.models.system import SysUser
        actor = db.get(SysUser, attempt.actor_id) if attempt.actor_id else None
        audit.event(db, attempt.operation, attempt.plugin_id, user=actor,
            version=manifest["version"], digest=attempt.new_digest, attempt_id=attempt_id,
            key_id=manifest["signature"]["key_id"], algo=manifest["signature"]["algo"], archive_digest=attempt.archive_digest)


def _semver(value):
    # SemVer precedence: build 元数据不参与比较；不能用 PEP440 把 prerelease 误作版本。
    import re
    core, _, pre = value.split("+", 1)[0].partition("-")
    parts = tuple(int(p) for p in core.split("."))
    tail = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return parts, not bool(pre), tail


def install(data: bytes, user, *, upgrade_id=None):
    attempt_id, pid, staging = uuid.uuid4().hex, upgrade_id or "unknown", None
    action = "upgrade" if upgrade_id else "install"
    try:
        if not settings.plugin_enabled:
            raise PluginError("PLATFORM_DISABLED", "插件平台未开启", 503)
        verified = verify_archive(data)
        manifest, pid = verified.manifest, verified.manifest["id"]
        if upgrade_id and pid != upgrade_id:
            raise PluginError("PLUGIN_ID_MISMATCH", "升级包 id 与目标插件不一致")
        root = root_path()
        if disk_usage(root) + sum(map(len, verified.files.values())) > settings.plugin_disk_quota_bytes:
            raise UploadTooLarge("插件存储配额不足")
        staging = root / ".staging" / attempt_id
        verified.extract(staging)
        with installation_lock(pid):
            if disk_usage(root) > settings.plugin_disk_quota_bytes:
                raise UploadTooLarge("插件存储配额不足")
            with SessionLocal() as db:
                old = db.get(PluginRegistry, pid)
                if upgrade_id:
                    if old is None or old.deleted_at:
                        raise PluginError("PLUGIN_NOT_FOUND", "插件不存在", 404)
                    require_stopped(db, old)
                    if _semver(manifest["version"]) <= _semver(old.version):
                        raise PluginError("DOWNGRADE_OR_REPLAY_REJECTED", "只允许升级到更高版本", 409)
                    if not {t["name"] for t in old.tables} <= {t["name"] for t in manifest.get("tables", [])}:
                        raise PluginError("DESTRUCTIVE_MIGRATION_REJECTED", "首版升级不得移除已登记的插件表", 409)
                elif old is not None:
                    raise PluginError("PLUGIN_ALREADY_EXISTS", "插件 id 已登记；保留表卸载后需恢复旧版本而不能重新安装", 409)
                for existing in db.scalars(select(PluginRegistry.plugin_id)):
                    if existing != pid and table_prefix(existing) == table_prefix(pid):
                        raise PluginError("PLUGIN_PREFIX_COLLISION", "插件表名前缀发生冲突", 409)
                old_digest = old.plugin_digest if old else ""
            backup = make_backup(pid, attempt_id) if upgrade_id else ""
            active = root / pid / verified.digest
            if active.exists() or active.parent.is_symlink():
                raise PluginError("IMMUTABLE_PACKAGE_EXISTS", "目标包目录已存在或不安全", 409)
            with SessionLocal.begin() as db:
                db.add(PluginInstallAttempt(attempt_id=attempt_id, plugin_id=pid, operation=action,
                    old_digest=old_digest, new_digest=verified.digest, staging_path=str(staging), active_path=str(active),
                    manifest_metadata=manifest, archive_digest=verified.archive_digest, actor_id=user.id, backup_path=backup))
            def migrated(conn, head, metadata):
                conn.execute(update(PluginInstallAttempt).where(PluginInstallAttempt.attempt_id == attempt_id).values(
                    status="MIGRATED", migration_head=head, tables=metadata))
            migrate(engine, staging, manifest, on_commit=migrated)
            active.parent.mkdir(mode=0o700, exist_ok=True)
            os.rename(staging, active)  # 同一 root 内，不 copy、不覆盖。
            _sync_directory(active.parent)
            commit_attempt(attempt_id)
        return {"plugin_id": pid, "status": "disabled" if upgrade_id else "pending_restart", "attempt_id": attempt_id}
    except BaseException as exc:
        code = getattr(exc, "code", "UPLOAD_TOO_LARGE" if isinstance(exc, UploadTooLarge) else "INSTALL_FAILED")
        # MIGRATED/rename 后的包交给 reconcile；不能清掉可向前完成的证据。
        with SessionLocal.begin() as db:
            attempt = db.get(PluginInstallAttempt, attempt_id)
            preserve = bool(attempt and attempt.status in {"MIGRATED", "COMMITTED"})
            if attempt and not preserve:
                attempt.status, attempt.error_code = "FAILED", code
        if staging and staging.exists() and not preserve:
            shutil.rmtree(staging)
        audit.failure(action, pid, user, code, attempt_id=attempt_id, findings=getattr(exc, "findings", []))
        raise


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
