from pathlib import Path
import os
import subprocess
import sys
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.plugins.installer import install, root_path
from app.plugins.models import PluginInstallAttempt, PluginRegistry
from app.plugins.reconcile import reconcile
from tests.plugin_fixtures import plugin_env


@pytest.mark.parametrize('point', ['before_rename', 'after_rename', 'commit_uncertain'])
def test_journal_recovers_committed_migration_without_reexecution(plugin_env, admin_user, monkeypatch, point):
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    from app.plugins import installer
    original_rename, original_commit = installer.os.rename, installer.commit_attempt
    if point == 'before_rename':
        def fail(*args):
            raise OSError('crash before rename')
        monkeypatch.setattr(installer.os, 'rename', fail)
    else:
        def fail(attempt):
            if point == 'commit_uncertain':
                original_commit(attempt)
            raise OSError('connection lost around registry commit')
        monkeypatch.setattr(installer, 'commit_attempt', fail)
    with pytest.raises(OSError):
        install(raw, admin_user)
    monkeypatch.setattr(installer.os, 'rename', original_rename)
    monkeypatch.setattr(installer, 'commit_attempt', original_commit)
    from app.plugins import reconcile as recovery
    monkeypatch.setattr(recovery, 'commit_attempt', original_commit)
    reconcile()
    with SessionLocal() as db:
        attempt = db.scalar(select(PluginInstallAttempt).where(PluginInstallAttempt.plugin_id == manifest['id']))
        plugin = db.get(PluginRegistry, manifest['id'])
        assert attempt.status == 'COMMITTED'
        assert plugin.pending_attempt_id == attempt.attempt_id
        assert (root_path() / plugin.plugin_id / plugin.plugin_digest).is_dir()
        assert not Path(attempt.staging_path).exists()


def test_orphan_missing_and_unknown_directory_are_not_loaded(plugin_env, admin_user):
    build, restart = plugin_env
    raw, m, _, _ = build()
    install(raw, admin_user)
    with SessionLocal() as db:
        p = db.get(PluginRegistry, m['id'])
        folder = root_path() / p.plugin_id / p.plugin_digest
    folder.rename(folder.parent / 'unregistered')
    report = reconcile()
    assert m['id'] in report['unavailable']
    assert report['quarantined']
    runtime = restart()
    assert m['id'] not in runtime.handlers


@pytest.mark.parametrize("point", ["before_rename", "after_rename", "commit_uncertain"])
def test_real_process_exit_keeps_durable_journal_and_releases_locks(plugin_env, admin_user, tmp_path, point):
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    bundle = tmp_path / "interrupted.cwp"
    bundle.write_bytes(raw)
    with engine.connect() as conn:
        main_head = conn.scalar(text("SELECT version_num FROM alembic_version"))
    source = r"""
import os
import sys
from pathlib import Path
from app.core.db import SessionLocal
from app.models.system import SysUser
from app.plugins import installer
point, bundle, actor = sys.argv[1:]
if point == "before_rename":
    installer.os.rename = lambda *args: os._exit(71)
else:
    original = installer.commit_attempt
    def interrupted(attempt):
        if point == "commit_uncertain":
            original(attempt)
        os._exit(71)
    installer.commit_attempt = interrupted
with SessionLocal() as db:
    installer.install(Path(bundle).read_bytes(), db.get(SysUser, int(actor)))
"""
    child = subprocess.run([sys.executable, "-c", source, point, str(bundle), str(admin_user.id)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=25,
        env=dict(os.environ, APP_ENV="test", PLUGIN_ENABLED="true", PLUGIN_ROOT=settings.plugin_root,
                 PLUGIN_TRUSTED_KEYS=settings.plugin_trusted_keys))
    assert child.returncode == 71, child.stdout + child.stderr
    with SessionLocal() as db:
        attempt = db.scalar(select(PluginInstallAttempt).where(PluginInstallAttempt.plugin_id == manifest["id"]))
        assert attempt.status == ("COMMITTED" if point == "commit_uncertain" else "MIGRATED")
    # os._exit 不执行 finally：这里同时证明 OS/数据库锁已释放，DDL 不会被重跑。
    reconcile()
    reconcile()
    from app.plugins.migrations import read_version
    with SessionLocal() as db:
        attempt = db.scalar(select(PluginInstallAttempt).where(PluginInstallAttempt.plugin_id == manifest["id"]))
        plugin = db.get(PluginRegistry, manifest["id"])
        assert attempt.status == "COMMITTED"
        assert plugin.pending_attempt_id == attempt.attempt_id
        assert (root_path() / plugin.plugin_id / plugin.plugin_digest).is_dir()
        assert not Path(attempt.staging_path).exists()
        assert len(plugin.tables) == 1
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == main_head
        assert read_version(conn, manifest["id"]) == "0001_items"


def test_real_registry_connection_loss_preserves_committed_migration(plugin_env, admin_user, monkeypatch):
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    from app.plugins import installer
    original = installer.commit_attempt
    disconnected = []
    def interrupted(attempt):
        def terminate_before_commit(conn):
            backend = conn.scalar(text("SELECT pg_backend_pid()"))
            with engine.connect() as killer:
                assert killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend})
            disconnected.append(backend)
        event.listen(engine, "commit", terminate_before_commit, once=True)
        try:
            original(attempt)
        finally:
            event.remove(engine, "commit", terminate_before_commit)
    monkeypatch.setattr(installer, "commit_attempt", interrupted)
    with pytest.raises(OperationalError):
        install(raw, admin_user)
    assert len(disconnected) == 1
    monkeypatch.setattr(installer, "commit_attempt", original)
    reconcile()
    with SessionLocal() as db:
        attempt = db.scalar(select(PluginInstallAttempt).where(PluginInstallAttempt.plugin_id == manifest["id"]))
        plugin = db.get(PluginRegistry, manifest["id"])
        assert attempt.status == "COMMITTED"
        assert plugin.pending_attempt_id == attempt.attempt_id
        assert (root_path() / plugin.plugin_id / plugin.plugin_digest).is_dir()
