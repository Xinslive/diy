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
