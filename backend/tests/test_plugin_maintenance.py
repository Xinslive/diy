"""插件存储配额、备份区清理与备份 catalog 的分域隔离。

对应审查报告 [P2-2]（.backups/.quarantine 计入配额且无清理路径）、
[P2-3]（一个插件的表漂移让全部备份失败）。
"""
from pathlib import Path

import pytest
from sqlalchemy import text
from app.backup.export import SCOPE_BOOK, SCOPE_SYSTEM, export_scope, read_snapshot
from app.core.config import settings
from app.core.db import engine
from app.plugins.backup import catalog
from app.plugins.installer import (
    code_usage,
    install,
    prune_reserved,
    reserved_quota,
    reserved_usage,
    root_path,
)
from app.plugins.manifest import table_prefix
from app.plugins.models import PluginRegistry
from tests.plugin_fixtures import plugin_env


def test_reserved_directories_do_not_consume_the_code_quota(plugin_env, client, auth, monkeypatch):
    build, _ = plugin_env
    monkeypatch.setattr(settings, "plugin_disk_quota_bytes", 4096)
    root = root_path()
    # 备份区塞满：以前这会让 install/upgrade/delete/cleanup 四条路径全部被拒，
    # 而且系统没有回收它们的入口 —— 配额耗尽即永久锁死。
    junk = root / ".backups" / "orphan"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "big.bin").write_bytes(b"x" * 8192)
    quarantine = root / ".quarantine" / "com.test.gone-abcdef"
    quarantine.mkdir(parents=True, exist_ok=True)
    (quarantine / "code.py").write_bytes(b"y" * 4096)

    assert code_usage(root) == 0
    assert reserved_usage(root) > settings.plugin_disk_quota_bytes

    raw, manifest, _, _ = build()
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    # 备份区到顶只影响备份流程，不再把插件管理锁死。
    assert code_usage(root) > 0
    assert code_usage(root) <= settings.plugin_disk_quota_bytes


def test_prune_removes_unreferenced_backups_but_keeps_referenced_ones(plugin_env, client, auth, admin_user, monkeypatch):
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    install(raw, admin_user)
    root = root_path()
    referenced = root / ".backups" / "referenced"
    referenced.mkdir(parents=True, exist_ok=True)
    (referenced / "system.json.gz").write_bytes(b"kept")
    orphan = root / ".backups" / "orphan"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "system.json.gz").write_bytes(b"dropped")
    quarantine = root / ".quarantine" / "com.test.gone-abcdef"
    quarantine.mkdir(parents=True, exist_ok=True)
    (quarantine / "code.py").write_bytes(b"z")

    # 先把 referenced 标成 registry 正在引用的备份：它必须被保留。
    from app.core.db import SessionLocal
    with SessionLocal.begin() as db:
        row = db.get(PluginRegistry, manifest["id"])
        row.backup_path = str(referenced)

    removed = prune_reserved(root, force=True)
    assert str(orphan) in removed
    assert not orphan.exists()
    assert not quarantine.exists()
    # 被 registry 引用的备份一律保留。
    assert str(referenced) not in removed
    assert referenced.exists()


def test_prune_endpoint_is_admin_only(plugin_env, client, auth, plain_auth):
    build, _ = plugin_env
    assert client.post("/api/system/plugins/maintenance/prune", headers=auth, json={"force": True}).status_code == 200
    assert client.post("/api/system/plugins/maintenance/prune", headers=plain_auth, json={"force": True}).status_code in {403, 404}


def test_catalog_isolates_a_drifted_plugin_from_the_rest(plugin_env, admin_user, book, tmp_path):
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    install(raw, admin_user)
    name = table_prefix(manifest["id"]) + "items"
    with engine.begin() as conn:
        conn.execute(text(f'INSERT INTO "{name}" (book_id,label) VALUES (:book,:label)'),
                     {"book": book.id, "label": "插件数据"})
        conn.execute(text(f'ALTER TABLE "{name}" ADD COLUMN drift TEXT'))

    out = tmp_path / "system.json.gz"
    with engine.connect() as conn:
        result = catalog(conn)
    entries, tables = result
    warnings = result.warnings
    assert warnings and warnings[0]["id"] == manifest["id"]
    assert warnings[0]["code"] == "TABLE_METADATA_MISMATCH"
    assert name in warnings[0]["tables"]
    # 漂移插件被跳过，其余内容照常可备份。
    assert [entry["id"] for entry in entries if entry.get("drifted")] == [manifest["id"]]
    assert name not in {table.name for table in tables}

    # 严格模式（恢复路径）仍然拒绝，并指出是哪个插件。
    from app.plugins.errors import PluginError
    with engine.connect() as conn:
        with pytest.raises(PluginError) as exc:
            catalog(conn, strict=True)
    assert manifest["id"] in exc.value.message

    # 系统备份与账套备份都不再被这一个插件拖垮。
    system_out = tmp_path / "system2.json.gz"
    from app.core.db import SessionLocal
    with SessionLocal() as db:
        export_scope(db, scope=SCOPE_SYSTEM, book_id=None, book_label="系统", out_path=system_out)
    assert system_out.exists()
    assert read_snapshot(system_out)["header"]["plugins"][0]["drifted"] is True
