"""安装前的包完整性及明显危险代码检查。静态扫描不是沙箱。

扫描策略是**能力面收敛**：只允许 :mod:`app.plugins.scan_policy` 里列出的模块
import，其余一律拒绝；并按「Name 引用形态」「Subscript 调用形态」「属性访问
形态」判定危险的内建能力。黑名单是无穷枚举，已核实至少漏 18 个等价入口，
详见 scan_policy 的模块说明。

签名校验的撤销名单在**安装期和加载期都生效**：``historical`` 只放行
``plugin_historical_keys``（历史包继续可校验），``plugin_revoked_keys`` 与运行时
封禁名单一律拒绝。
"""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import io
import json
import os
import re
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.config import settings
from app.core.uploads import RowBudget, UploadTooLarge, check_archive_size
from app.plugins.errors import PluginError
from app.plugins.manifest import canonical_payload, parse_manifest, plugin_digest, safe_path
from app.plugins.scan_policy import (
    ALLOWED_DUNDER_NAMES,
    BACKEND_MODULES,
    DENIED_ATTRIBUTES,
    DENIED_NAMES_ON_CALL,
    DENIED_NAMES_ON_LOAD,
    MIGRATION_MODULES,
)

TEXT_TYPES = {".py", ".js", ".json", ".css", ".sql", ".md", ".txt"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
#: 插件前端资产的签名票据前缀（URL 路径段，见 :func:`asset_token`）。
ASSET_TICKET_SEGMENT = "_t"
#: 资产票据默认有效期（秒）。票据只覆盖「一个已安装包的静态资产」。
ASSET_TICKET_TTL = 3600


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


def _runtime_revoked_keys() -> set[str]:
    """运行时封禁名单：``plugin_root/.revoked-keys``（管理动作写，一行一个 key_id）。

    与 ``PLUGIN_REVOKED_KEYS`` 环境变量并存：环境变量要改配置并重启，这份名单
    由管理接口当场写入，**当次生效**。两条路径都在安装期和加载期判定。
    """
    try:
        root = Path(settings.plugin_root).absolute()
        path = root / ".revoked-keys"
        if path.is_symlink() or not path.is_file():
            return set()
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except (OSError, UnicodeError):
        return set()


def revoked_key_ids() -> set[str]:
    ids = {k.strip() for k in settings.plugin_revoked_keys.split(",") if k.strip()}
    return ids | _runtime_revoked_keys()


def historical_key_ids() -> set[str]:
    return {k.strip() for k in settings.plugin_historical_keys.split(",") if k.strip()}


def add_revoked_key(key_id: str) -> set[str]:
    """把 key_id 写进运行时封禁名单，返回封禁后的完整名单。"""
    from app.plugins.installer import root_path
    key_id = key_id.strip()
    if not key_id or len(key_id) > 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", key_id):
        raise PluginError("INVALID_KEY_ID", "签名密钥标识无效")
    root = root_path()
    path = root / ".revoked-keys"
    current = _runtime_revoked_keys()
    current.add(key_id)
    payload = "".join(sorted(current)) + "\n"
    temporary = root / ".revoked-keys.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)
    return current


def verify_signature(manifest: dict, keys=None, *, historical=False):
    """校验插件签名。

    ``historical=True`` 只放行 ``plugin_historical_keys``（历史包保留校验公钥），
    **不再跳过撤销判定**：撤销名单在安装期与加载期一律拒绝。
    """
    keys = trusted_keys() if keys is None else keys
    sig = manifest["signature"]
    key = keys.get(sig["key_id"])
    if key is None:
        raise PluginError("UNTRUSTED_SIGNATURE", "签名密钥不受信任")
    if sig["key_id"] in revoked_key_ids():
        raise PluginError("KEY_REVOKED", "签名密钥已撤销，插件不再被信任")
    if not historical and sig["key_id"] in historical_key_ids():
        raise PluginError("UNTRUSTED_SIGNATURE", "签名密钥不受信任")
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True))
        public.verify(base64.b64decode(sig["value"], validate=True), canonical_payload(manifest))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PluginError("INVALID_SIGNATURE", "插件签名校验失败") from exc


def _rules_for(path: str) -> dict:
    normalized = path.replace("\\", "/")
    rules = dict(BACKEND_MODULES)
    if normalized.startswith("migrations/"):
        rules.update(MIGRATION_MODULES)
    return rules


def _scan_python(path: str, tree: ast.AST, permissions) -> list[dict]:
    findings: list[dict] = []
    rules = _rules_for(path)
    granted = set(permissions or ())
    bindings: dict[str, tuple] = {}

    def finding(rule, line):
        findings.append({"file": path, "line": line, "rule": rule})

    # 第一遍：收集 import。必须先收齐再判定属性访问 —— 名字在 Python 里是
    # 作用域绑定，不是按出现顺序解析的；只按遍历顺序绑定会让「先使用、
    # 后 import」的写法绕过检查。
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rule = rules.get(alias.name)
                if rule is None:
                    # 不在白名单：不管它是不是「危险模块」，一律拒绝。
                    finding("PY_IMPORT", node.lineno)
                    continue
                if rule.symbols is not None:
                    # 受限模块必须逐个 from-import 受控符号，裸 import 会拿到整个命名空间。
                    finding("PY_IMPORT", node.lineno)
                    continue
                if rule.requires_any and not granted.intersection(rule.requires_any):
                    finding("PY_IMPORT", node.lineno)
                    continue
                bindings[alias.asname or alias.name.split(".")[0]] = (rule, None)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对导入：插件包内也没有可导入的兄弟模块。
                finding("PY_IMPORT", node.lineno)
                continue
            rule = rules.get(node.module or "")
            if rule is None:
                finding("PY_IMPORT", node.lineno)
                continue
            if rule.requires_any and not granted.intersection(rule.requires_any):
                finding("PY_IMPORT", node.lineno)
                continue
            for alias in node.names:
                name = alias.name
                if name == "*" or name in rule.denied:
                    finding("PY_IMPORT", node.lineno)
                    continue
                # **别名的原名仍然是原名**（审查 S-01）。
                # ``from io import open as fh`` 之后，代码里再也不会出现
                # ``open`` 这个名字 —— 于是「读取即拒绝」那档看不见它，
                # 而属性规则只认 ``io.open(...)`` 这种属性形态，也看不见它。
                # 扫描结果是 CLEAN，运行时却拿到了宿主文件系统。
                # 所以：**内建能力名与双下划线名，一律在 import 这一步就拒**，
                # 无论被换成什么别名、无论从哪个模块来。
                if name in DENIED_NAMES_ON_LOAD or name.startswith("__"):
                    finding("PY_IMPORT", node.lineno)
                    continue
                if rule.symbols is not None and name not in rule.symbols:
                    finding("PY_IMPORT", node.lineno)
                    continue
                bindings[alias.asname or name] = (rule, name)

    # 第二遍：危险形态。
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                finding("PY_REFLECTION", node.lineno)
                continue
            if node.attr in DENIED_ATTRIBUTES:
                finding("PY_ATTRIBUTE", node.lineno)
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id in bindings:
                rule, symbol = bindings[base.id]
                if rule.denies(symbol, node.attr):
                    finding("PY_ATTRIBUTE", node.lineno)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id in DENIED_NAMES_ON_LOAD:
                # 「取名后调用」的第一环：imp = __import__ / e = eval / f = open。
                finding("PY_CALL", node.lineno)
            elif (isinstance(node.ctx, ast.Load) and node.id.startswith("__")
                  and node.id not in ALLOWED_DUNDER_NAMES):
                # **双下划线名字按形态一律拒**（审查 S-01）。
                # 属性形态（``x.__class__``）上面那条规则已经拦了；这里补的是
                # **裸 Name** 形态 —— ``__builtins__`` / ``__loader__`` /
                # ``__spec__`` / ``__dict__`` 都是这一类，属性规则看不见它们，
                # 而它们每一个都能把内建能力重新拿回来。
                # 白名单只有 ``__name__`` / ``__file__`` / ``__doc__`` /
                # ``__package__`` 四个只读元信息。
                finding("PY_REFLECTION", node.lineno)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Subscript):
                # func["eval"](...)：下标取函数一律拒绝。
                finding("PY_SUBSCRIPT", node.lineno)
            elif isinstance(node.func, ast.Name) and node.func.id in DENIED_NAMES_ON_CALL:
                finding("PY_CALL", node.lineno)
    return findings


def scan_file(path: str, data: bytes, *, permissions=None) -> list[dict]:
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
        findings.extend(_scan_python(path, tree, permissions))
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
    permissions = manifest.get("permissions")
    findings = [finding for path, content in files.items()
                for finding in scan_file(path, content, permissions=permissions)]
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
    # historical 只放行历史密钥；**撤销的密钥在这里同样被拒绝** —— 加载期必须查撤销名单。
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
    # **加载期也重跑一次静态扫描**（审查 L-28）。
    #
    # 之前的现状：安装期扫过、加载期不扫。单看当下这不是漏洞 ——
    # 文件内容被 manifest 里的 sha256 钉死，manifest 本身被签名覆盖，
    # 所以「装时干净、载时脏」在没有签名密钥的前提下不可达。
    #
    # 但它是**纵深深度**上的缺口：扫描规则是**会变**的。哪天有人放宽了
    # 某条规则（或加进一条新的能力），已经装在磁盘上的旧包**不会被重扫** ——
    # 于是"当前规则认为危险"的代码仍然能加载并执行。
    #
    # 重扫的成本是一次 AST 遍历（插件文件本来就要读一遍算 sha256），
    # 换来的是"规则一变，存量插件立刻按新规则复检"。
    permissions = manifest.get("permissions")
    findings = [finding for path, content in files.items()
                for finding in scan_file(path, content, permissions=permissions)]
    if findings:
        raise PluginError("STATIC_SCAN_REJECTED", "插件包含不允许的代码或资源", findings=findings)
    return VerifiedPackage(manifest, expected_digest, files)


def _ticket_key() -> bytes:
    secret = settings.jwt_secret or ""
    return hashlib.sha256(("cwq.plugin.asset." + secret).encode()).digest()


def asset_token(plugin_id: str, digest: str, *, ttl: int = ASSET_TICKET_TTL) -> str:
    """给「一个已安装包的静态资产」签发短期票据。

    浏览器原生 ``import()`` 不带 Authorization 头（前端契约不允许把令牌放进
    URL），所以插件前端资源不能走 Bearer。折中办法是发一张**只覆盖该包静态
    资产**的短期票据，随 manifest 一起交给已登录用户；没有票据的匿名请求一律
    401。票据不含用户身份，泄露也只等于读到那一份已签名、可公开的静态文件。
    """
    expires = int(time.time()) + max(60, int(ttl))
    payload = f"{plugin_id}|{digest}|{expires}"
    signature = hmac.new(_ticket_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def verify_asset_token(plugin_id: str, digest: str, token: str) -> bool:
    if not token or not isinstance(token, str):
        return False
    expires_text, _, signature = token.partition(".")
    if not signature or not expires_text.isdigit():
        return False
    expires = int(expires_text)
    if expires < int(time.time()):
        return False
    payload = f"{plugin_id}|{digest}|{expires}"
    expected = hmac.new(_ticket_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
