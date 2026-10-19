from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.audit import AuditLog
from app.plugins.models import PluginRegistry
from tests.plugin_fixtures import plugin_env


def test_install_requires_admin_has_no_default_users_and_pending_restart(plugin_env, client, auth, plain_auth):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("test.cwp", raw)}, headers=plain_auth).status_code == 403
    result = client.post("/api/system/plugins/install", files={"file": ("test.cwp", raw)}, headers=auth)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "pending_restart"
    assert client.get(f"/api/system/plugins/{pid}/users", headers=auth).json() == {"users": []}
    assert client.get("/api/plugins/manifest", headers=auth).json() == {"plugins": []}
    restart()
    assert client.get("/api/plugins/manifest", headers=auth).json() == {"plugins": []}
    assert client.get(f"/api/plugins/{pid}/hello", headers=auth).status_code == 404
    assert client.get("/api/system/plugins", headers=auth).json()["plugins"][0]["runtime_status"] == "READY"
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code == 200


def test_failed_install_is_audited_and_cleans_staging(plugin_env, client, auth):
    from pathlib import Path
    from app.core.config import settings
    result = client.post("/api/system/plugins/install", files={"file": ("bad.cwp", b"not zip")}, headers=auth)
    assert result.status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "plugin.install", AuditLog.after["result"].astext == "failed"))
    assert not list((Path(settings.plugin_root) / ".staging").glob("*"))
