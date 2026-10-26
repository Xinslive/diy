"""验证 → staging → journal/迁移 → rename → registry。文件系统与 DB 不共享事务。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
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

#: 不计入插件代码配额的运行时目录：备份、隔离区与暂存区。
RESERVED_DIRS = (".backups", ".quarantine", ".staging")
#: 无引用的备份保留多久（秒）。超过后才允许被清理。
BACKUP_RETENTION_SECONDS = 7 * 86400
#: 卸载后隔离代码保留多久（秒）。它是人工恢复的证据，不是无限期垃圾。
QUARANTINE_RETENTION_SECONDS = 30 * 86400


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


def disk_usage(root: Path, *, include_reserved: bool = True) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if not include_reserved and _is_reserved(root, path):
            continue
        total += path.stat().st_size
    return total


def code_usage(root: Path) -> int:
    """插件**代码**占用：不含备份、隔离与暂存区。

    配额只管代码：备份与隔离区是「变更前证据」，它们涨满时如果和代码共用
    一个闸门，插件管理会被永久锁死（装不进去也卸不掉，因为卸载也要写备份）。
    """
    return disk_usage(root, include_reserved=False)


def reserved_usage(root: Path) -> int:
    """备份、隔离与暂存区占用：单独计量、单独额度。"""
    total = 0
    for name in RESERVED_DIRS:
        folder = root / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
    return total


def reserved_quota() -> int:
    """备份与隔离区的独立额度。

    配置里只有一个 ``plugin_disk_quota_bytes``（config.py 不在插件模块的改动
    范围内），所以这里复用同一个数值但**单独判定**：备份区到顶只会卡住备份
    流程并提示清理，不会把插件管理的四条路径一起锁死。
    """
    return settings.plugin_disk_quota_bytes


def _is_reserved(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in RESERVED_DIRS


def prune_reserved(root: Path, *, force: bool = False, now=None) -> list[str]:
    """清理不再被引用的备份与过期隔离目录。

    安全规则（宁可少删）：
    - ``.backups/<attempt>``：仍被 registry 的 ``backup_path`` 或**未失败**的
      安装尝试引用的一律保留；其余只在超过保留期后才删（``force=True`` 时
      立即删掉无引用的孤儿备份）。
    - ``.quarantine/<名字>``：插件仍处于已登记（未卸载）状态时保留；卸载后
      它只是人工恢复的证据，超过保留期才删。

    返回被删除的路径列表。
    """
    now = time.time() if now is None else now
    removed: list[str] = []
    with SessionLocal() as db:
        keep = {row.backup_path for row in db.scalars(select(PluginRegistry)) if row.backup_path}
        for attempt in db.scalars(select(PluginInstallAttempt)):
            if attempt.status != "FAILED" and attempt.backup_path:
                keep.add(attempt.backup_path)
        registered = {row.plugin_id for row in db.scalars(select(PluginRegistry))}
        active = {row.plugin_id for row in db.scalars(select(PluginRegistry).where(PluginRegistry.deleted_at.is_(None)))}
    backups = root / ".backups"
    if backups.is_dir() and not backups.is_symlink():
        for entry in sorted(backups.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            if str(entry) in keep:
                continue
            if not force and now - entry.lstat().st_mtime < BACKUP_RETENTION_SECONDS:
                continue
            shutil.rmtree(entry)
            removed.append(str(entry))
    quarantine = root / ".quarantine"
    if quarantine.is_dir() and not quarantine.is_symlink():
        for entry in sorted(quarantine.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            plugin_id = entry.name.rsplit("-", 1)[0]
            if plugin_id in active:
                continue
            if plugin_id in registered and not force:
                continue
            if not force and now - entry.lstat().st_mtime < QUARANTINE_RETENTION_SECONDS:
                continue
            shutil.rmtree(entry)
            removed.append(str(entry))
    return removed


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
    # 备份区有自己的额度：到顶时先清理无引用的旧备份，再决定是否中止。
    if reserved_usage(root_path()) > reserved_quota():
        prune_reserved(root_path())
        if reserved_usage(root_path()) > reserved_quota():
            raise UploadTooLarge("插件备份区超过独立配额；已清理无引用备份仍不足，请检查 .backups 与 .quarantine")
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
        # 配额只管插件代码：备份与隔离区涨满时不应该让插件装不进去。
        if code_usage(root) + sum(map(len, verified.files.values())) > settings.plugin_disk_quota_bytes:
            raise UploadTooLarge("插件存储配额不足")
        staging = root / ".staging" / attempt_id
        verified.extract(staging)
        with installation_lock(pid):
            if code_usage(root) > settings.plugin_disk_quota_bytes:
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
                    # 两种「已登记」要分开说：一种是扩展还装着（换版本走
                    # 「升级」，且要先停用并重启），另一种是卸载后保留了业务表
                    # （那种情况只能按原版本恢复，不能当新插件装）。原来两种
                    # 情况共用一句话，用户照着做只会撞同一堵墙。
                    if old.deleted_at:
                        raise PluginError("PLUGIN_ALREADY_EXISTS", "插件 id 已登记；保留表卸载后需恢复旧版本而不能重新安装", 409)
                    raise PluginError("PLUGIN_ALREADY_EXISTS", "该扩展已安装；要换版本请在扩展列表里点「升级」"
                                 "（升级前需先停用并重启服务）", 409)
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
