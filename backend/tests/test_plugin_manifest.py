import copy
import pytest
from app.plugins.errors import PluginError
from app.plugins.manifest import parse_manifest, validate_manifest, table_prefix, version_table, canonical_payload
from tests.plugin_helpers import package


@pytest.mark.parametrize("path,value", [
    (("id",), "../bad"), (("version",), "1.0"), (("api_version",), ">=99"),
    (("permissions",), ["admin:all"]), (("hooks",), ["startup"]),
    (("access", "default"), "installer"), (("access", "users"), [1]),
    (("contributes", "backend_entry"), "../main.py"),
    (("contributes", "menu", "route"), "/books"),
    (("contributes", "menu", "module"), "system"),
    (("contributes", "menu", "icon"), []),
    (("contributes", "menu", "name"), "host"),
    (("files",), []), (("signature", "algo"), "none"),
    (("tables",), [{"name": "host_table", "scope": "book"}]),
])
def test_manifest_rejects_invalid_contract(path, value):
    _, manifest, _, _ = package()
    target = manifest
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(PluginError):
        validate_manifest(manifest)


def test_duplicate_json_and_canonical_signature():
    with pytest.raises(PluginError):
        parse_manifest(b'{"id":"a","id":"b"}')
    _, manifest, _, _ = package()
    reordered = dict(reversed(list(manifest.items())))
    assert canonical_payload(reordered) == canonical_payload(manifest)
    assert table_prefix(manifest["id"])[7:19] == version_table(manifest["id"])[22:]


@pytest.mark.parametrize("task", [
    {"name": "hourly_sync", "schedule": {"type": "interval", "minutes": 15}},
    {"name": "nightly_sync", "schedule": {"type": "cron", "expression": "10 2 * * *"}},
    {"name": "nightly_sync", "schedule": {"type": "cron", "expression": "10 2 * * *", "timezone": "Asia/Shanghai"}},
])
def test_manifest_accepts_scheduled_task(task):
    _, manifest, _, _ = package(edit=lambda value: value.update(tasks=[task]))
    assert manifest["tasks"] == [task]


@pytest.mark.parametrize("tasks", [
    [{"name": "bad-name", "schedule": {"type": "interval", "minutes": 1}}],
    [{"name": "daily", "schedule": {"type": "interval", "minutes": 0}}],
    [{"name": "daily", "schedule": {"type": "interval", "minutes": True}}],
    [{"name": "daily", "schedule": {"type": "cron", "expression": "not cron"}}],
    [{"name": "daily", "schedule": {"type": "cron", "expression": "0 0 * * *", "timezone": "UTC"}}],
    [
        {"name": "daily", "schedule": {"type": "interval", "minutes": 1}},
        {"name": "daily", "schedule": {"type": "interval", "minutes": 2}},
    ],
])
def test_manifest_rejects_invalid_scheduled_tasks(tasks):
    with pytest.raises(PluginError):
        package(edit=lambda manifest: manifest.update(tasks=tasks))


def test_old_sdk_range_keeps_existing_plugins_but_cannot_declare_tasks():
    package(edit=lambda manifest: manifest.update(api_version=">=1.1.0 <1.2.0"))
    with pytest.raises(PluginError, match="SDK 1.2"):
        package(edit=lambda manifest: manifest.update(api_version=">=1.1.0 <1.2.0", tasks=[
            {"name": "daily", "schedule": {"type": "interval", "minutes": 60}},
        ]))


def test_impossible_cron_is_rejected_at_installation():
    with pytest.raises(PluginError):
        package(edit=lambda manifest: manifest.update(tasks=[
            {"name": "daily", "schedule": {"type": "cron", "expression": "0 0 31 2 *"}},
        ]))
