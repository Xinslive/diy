"""跨文件系统/DB journal 的保守恢复，不删除业务表，不自动 downgrade。"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.plugins import audit
from app.plugins.errors import PluginError
from app.plugins.installer import root_path, installation_lock, commit_attempt, _sync_directory
from app.plugins.migrations import read_head, read_version, validate_tables
from app.plugins.models import PluginInstallAttempt, PluginRegistry
from app.plugins.security import verify_directory


def quarantine(path: Path, root: Path):
    if path.exists() or path.is_symlink():
        # 不跟随未知目录中的链接，也不执行其中的任何代码。
        os.rename(path, root / ".quarantine" / (path.name + "-" + uuid.uuid4().hex))
        _sync_directory(root / ".quarantine")


def reconcile():
    root, report = root_path(), {"completed": [], "quarantined": [], "unavailable": []}
    with SessionLocal() as db:
        attempts = [(a.attempt_id, a.plugin_id) for a in db.scalars(select(PluginInstallAttempt).where(
            PluginInstallAttempt.status.in_(["PREPARED", "MIGRATED"])))]
    for attempt_id, pid in attempts:
        try:
            with installation_lock(pid):
                with SessionLocal() as db:
                    attempt = db.get(PluginInstallAttempt, attempt_id)
                    if attempt.status not in {"PREPARED", "MIGRATED"}:
                        continue
                    stage, active = Path(attempt.staging_path), Path(attempt.active_path)
                    if stage != root / ".staging" / attempt_id or active != root / pid / attempt.new_digest or stage.is_symlink() or active.parent.is_symlink():
                        raise PluginError("RECONCILE_UNSAFE_PATH", "journal 路径与宿主路径不符")
                    registered = db.get(PluginRegistry, pid)
                    if registered and registered.pending_attempt_id == attempt_id and registered.plugin_digest == attempt.new_digest:
                        with SessionLocal.begin() as writer:
                            writer.get(PluginInstallAttempt, attempt_id).status = "COMMITTED"
                        continue
                    if attempt.status == "PREPARED":
                        # 迁移与 MIGRATED journal 更新在同一个 DB 事务中，PREPARED 表明没有提交迁移。
                        quarantine(stage, root)
                        quarantine(active, root)
                        with SessionLocal.begin() as writer:
                            row = writer.get(PluginInstallAttempt, attempt_id)
                            row.status, row.error_code = "FAILED", "INTERRUPTED_BEFORE_MIGRATION_COMMIT"
                            audit.event(writer, "reconcile", pid, result="failed", code=row.error_code, attempt_id=attempt_id)
                        report["quarantined"].append(attempt_id)
                        continue
                    package_dir = active if active.exists() else stage
                    package = verify_directory(package_dir, attempt.new_digest)
                    with engine.connect() as conn:
                        if read_head(package_dir) != attempt.migration_head or read_version(conn, pid) != attempt.migration_head:
                            raise PluginError("RECONCILE_MIGRATION_UNCERTAIN", "迁移状态不明，保留现场等待人工恢复")
                        validate_tables(conn, package.manifest, expected=attempt.tables)
                    if not active.exists():
                        active.parent.mkdir(mode=0o700, exist_ok=True)
                        os.rename(stage, active)
                        _sync_directory(active.parent)
                commit_attempt(attempt_id)
                report["completed"].append(attempt_id)
        except (PluginError, OSError) as exc:
            if isinstance(exc, OSError):
                exc = PluginError("RECONCILE_IO_ERROR", "插件恢复遇到文件系统错误，保留现场")
            if exc.code == "PLUGIN_BUSY":
                continue
            with SessionLocal.begin() as db:
                a = db.get(PluginInstallAttempt, attempt_id)
                a.error_code = exc.code
                plugin = db.get(PluginRegistry, pid)
                if plugin:
                    plugin.runtime_status, plugin.error_code = "ORPHANED", exc.code
                audit.event(db, "reconcile", pid, result="failed", code=exc.code, attempt_id=attempt_id)
            report["unavailable"].append(pid)
    # 锁内重读 registry，避免把正在提交的 rename 误当孤儿。
    with installation_lock("host.reconcile"):
        with SessionLocal.begin() as db:
            plugins = list(db.scalars(select(PluginRegistry)))
            committed = {(p.plugin_id, p.plugin_digest) for p in plugins if not p.deleted_at}
            retained = {(a.plugin_id, a.new_digest) for a in db.scalars(select(PluginInstallAttempt).where(
                PluginInstallAttempt.status.in_(["COMMITTED", "MIGRATED"])))}
            for p in plugins:
                if p.deleted_at:
                    continue
                path = root / p.plugin_id / p.plugin_digest
                if not path.is_dir() or path.is_symlink():
                    p.runtime_status, p.error_code = "ORPHANED", "PACKAGE_MISSING"
                    report["unavailable"].append(p.plugin_id)
            for parent in root.iterdir():
                if parent.name.startswith("."):
                    continue
                if parent.is_symlink() or not parent.is_dir():
                    quarantine(parent, root)
                    report["quarantined"].append(parent.name)
                    continue
                for path in parent.iterdir():
                    if (parent.name, path.name) not in committed | retained:
                        quarantine(path, root)
                        audit.event(db, "reconcile", parent.name[:96], result="quarantined", code="UNREGISTERED_PACKAGE")
                        report["quarantined"].append(parent.name + "/" + path.name)
            active_stages = {a.attempt_id for a in db.scalars(select(PluginInstallAttempt).where(
                PluginInstallAttempt.status.in_(["PREPARED", "MIGRATED"])))}
            for stage in (root / ".staging").iterdir():
                if stage.name not in active_stages and time.time() - stage.lstat().st_mtime > 3600:
                    quarantine(stage, root)
                    report["quarantined"].append(stage.name)
    return report
