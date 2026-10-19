"""安装前的包完整性及明显危险代码检查。静态扫描不是沙箱。"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.config import settings
from app.core.uploads import RowBudget, UploadTooLarge, check_archive_size
from app.plugins.errors import PluginError
from app.plugins.manifest import canonical_payload, parse_manifest, plugin_digest, safe_path

TEXT_TYPES = {".py", ".js", ".json", ".css", ".sql", ".md", ".txt"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
BAD_IMPORTS = {"subprocess", "socket", "ctypes", "pickle", "marshal", "shelve", "dill", "multiprocessing", "importlib", "builtins", "os", "sys", "shutil", "pathlib", "urllib", "requests", "http", "psycopg"}
BAD_CALLS = {"eval", "exec", "compile", "__import__", "open", "getattr", "setattr", "delattr", "globals", "locals", "breakpoint", "input"}
BAD_ATTRIBUTES = {"write", "write_text", "write_bytes", "unlink", "rmdir", "mkdir", "system", "popen", "connect", "load_library", "autocommit_block", "execution_options", "commit", "rollback"}


@dataclass
class VerifiedPackage:
    manifest: dict
    digest: str
    files: dict[str, bytes]
    archive_digest: str = ""

    def extract(self, destination: Path):
        # destination 必须是宿主新建目录；不使用 ZipFile.extract/extractall。
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for path, data in self.files.items():
            target = destination / safe_path(path)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise PluginError("UNSAFE_PATH", "解压路径越界")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o400)
        target = destination / "plugin.json"
        with target.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(self.manifest, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        for folder in sorted((p for p in destination.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        target.chmod(0o400)


def trusted_keys() -> dict:
    try:
        keys = json.loads(settings.plugin_trusted_keys)
        if not isinstance(keys, dict):
            raise ValueError
        return keys
    except (ValueError, TypeError) as exc:
        raise PluginError("INVALID_TRUST_CONFIGURATION", "插件可信密钥配置错误", 503) from exc


def verify_signature(manifest: dict, keys=None, *, historical=False):
    keys = trusted_keys() if keys is None else keys
    sig = manifest["signature"]
    key = keys.get(sig["key_id"])
    revoked = {k.strip() for k in settings.plugin_revoked_keys.split(",") if k.strip()}
    historical_ids = {k.strip() for k in settings.plugin_historical_keys.split(",") if k.strip()}
    if key is None or (not historical and (sig["key_id"] in revoked or sig["key_id"] in historical_ids)):
        raise PluginError("UNTRUSTED_SIGNATURE", "签名密钥不受信任或已撤销")
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True))
        public.verify(base64.b64decode(sig["value"], validate=True), canonical_payload(manifest))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PluginError("INVALID_SIGNATURE", "插件签名校验失败") from exc


def scan_file(path: str, data: bytes) -> list[dict]:
    suffix = Path(path).suffix.lower()
    findings = []
    def finding(rule, line=1):
        findings.append({"file": path, "line": line, "rule": rule})
    if suffix in IMAGE_TYPES:
        valid = ((suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                 or (suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"))
                 or (suffix == ".webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
        if not valid:
            finding("RESOURCE_TYPE")
        return findings
    if suffix not in TEXT_TYPES:
        finding("RESOURCE_TYPE")
        return findings
    try:
        source = data.decode("utf-8")
    except UnicodeError:
        finding("RESOURCE_ENCODING")
        return findings
    if "\0" in source or source.startswith("#!"):
        finding("EXECUTABLE_RESOURCE")
    if suffix == ".py":
        try:
            tree = ast.parse(source, filename=path)
        except (SyntaxError, RecursionError):
            finding("PY_SYNTAX")
            return findings
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(n.split(".")[0] in BAD_IMPORTS or n == "app" or n.startswith("app.") for n in names):
                    finding("PY_IMPORT", node.lineno)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in BAD_CALLS:
                    finding("PY_CALL", node.lineno)
                if isinstance(node.func, ast.Attribute) and node.func.attr in BAD_ATTRIBUTES:
                    finding("PY_CALL", node.lineno)
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                finding("PY_REFLECTION", node.lineno)
    elif suffix in {".js", ".css", ".sql"}:
        patterns = {
            ".js": [(r"\b(?:eval\s*\(|(?:new\s+)?Function\s*\(|import\s*\(|import\s+|export\s+[^;]*?\sfrom\s|require\s*\(|WebSocket\s*\(|Worker\s*\()", "JS_DYNAMIC_CODE"),
                    (r"(?:<\s*script|javascript\s*:|\.innerHTML\s*=|document\.write\s*\(|\bon\w+\s*=)", "JS_INLINE_CODE")],
            ".css": [(r"@import|expression\s*\(|javascript\s*:|url\s*\(\s*['\"]?(?:https?:|//|data:)", "CSS_EXTERNAL_CODE")],
            ".sql": [(r"\b(?:COMMIT|ROLLBACK|VACUUM|COPY|CREATE\s+EXTENSION|ALTER\s+SYSTEM|DO\s+\$|CONCURRENTLY)\b", "SQL_TRANSACTION")],
        }[suffix]
        for pattern, rule in patterns:
            for match in re.finditer(pattern, source, re.I):
                finding(rule, source[:match.start()].count("\n") + 1)
    return findings


def verify_archive(data: bytes, *, keys=None) -> VerifiedPackage:
    cap = min(settings.plugin_max_bytes, settings.import_max_bytes)
    if len(data) > cap:
        raise UploadTooLarge("插件包超过上传上限")
    check_archive_size(data, limit=min(settings.plugin_max_unzipped_bytes, settings.import_max_unzipped_bytes), label="插件包")
    budget = RowBudget(settings.plugin_max_files, what="个插件文件")
    files, seen, total = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                budget.tick()
                path = safe_path(member.orig_filename)
                if member.is_dir() or path.casefold() in seen:
                    raise PluginError("UNSAFE_ARCHIVE", "插件包包含目录成员、重复文件或大小写冲突")
                seen.add(path.casefold())
                mode = member.external_attr >> 16
                if (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or mode & 0o7111 or member.flag_bits & 1:
                    raise PluginError("UNSAFE_ARCHIVE", "插件包包含链接、特殊成员、可执行文件或加密文件")
                if member.file_size > settings.plugin_max_file_bytes:
                    raise UploadTooLarge("插件单文件超过上限")
                with archive.open(member) as handle:
                    content = handle.read(settings.plugin_max_file_bytes + 1)
                if len(content) > settings.plugin_max_file_bytes:
                    raise UploadTooLarge("插件单文件超过上限")
                total += len(content)
                if total > min(settings.plugin_max_unzipped_bytes, settings.import_max_unzipped_bytes):
                    raise UploadTooLarge("插件包解压体积超过上限")
                files[path] = content
    except UploadTooLarge:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, ValueError) as exc:
        raise PluginError("INVALID_ARCHIVE", "插件包不是有效的安全 ZIP") from exc
    if "plugin.json" not in files:
        raise PluginError("INVALID_MANIFEST", "包根目录缺少唯一的 plugin.json")
    raw_manifest = files.pop("plugin.json")
    if len(raw_manifest) > 256 * 1024:
        raise UploadTooLarge("plugin.json 超过上限")
    manifest = parse_manifest(raw_manifest)
    declared = {item["path"]: item for item in manifest["files"]}
    if set(files) != set(declared):
        raise PluginError("FILE_SET_MISMATCH", "包内文件集合与签名清单不一致")
    for path, content in files.items():
        item = declared[path]
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise PluginError("FILE_DIGEST_MISMATCH", "插件文件完整性校验失败")
    verify_signature(manifest, keys)
    findings = [finding for path, content in files.items() for finding in scan_file(path, content)]
    if findings:
        raise PluginError("STATIC_SCAN_REJECTED", "插件包含不允许的代码或资源", findings=findings)
    return VerifiedPackage(manifest, plugin_digest(manifest), files, hashlib.sha256(data).hexdigest())


def verify_directory(directory: Path, expected_digest: str, *, keys=None) -> VerifiedPackage:
    if directory.is_symlink() or directory.parent.is_symlink() or not directory.is_dir():
        raise PluginError("PACKAGE_MISSING", "插件代码目录缺失")
    manifest_path = directory / "plugin.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_nlink != 1 or manifest_path.stat().st_size > 256 * 1024:
        raise PluginError("INVALID_MANIFEST", "插件目录缺少安全 manifest")
    manifest = parse_manifest(manifest_path.read_bytes())
    if plugin_digest(manifest) != expected_digest:
        raise PluginError("FILE_DIGEST_MISMATCH", "插件目录 digest 不一致")
    verify_signature(manifest, keys, historical=True)
    files = {}
    actual = set()
    for target in directory.rglob("*"):
        if len(actual) > settings.plugin_max_files:
            raise PluginError("FILE_SET_MISMATCH", "插件目录文件过多")
        mode = target.lstat()
        if target.is_symlink() or (not stat.S_ISREG(mode.st_mode) and not stat.S_ISDIR(mode.st_mode)) or (stat.S_ISREG(mode.st_mode) and mode.st_nlink != 1):
            raise PluginError("UNSAFE_PATH", "插件目录包含链接或特殊文件")
        if target.is_file():
            actual.add(target.relative_to(directory).as_posix())
    if actual != {"plugin.json", *(item["path"] for item in manifest["files"])}:
        raise PluginError("FILE_SET_MISMATCH", "插件目录文件清单不一致")
    for item in manifest["files"]:
        target = directory / item["path"]
        if not target.is_file() or target.stat().st_size != item["size"] or target.stat().st_size > settings.plugin_max_file_bytes:
            raise PluginError("FILE_DIGEST_MISMATCH", "插件目录文件大小不符")
        content = target.read_bytes()
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise PluginError("FILE_DIGEST_MISMATCH", "插件目录文件被修改")
        files[item["path"]] = content
    return VerifiedPackage(manifest, expected_digest, files)
