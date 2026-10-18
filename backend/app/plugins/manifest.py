""".cwp 契约、RFC 8785 签名 payload 和安全命名。解析不执行包内代码。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import PurePosixPath

import rfc8785
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from app.plugins import API_VERSION
from app.plugins.errors import PluginError

ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z][a-z0-9]*){1,10}\Z")
SEMVER_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
DIGEST_RE = re.compile(r"[a-f0-9]{64}\Z")
# ``subject:read`` 是为「按外部文件批量制证」补的：``VoucherIn`` 的分录要的是
# ``subject_id``（科目表主键），而插件拿到的往往是一张写着「6001」的表。
# 宿主有 ``/subjects/flat`` 能做 code→id 映射，但插件此前**没有任何读科目的
# 能力**，于是这条路径只剩两个走不通的走法：把 id 硬编码进插件（换个账套就
# 全错），或者绕开能力模型自己去查宿主表（正好是这套窄 SDK 要防的事）。
# 它只读、不写，且和 ``book:read`` 一样每次调用重读当前用户状态。
PERMISSIONS = frozenset({"ledger:read", "book:read", "book:write", "subject:read", "voucher:write", "plugin_data:rw", "audit:write", "settings:rw"})
HOOKS = frozenset({"on_period_close"})
ICONS = frozenset({"Calendar", "Grid", "Document", "DataAnalysis", "Tools", "Coin", "Folder"})


def fail(message, code="INVALID_MANIFEST"):
    raise PluginError(code, message)


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(0xD800 <= ord(c) <= 0xDFFF for c in value) or len(value.encode("utf-8")) > 240:
        fail("包内路径为空或过长", "UNSAFE_PATH")
    value = unicodedata.normalize("NFC", value)
    if any(ord(c) < 32 for c in value) or "\\" in value or ":" in value or "%" in value \
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        fail("包内路径不安全", "UNSAFE_PATH")
    if len(PurePosixPath(value).parts) > 10:
        fail("包内目录层数超过上限", "UNSAFE_PATH")
    return value


def table_prefix(plugin_id: str) -> str:
    return "plugin_" + hashlib.sha256(plugin_id.encode()).hexdigest()[:12] + "_"


def version_table(plugin_id: str) -> str:
    return "plugin_schema_version_" + hashlib.sha256(plugin_id.encode()).hexdigest()[:12]


def _object(value, fields, required=()):
    if not isinstance(value, dict) or set(value) - set(fields) or not set(required) <= set(value):
        fail("manifest 字段缺失、类型错误或包含未知字段")


def _text(value, cap=128):
    if not isinstance(value, str) or not value.strip() or len(value) > cap or any(ord(c) < 32 for c in value):
        fail("manifest 文本字段无效")


def _unique_list(value, allowed):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value) \
            or len(set(value)) != len(value) or not set(value) <= allowed:
        fail("manifest 的权限或 hook 不在白名单中")


def parse_manifest(raw: bytes) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                fail("plugin.json 包含重复键")
            out[key] = value
        return out
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                         parse_constant=lambda _: fail("不接受非有限 JSON 数字"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PluginError("INVALID_MANIFEST", "plugin.json 不是有效 UTF-8 JSON") from exc
    return validate_manifest(doc)


def validate_manifest(source: dict) -> dict:
    doc = copy.deepcopy(source)
    _object(doc, {"format_version", "id", "name", "version", "api_version", "description", "permissions", "access", "contributes", "hooks", "tables", "files", "signature"},
            {"format_version", "id", "name", "version", "api_version", "permissions", "access", "contributes", "files", "signature"})
    if type(doc["format_version"]) is not int or doc["format_version"] != 1:
        fail("不支持的插件包格式")
    pid = doc["id"]
    if not isinstance(pid, str) or len(pid) > 96 or not ID_RE.fullmatch(pid) or pid in {"plugin.registry", "plugin.worker"}:
        fail("插件 id 无效")
    _text(doc["name"])
    if "description" in doc:
        _text(doc["description"], 1000)
    if not isinstance(doc["version"], str) or len(doc["version"]) > 64 or not SEMVER_RE.fullmatch(doc["version"]):
        fail("插件 version 必须是 SemVer")
    try:
        spec = doc["api_version"]
        if not isinstance(spec, str) or not spec or len(spec) > 128:
            fail("api_version 无效")
        spec = re.sub(r"\s+(?=[<>=!~])", ",", spec.strip())
        if Version(API_VERSION) not in SpecifierSet(spec):
            fail("插件不兼容当前宿主 SDK", "INCOMPATIBLE_API")
    except (InvalidSpecifier, TypeError) as exc:
        raise PluginError("INVALID_MANIFEST", "api_version 无效") from exc
    _unique_list(doc["permissions"], PERMISSIONS)
    _unique_list(doc.get("hooks", []), HOOKS)
    _object(doc["access"], {"assignment", "default"}, {"assignment", "default"})
    if doc["access"] != {"assignment": "admin_users", "default": "none"}:
        fail("插件必须由管理员显式授权且默认无人可用")
    contributes = doc["contributes"]
    _object(contributes, {"menu", "backend_entry", "frontend_entry"}, {"backend_entry"})
    for field, suffix in (("backend_entry", ".py"), ("frontend_entry", ".js")):
        if field in contributes:
            contributes[field] = safe_path(contributes[field])
            if not contributes[field].startswith(field.split("_")[0] + "/") or not contributes[field].endswith(suffix):
                fail("插件入口路径或类型错误")
    if ("frontend_entry" in contributes) != ("menu" in contributes):
        fail("前端入口必须声明菜单")
    if "menu" in contributes:
        menu = contributes["menu"]
        _object(menu, {"module", "title", "icon", "order", "route", "name", "key"}, {"module", "title", "icon", "route"})
        _text(menu["title"])
        if menu["module"] != "extensions" or not isinstance(menu["icon"], str) or menu["icon"] not in ICONS:
            fail("插件菜单只能位于扩展命名空间且使用白名单图标")
        route = menu["route"]
        if not isinstance(route, str) or not re.fullmatch(r"/plugin/" + re.escape(pid) + r"(?:/[a-z0-9-]+)*", route):
            fail("插件 route 超出命名空间")
        for key in ("name", "key"):
            if key in menu and menu[key] != f"plugin:{pid}:main":
                fail("插件菜单 key/route name 不合法")
        if "order" in menu and (type(menu["order"]) is not int or not 0 <= menu["order"] <= 10000):
            fail("菜单 order 无效")
    tables = doc.get("tables", [])
    if not isinstance(tables, list) or len(tables) > 32:
        fail("插件表声明无效")
    seen = set()
    for table in tables:
        _object(table, {"name", "schema", "scope", "purpose"}, {"name", "scope"})
        name = table["name"]
        if not isinstance(name, str) or not re.fullmatch(re.escape(table_prefix(pid)) + r"[a-z][a-z0-9_]*", name) or len(name) > 63 or name in seen:
            fail("插件表前缀、长度或唯一性错误")
        seen.add(name)
        if table.get("schema", "public") != "public" or table["scope"] not in ("book", "global"):
            fail("插件表 schema/scope 不合法")
        if table["scope"] == "global" and table.get("purpose") not in ("configuration", "reference"):
            fail("global 表必须声明 configuration/reference 用途，不得存账套数据")
    if tables and "plugin_data:rw" not in doc["permissions"]:
        fail("插件表需要 plugin_data:rw 能力")
    files = doc["files"]
    if not isinstance(files, list) or not files or len(files) > 512:
        fail("插件 files 清单无效")
    seen = set()
    for item in files:
        _object(item, {"path", "size", "sha256"}, {"path", "size", "sha256"})
        item["path"] = safe_path(item["path"])
        path = item["path"]
        if path == "plugin.json" or path.casefold() in seen:
            fail("插件 files 清单重复或包含 plugin.json")
        seen.add(path.casefold())
        if type(item["size"]) is not int or not 0 <= item["size"] <= 20 * 1024 * 1024 or not isinstance(item["sha256"], str) or not DIGEST_RE.fullmatch(item["sha256"]):
            fail("文件 size/sha256 无效")
    files.sort(key=lambda item: item["path"].encode("utf-8"))
    paths = {item["path"] for item in files}
    if any(contributes[key] not in paths for key in ("backend_entry", "frontend_entry") if key in contributes):
        fail("入口文件未登记")
    sig = doc["signature"]
    _object(sig, {"algo", "key_id", "value"}, {"algo", "key_id", "value"})
    if sig["algo"] != "ed25519" or not isinstance(sig["key_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sig["key_id"]) or not isinstance(sig["value"], str) or len(sig["value"]) > 128:
        fail("签名描述无效")
    return doc


def canonical_payload(manifest: dict) -> bytes:
    doc = copy.deepcopy(manifest)
    doc["signature"].pop("value", None)
    try:
        return rfc8785.dumps({"format_version": doc["format_version"], "manifest": doc, "files": doc["files"]})
    except (ValueError, TypeError) as exc:
        raise PluginError("INVALID_MANIFEST", "manifest 无法规范化为 RFC 8785 JSON") from exc


def plugin_digest(manifest: dict) -> str:
    return hashlib.sha256(canonical_payload(manifest)).hexdigest()
