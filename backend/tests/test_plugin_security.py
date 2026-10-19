import io
import json
import stat
import zipfile
import pytest
from app.core.config import settings
from app.core.uploads import UploadTooLarge
from app.plugins.errors import PluginError
from app.plugins.security import verify_archive, verify_directory, scan_file
from tests.plugin_helpers import package, archive


def test_signed_package_roundtrip(tmp_path):
    raw, manifest, keys, _ = package(tables=True)
    verified = verify_archive(raw, keys=keys)
    verified.extract(tmp_path / "package")
    assert verify_directory(tmp_path / "package", verified.digest, keys=keys).digest == verified.digest
    assert len(verified.digest) == 64


@pytest.mark.parametrize("path,source", [("backend/main.py", b"def register(r,c): pass\n"),
    ("frontend/index.js", b"export default {};"), ("migrations/versions/0001_items.py", b"revision='bad'"),
    ("frontend/readme.txt", b"changed")])
def test_every_member_is_signed(path, source):
    files = {"backend/main.py": b"def register(r,c): pass\n", "frontend/index.js": b"export default {};",
             "migrations/versions/0001_items.py": b"revision='one'", "frontend/readme.txt": b"original"}
    raw, manifest, keys, _ = package(files=files)
    files[path] = source + b"tamper"
    with pytest.raises(PluginError, match="完整性"):
        verify_archive(archive(manifest, files), keys=keys)


@pytest.mark.parametrize("name", ["../bad", "/bad", "backend\\main.py", "x/../../bad", "x\0bad", "C:/bad", "x//bad"])
def test_archive_unsafe_paths(name):
    raw, manifest, keys, _ = package()
    with pytest.raises(PluginError):
        verify_archive(archive(manifest, {name: b"bad"}), keys=keys)


def test_signature_missing_revoked_wrong_and_symlink(monkeypatch):
    raw, manifest, keys, _ = package()
    with pytest.raises(PluginError):
        verify_archive(raw, keys={})
    monkeypatch.setattr(settings, "plugin_revoked_keys", "test")
    with pytest.raises(PluginError):
        verify_archive(raw, keys=keys)
    monkeypatch.setattr(settings, "plugin_revoked_keys", "")
    manifest["signature"]["value"] = "bad"
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        files = {n: z.read(n) for n in z.namelist() if n != "plugin.json"}
    with pytest.raises(PluginError):
        verify_archive(archive(manifest, files), keys=keys)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        info = zipfile.ZipInfo("backend/main.py")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    with pytest.raises(PluginError):
        verify_archive(out.getvalue(), keys=keys)


def test_resource_limits_and_obvious_dangerous_code(monkeypatch):
    raw, _, keys, _ = package()
    monkeypatch.setattr(settings, "plugin_max_files", 1)
    with pytest.raises(UploadTooLarge):
        verify_archive(raw, keys=keys)
    assert scan_file("backend/main.py", b"import subprocess\nsubprocess.run(['x'])")
    assert scan_file("frontend/index.js", b"eval('x')")
    assert scan_file("x.so", b"ELF")
