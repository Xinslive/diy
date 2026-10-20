"""插件静态扫描的白名单契约、签名撤销与请求边界。

对应审查报告 [P1-1]（黑名单遗漏等价入口）、[P1-2]（撤销对已安装插件无效）、
[P2-4]（registrar.route）、[P2-5]（缺 body 返 500）、[P3-4]（资产匿名可读）。
"""
import json
from pathlib import Path

import pytest
from app.core.config import settings
from app.plugins.errors import PluginError
from app.plugins.installer import root_path
from app.plugins.manifest import plugin_digest
from app.plugins.security import (
    add_revoked_key,
    asset_token,
    scan_file,
    verify_archive,
    verify_directory,
)
from tests.plugin_fixtures import plugin_env

REPO_ROOT = Path(__file__).resolve().parents[2]

# 报告实测表里的模块：黑名单漏掉的等价入口 + 原本就拦住的。
DANGEROUS_MODULES = [
    "posix", "pty", "signal", "asyncio", "smtplib", "ftplib", "ssl", "runpy",
    "pkgutil", "code", "sqlalchemy", "psycopg2", "httpx", "urllib3", "inspect",
    "tempfile", "base64", "codecs", "zlib", "os", "subprocess", "socket",
    "ctypes", "pickle", "importlib", "builtins", "shutil", "pathlib", "urllib",
    "requests", "app", "app.core.db", "sys",
]

# 「取名后调用」的各种形态：旧扫描器只在 Call 位置判 Name，所以这些全部放行。
NAME_REFERENCE_FORMS = [
    "imp = __import__\n",
    "e = eval\n",
    "f = open\n",
    "c = compile\n",
    "g = getattr\n",
    "s = setattr\n",
    "gl = globals\n",
    "lo = locals\n",
    "exec('x')\n",
    "eval('x')\n",
    "open('/etc/passwd')\n",
    "getattr(object, '__class__')\n",
]

SUBSCRIPT_CALLS = [
    "func['eval']('x')\n",
    "handlers['run']()\n",
]


def rules_of(path, source, permissions=None):
    return {finding["rule"] for finding in scan_file(path, source.encode(), permissions=permissions)}


@pytest.mark.parametrize("module", DANGEROUS_MODULES)
def test_whitelist_rejects_every_non_whitelisted_module(module):
    assert "PY_IMPORT" in rules_of("backend/main.py", f"import {module}\n")
    assert "PY_IMPORT" in rules_of("backend/main.py", f"from {module} import thing\n")


@pytest.mark.parametrize("module", ["posix", "pty", "signal", "runpy", "code"])
def test_dangerous_attribute_access_is_judged(module):
    assert rules_of("backend/main.py", f"import {module}\n{module}.execv('/bin/sh', [])\n")


@pytest.mark.parametrize("source", NAME_REFERENCE_FORMS)
def test_taking_a_name_then_calling_it_is_rejected(source):
    assert "PY_CALL" in rules_of("backend/main.py", source)


@pytest.mark.parametrize("source", SUBSCRIPT_CALLS)
def test_subscript_call_form_is_rejected(source):
    assert "PY_SUBSCRIPT" in rules_of("backend/main.py", source)


def test_whitelist_allows_the_modules_plugins_actually_need():
    assert rules_of("backend/main.py", "import re\nfrom datetime import date\nfrom decimal import Decimal\n") == set()
    assert rules_of("backend/main.py", "import csv\nimport io\nimport json\nfrom typing import Optional\n") == set()
    assert rules_of("backend/main.py", "from fastapi import HTTPException, File, UploadFile\n") == set()
    assert rules_of("backend/main.py", "from openpyxl import load_workbook\n", ["upload:read"]) == set()
    # 受限模块必须逐个 from-import：裸 import 会拿到整个命名空间。
    assert "PY_IMPORT" in rules_of("backend/main.py", "import fastapi\n")
    assert "PY_IMPORT" in rules_of("backend/main.py", "from fastapi import testclient\n")


def test_openpyxl_requires_the_upload_capability():
    assert "PY_IMPORT" in rules_of("backend/main.py", "from openpyxl import load_workbook\n", [])
    assert "PY_IMPORT" in rules_of("backend/main.py", "from openpyxl import load_workbook\n", ["book:read"])


def test_migrations_may_use_ddl_but_not_raw_sql():
    migration = "migrations/versions/0001_x.py"
    assert rules_of(migration, "from alembic import op\nimport sqlalchemy as sa\n"
                               "op.create_table('t', sa.Column('id', sa.Integer))\n") == set()
    assert "PY_ATTRIBUTE" in rules_of(migration, "from alembic import op\nop.execute('UPDATE sys_user SET is_admin=true')\n")
    assert "PY_ATTRIBUTE" in rules_of(migration, "from alembic import op\nop.get_bind()\n")
    assert "PY_ATTRIBUTE" in rules_of(migration, "import sqlalchemy as sa\nsa.create_engine('postgresql://x')\n")
    assert "PY_IMPORT" in rules_of(migration, "from alembic import command\n")
    # 名字绑定与出现顺序无关：先使用、后 import 也要判。
    assert "PY_ATTRIBUTE" in rules_of(migration, "op.execute('UPDATE sys_user SET is_admin=true')\nfrom alembic import op\n")
    # 迁移文件里的 sqlalchemy 不能出现在后端代码里。
    assert "PY_IMPORT" in rules_of("backend/main.py", "import sqlalchemy as sa\n")


@pytest.mark.parametrize("folder", ["plugins/com.cwq.periodic", "plugins/com.cwq.reimburse",
                                    "docs/plugin-example", "docs/plugin-import-example"])
def test_real_plugin_sources_pass_the_whitelist(folder):
    root = REPO_ROOT / folder
    if not root.is_dir():
        pytest.skip("本机没有该插件源码")
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        findings = scan_file(relative, path.read_bytes(), permissions=manifest["permissions"])
        assert findings == [], f"{folder}/{relative}: {findings}"


def test_signed_package_with_dangerous_import_is_rejected(plugin_env):
    build, _ = plugin_env
    files = {"backend/main.py": b"import posix\ndef register(registrar, ctx):\n    pass\n",
             "frontend/index.js": b"export default () => ({});"}
    raw, manifest, keys, _ = build(files=files)
    with pytest.raises(PluginError) as exc:
        verify_archive(raw, keys=keys)
    assert exc.value.code == "STATIC_SCAN_REJECTED"
    assert {finding["rule"] for finding in exc.value.findings} == {"PY_IMPORT"}


def test_revoked_key_blocks_an_installed_plugin_on_the_load_path(plugin_env, client, auth, monkeypatch):
    build, restart = plugin_env
    raw, manifest, keys, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    digest = plugin_digest(manifest)
    directory = root_path() / pid / digest
    assert verify_directory(directory, digest, keys=keys).digest == digest

    monkeypatch.setattr(settings, "plugin_revoked_keys", "test")
    with pytest.raises(PluginError) as exc:
        verify_directory(directory, digest, keys=keys)
    assert exc.value.code == "KEY_REVOKED"

    # 加载期同样拒绝：worker 重启后这个插件进不来。
    restart()
    rows = client.get("/api/system/plugins", headers=auth).json()["plugins"]
    assert rows[0]["runtime_status"] == "UNAVAILABLE"
    assert rows[0]["error_code"] == "KEY_REVOKED"


def test_revoke_key_action_blocks_install_and_stops_loading(plugin_env, client, auth, plain_user):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    assert client.post("/api/system/plugins/revoke-key", headers=auth, json={"key_id": "test"}).status_code == 200
    # 运行时封禁名单落盘：撤销当次生效，不必改环境变量再重启。
    revoked_file = root_path() / ".revoked-keys"
    assert "test" in revoked_file.read_text(encoding="utf-8")
    # 新包被拒。
    with pytest.raises(PluginError) as exc:
        verify_archive(raw)
    assert exc.value.code == "KEY_REVOKED"
    # 已安装插件被停用，重启后不再加载。
    rows = client.get("/api/system/plugins", headers=auth).json()["plugins"]
    assert rows[0]["desired_enabled"] is False
    restart()
    rows = client.get("/api/system/plugins", headers=auth).json()["plugins"]
    assert rows[0]["runtime_status"] == "DISABLED"
    # 授权接口不受影响（管理动作只碰插件）。
    assert client.post(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200


def test_revoke_key_rejects_bad_identifiers(plugin_env, client, auth):
    build, _ = plugin_env
    for payload in ({"key_id": ""}, {"key_id": "a b"}, {"key_id": "x" * 100}, {"key_id": "ok", "extra": 1}):
        assert client.post("/api/system/plugins/revoke-key", headers=auth, json=payload).status_code in {400, 422}
    with pytest.raises(PluginError):
        add_revoked_key("bad key")


POST_PLUGIN = b'''
from fastapi import HTTPException


def register(registrar, ctx):
    def save_note(payload: dict):
        return {"got": payload["x"]}

    registrar.post("/save", save_note)
'''

ROUTER_PLUGIN = b'''
from fastapi import APIRouter


def register(registrar, ctx):
    router = APIRouter()

    @router.get("/ping")
    def ping():
        return {"pong": True}

    registrar.route(router)
'''


def _install_and_grant(client, auth, plain_user, build, files):
    raw, manifest, _, _ = build(files=files)
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    return pid


def test_plugin_post_without_body_returns_422(plugin_env, client, auth, plain_auth, plain_user):
    build, restart = plugin_env
    pid = _install_and_grant(client, auth, plain_user, build,
                             {"backend/main.py": POST_PLUGIN, "frontend/index.js": b"export default () => ({});"})
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200
    url = f"/api/plugins/{pid}/save"
    assert client.post(url, headers=plain_auth, json={"x": 7}).json() == {"got": 7}
    missing = client.post(url, headers=plain_auth)
    assert missing.status_code == 422, missing.text
    assert "缺少请求体" in missing.text
    assert client.post(url, headers=plain_auth, json={}).status_code == 422


def test_registrar_route_without_namespace_works(plugin_env, client, auth, plain_auth, plain_user):
    build, restart = plugin_env
    pid = _install_and_grant(client, auth, plain_user, build,
                             {"backend/main.py": ROUTER_PLUGIN, "frontend/index.js": b"export default () => ({});"})
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200
    r = client.get(f"/api/plugins/{pid}/ping", headers=plain_auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"pong": True}


def test_asset_route_rejects_anonymous_requests(plugin_env, client, auth):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    digest = plugin_digest(manifest)
    base = f"/api/plugins/assets/{pid}/{digest}/frontend"
    assert client.get(base + "/index.js").status_code == 401
    assert client.get(f"{base}/_t/{asset_token(pid, digest)}/index.js").status_code == 200
    # 别的包的票据不通用。
    assert client.get(f"{base}/_t/{asset_token('com.other', digest)}/index.js").status_code == 401
