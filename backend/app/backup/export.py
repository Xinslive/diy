"""把一个账套的数据导成一个压缩的、自带说明的快照文件。

产物格式（**先压缩，后上传** —— 压缩在这里做，因为压缩后的大小就是 OSS 上
真正占的钱，而分片上传前的落盘也要按压缩后算）：

    {format, version, scope, book_id, book_label, exported_at, tables:{表名:{columns,rows}}}

行是**行式字典**而不是列式数组：备份最怕的是「恢复时看不懂」。列式紧凑几个
百分点，但排障时面对 `[[1,"1001",...]]` 没人能确认第 5 列是金额还是余额 ——
而恢复路径本来就是「出事那天才第一次跑」的那段代码。gzip 已经把体积压下来了。

**为什么逐行流式写，而不是先攒成一个大 dict 再 dumps。** 账套备份的大小由凭证
量决定：几万张凭证的账套导出成 dict 就是几百 MB 的常驻内存，而备份这件事
本来就该在业务低峰跑、不该把 API 进程挤到 OOM。这里按批取（``yield_per``）
边取边写，内存与账套大小无关。

**读到的数据必须来自同一个时间点。** 导出要连着读十几张表，而每一张都可能
是几秒钟；按默认的 READ COMMITTED，凭证表读完之后科目表再读一次，这中间
别人录了一张凭证 —— 出来的快照是一个**从未存在过的状态**：分录在、凭证不在。
拼不出来的备份比没有更糟，因为它看起来是完整的。所以这里显式把事务提到
REPEATABLE READ（见 :func:`export_scope`）。

**制品里不带走什么。** 备份制品会离开这台机器（传到 OSS、落在运维的笔记本
上、塞进别人手里），所以「拿起来就能用」的值一律不带走：``sys_setting`` 里
那几个凭据（见 :data:`REDACTED_SETTING_KEYS`），以及**全体账号的口令散列**
（见 :data:`REDACTED_COLUMNS`，审计 P1-2）。两处都是**白名单式**的 ——
多脱一个是把业务数据也毁了，而后者更隐蔽。

**恢复出来之后，所有账号都要重置口令。** 脱敏只管住**新的**制品，而 OSS 上
躺着的历史归档里仍然带着当年的真实散列 —— 所以恢复这一步不信任归档里的
``password_hash``（见 :func:`_force_password_reset`）。代价是恢复后的系统
暂时没有人能登录：运维要先给一个管理员重置口令，管理员再逐个重置其余账号。
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import os
import re
import gzip
import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.schema_version import BACKEND_ROOT


def _snapshot_workdir() -> str:
    """解密的明文快照往哪个目录落。

    **不能是系统临时目录。** ``cwq-api.service`` 开着 ``PrivateTmp=true``，
    服务里的 ``/tmp`` 是内存盘（tmpfs）—— 明文快照的大小**就是它占的内存**，
    而且不会换出。备份与恢复都发生在业务低峰，但低峰不等于空闲：
    那正是「把这本书的记忆吃光」的最坏时机。

    优先用运维配的 ``CWQ_BACKUP_WORK_DIR``；没配就退到
    ``backend/data``（工作目录之下，一定可写，也在 unit 的
    ``ReadWritePaths=`` 白名单里）。
    """
    override = os.environ.get("CWQ_BACKUP_WORK_DIR", "").strip()
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    fallback = Path(BACKEND_ROOT) / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)
from app.models import SCOPE_BOOK, SCOPE_SYSTEM
from app.models.base import Base

FORMAT = "cwq-book-backup"

#: 程序**当前写出**的格式版本。改它之前先读下面那段。
VERSION = 2

# ---------------------------------------------------------------------------
# 版本 → reader 注册表
# ---------------------------------------------------------------------------
#
# **为什么不能写 `if header["version"] != VERSION: raise`。**
#
# 那个判断（2026-10-17 之前就是这么写的）在 VERSION 还是 1、**一份归档都还没
# 产生**的时候看着完全无害：它此刻拒绝不了任何东西。
#
# 致命之处在于月度归档是**永久保留**的（``app/models/backup.py`` 里
# ``KEEP_DAYS[TIER_MONTHLY] = 0``，``retention.py`` 对 monthly 直接 continue，
# 结构上就没有删除分支）。所以从今往后每年都会多出一批 v1 归档躺在 OSS 上，
# 而等某天 VERSION 升到 2，那句 `!= VERSION` 会让**今天存的每一份月度备份
# 同时失去可恢复性** —— 而它恰恰是出事那天唯一能救回来的东西。
#
# 也就是说：那个缺陷第一次发作的时刻，正好是它最没有机会被测试覆盖的时刻
# （那时所有测试都已经是 v2 了），而代价是永久的。能在 VERSION 还是 1、
# 还没有任何一份归档存在的时候把它改掉，是唯一便宜的时机。
#
# 所以规矩是：
#
#   * **v1 的 reader 永远不许删。** 不是「暂时留着」，是永久 —— 它是所有
#     过去与将来月度归档的唯一入口。tests 里有一条守卫专门钉这条。
#   * 升 VERSION 时**加 reader**，不是在旧判断上加一个 or。
#   * 真的认不出来的版本仍然拒绝（硬灌一份读不懂的归档比恢复失败更糟），
#     但报错必须说清「这份是哪一版、本程序能读哪几版、拿什么办法读」。
FORMAT_READERS: dict[int, "Reader"] = {}


class Reader:
    """把某一版归档读成 :func:`restore` 认识的**规范形状**。

    为什么要有这一层而不是「读出来直接用」：v1 恰好和规范形状同形（所以它的
    reader 近似恒等），但**下一版不会**。加上这层之后，``restore`` 与所有
    校验逻辑只需要认识一种形状，而每种历史版本各自负责把自己翻译过来 ——
    新增一版归档格式时不必去改恢复主体，也就不必去动那条正在救命的代码。
    """

    __slots__ = ("version", "_fn", "summary")

    def __init__(self, version: int, fn, summary: str):
        self.version = version
        self._fn = fn
        self.summary = summary

    def __call__(self, data: dict, path: "Path") -> dict:
        return self._fn(data, path)


def register_reader(version: int, summary: str):
    """把某一版的 reader 登记进来。**升 VERSION 时第一件要做的就是它。**"""

    def deco(fn):
        if version in FORMAT_READERS:
            raise ValueError(
                f"格式版本 {version} 已经有 reader 了。"
                "重复登记几乎总是意味着有人想「改掉」旧 reader —— "
                "而那正是让已归档的备份集体失效的做法。旧 reader 永远不许替换。")
        FORMAT_READERS[version] = Reader(version, fn, summary)
        return fn

    return deco


@register_reader(1, "行式字典 + gzip，首版")
def _read_v1(data: dict, path: "Path") -> dict:
    """v1 的形状 = 规范形状：``tables[name] = {columns, rows}``。

    这里只做**结构**校验。值的解码（``_decode``）与列名对不对得上留给
    :func:`restore` —— 拆开是因为前者在解压后立刻就知道，后者要查数据库。
    """
    tables = data.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise ExportRefused(f"{path.name} 里一张表都没有，拒绝恢复。")
    if data["header"].get("version") == 1 and (data["header"].get("plugins") or any(
            re.fullmatch(r"plugin_[a-f0-9]{12}_.+", name) for name in tables)):
        raise ExportRefused("v1 归档不得含插件 metadata 或插件业务表")
    for name, payload in tables.items():
        if not isinstance(payload, dict):
            raise ExportRefused(
                f"{path.name} 里表「{name}」的内容不是一个对象 —— 文件可能被改过。")
        if not isinstance(payload.get("rows", []), list):
            raise ExportRefused(
                f"{path.name} 里表「{name}」的 rows 不是数组 —— 文件可能被改过。")
    return data


@register_reader(2, "插件 metadata、scope 与存量库结构校验")
def _read_v2(data: dict, path: "Path") -> dict:
    _read_v1(data, path)
    if not isinstance(data["header"].get("plugins"), list):
        raise ExportRefused("v2 归档缺少 plugins 段")
    return data


# 程序当前能读的全部版本，按版本升序。用于「这份是哪一版 / 我能读哪几版」。
def readable_versions() -> list[int]:
    return sorted(FORMAT_READERS)


def _version_refusal(path: "Path", got: object) -> ExportRefused:
    """认不出来的版本：拒绝，但**必须可操作**。

    「不兼容」这三个字本身是有害的：它既没说这份归档是哪一版、也没说这台
    程序能读哪几版、更没说该怎么读到它。收到这句话的人只有两个选择：干等着，
    或者凭感觉猜一个版本硬改数字。后者会把归档灌坏。
    """
    known = readable_versions()
    if isinstance(got, int) and got > max(known):
        # 比程序新：这是最常见的一种（拿新机器的程序去读旧机器的备份），
        # 而且它是**可解**的 —— 找一台跑着对应旧版本的机器。
        hint = (f"这份归档比本程序新。要恢复它，请换一台跑着**能读第 {got} 版**"
                f"归档的程序来恢复。")
    elif isinstance(got, int):
        hint = (f"这份归档比本程序老，本程序只会写第 {VERSION} 版。"
                f"它自己读不了 —— 但**不要**去改归档里的 version 数字："
                f"那只是把一个读不懂的文件说成读得懂。")
    else:
        hint = "连版本号都不是一个整数，这份文件不是本系统导出的备份。"
    return ExportRefused(
        f"{path.name} 的格式版本是 {got!r}，本程序只能读{'、'.join(f'第 {v} 版' for v in known)}"
        f"（当前写出第 {VERSION} 版）。\n"
        f"  {hint}\n"
        f"  月度归档是**永久保留**的（结构上没有删除分支），所以旧版本归档会一直"
        f"存在 —— 程序侧会一直保留对应的 reader，不会因为将来升版本而失效。")

# 不进备份的表。
#
# ``sys_backup_run`` 排除，避免备份运行日志递归膨胀。
# 插件 registry 在 v2 header 表示；安装 journal、cohort、worker lease 属于本机运行状态，
# 恢复时保留现场事实，不允许用旧归档覆盖。plugin_user_access 仍按 SYSTEM 表备份。
# 它自己带 book_id，所以「所有带 book_id 的表」这条规则会把它一并捞进来 ——
# 于是这一期的备份里装着上一期的备份记录，上上期的还装着…，体积每一期都比上一期
# 大，而且没有任何东西会报错。这正是「递归的日志表」这类陷阱：它只会慢慢胀，
# 不会坏，你要在两年之后才发现 OSS 账单不对。
EXCLUDED_TABLES = frozenset({"sys_backup_run", "plugin_registry", "plugin_install_attempt", "plugin_worker_runtime", "plugin_cohort"})

#: 备份制品里**不带走**的凭据型配置键（``sys_setting.value`` 写空）。
#:
#: **为什么。** 备份制品会离开这台机器：传到 OSS、落在运维的笔记本上、
#: 塞进别人的手里。而这些值都是**拿起来就能用**的凭据 ——
#: AccessKey 能往桶里写，token 能调 OCR，SMTP 口令能发信。带着它们的
#: 备份等于把凭据抄送了一份给每一个拿到备份的人（审计 P0-1 点的就是这个面：
#: 备份制品内含「用于上传该备份的 AccessKey」）。
#:
#: **只列「值本身即可用」的键。** 调度密钥在库里存的是 SHA-256 摘要
#: （不可用），而且恢复之后要能继续跑 —— 把它清掉等于让恢复后的系统
#: 收不到任何调度触发，留下一个「恢复好了」的假象。
#:
#: **写空而不是删行。** 行还在，页面上还看得到那个键，管理员一看就知道
#: 「这个凭据没被带走、恢复后要重填」；删行的话，恢复后页面上干干净净，
#: 与"压根没配过"长得一模一样。
REDACTED_SETTING_KEYS = frozenset({
    "oss_access_key_id",
    "oss_access_key_secret",
    "ocr_api_token",
    "smtp_password",
})

#: 备份制品里**不带走**的**列**（按表精确到列）。
#:
#: **为什么需要这一张表**（审计 P1-2）：``REDACTED_SETTING_KEYS`` 只管
#: ``sys_setting`` 一张表，而 ``sys_user``（无 ``book_id`` ⇒ 判为 global ⇒
#: 进 ``SCOPE_SYSTEM`` 备份）带着完整的 ``password_hash``。于是**系统级备份
#: 制品里含全体用户（含管理员）的口令散列** —— 拿到桶读权限（或在链路上
#: 截获，见 P1-1）就等于拿到一份离线爆破素材（PBKDF2-240k 对 6 位及以下
#: 的口令只是约 20ms/次）。
#:
#: 项目自己在 ``api/system.py::_user_snapshot`` 写过同一条判据（「散列落进去
#: 等于给每个管理员送一份爆破素材」）—— 审计表用这条判据做了防护，备份表
#: 没有。**同一个判据在一个仓库里只对一张表生效**，这是漏做而不是取舍。
#:
#: **仍然是白名单式的**：只列「值本身即可用」的**列**，精确到列名。
#: 写成「``sys_user`` 整行清空」会把 ``username`` / ``is_admin`` /
#: ``real_name`` 一起毁掉 —— 那是把业务数据也毁了（而后者更隐蔽：数据少了
#: 没人知道）。所以是「表 → 列」的精确映射。
REDACTED_COLUMNS: dict[str, frozenset[str]] = {
    "sys_user": frozenset({"password_hash"}),
}

#: 每批取多少行就往 gzip 里写多少。1 千行是个平衡点：再大则一次 flush 的
#: 内存峰值高，再小则 flush 次数多、gzip 窗口利用率低。
BATCH = 1000


class _HashingWriter:
    """转发写入，同时喂给 SHA256。

    **为什么是个类而不是个闭包。** 它一开始写成 ``class _Tee`` 定义在
    ``with`` 里面、闭包引用外层的 ``fh`` —— 而那个 ``fh`` 正是后面
    ``with gzip.GzipFile(...) as fh`` 要绑定的名字，于是构造它的那一刻
    ``fh`` 还不存在，导出第一行就 ``NameError``。一个只在真正导出时才炸的
    闭包捕食错误。类不闭包任何东西。
    """

    def __init__(self, target, digest):
        self._target = target
        self._digest = digest

    def write(self, b):
        self._digest.update(b)
        return self._target.write(b)

    def flush(self):
        self._target.flush()


@dataclass
class ExportStats:
    path: Path
    rows: int = 0
    raw_bytes: int = 0          # 压缩前
    size_bytes: int = 0         # 压缩后（= OSS 上占的字节）
    checksum: str = ""
    tables: dict[str, int] = field(default_factory=dict)


class ExportRefused(Exception):
    """拒绝产出这份备份。消息必须说清「为什么不备份」与「下一步」。"""


# --------------------------------------------------------------------- 制品加密
#
#: 加密制品的魔数。恢复时靠它认出「这是密文」，而不是把密文当 gzip 去解。
ENCRYPTED_MAGIC = b"CWQENC01\n"

#: GCM 的 nonce 长度与 tag 长度（字节）。
_NONCE_BYTES = 12
_TAG_BYTES = 16

#: 流式读写块大小。加密与解密都按块走，**内存与制品大小无关**。
_CHUNK = 1024 * 1024


def encryption_key() -> Optional[bytes]:
    """客户端加密密钥（32 字节），**没配就返回 None**。

    **None 的含义是「这一份备份不加密」**，调用方必须**明确记一条告警** ——
    默默当成加密了才是真正危险的形态。

    **密钥必须与 OSS AccessKey 分开存放。** 两者放在同一个地方，等于「拿到
    备份系统的人同时拿到了解密钥匙」：AccessKey 只该给上传权限，密钥该给
    （能拿到备份的人）解密权限。两者的爆炸半径不一样，就不该放在同一处。
    """
    raw = (settings.backup_encryption_key or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ExportRefused(
            "备份加密密钥（BACKUP_ENCRYPTION_KEY）不是合法的 base64；"
            "请用 `python -c \"import base64,os;print(base64.b64encode(os.urandom(32)).decode())\"` 生成") from exc
    if len(key) != 32:
        raise ExportRefused(
            f"备份加密密钥必须是 32 字节（当前 {len(key)} 字节）；"
            "请用 `python -c \"import base64,os;print(base64.b64encode(os.urandom(32)).decode())\"` 生成")
    return key


def encrypt_file(src: Path, dst: Path) -> int:
    """把 gzip 制品加密成另一个文件（AES-256-GCM，**流式**），返回密文字节数。

    **格式**：``magic | nonce(12) | 密文 | tag(16)``。

    * 魔数让恢复侧能认出这是密文；
    * nonce 每次随机（GCM 下 nonce 重用是灾难性的，所以绝不重复）；
    * tag 在**末尾** —— 这是流式 GCM 的形状：认证标签只有整段消息结束时才算得出来。

    **为什么用流式 GCM 而不是 ``AESGCM.encrypt``。** 高层 API 一次吃整段消息，
    几百 MB 的备份就得整段进内存 —— 而备份任务就跑在 API 进程里，与在线请求
    共享内存。低层 ``Cipher(..., modes.GCM(...))`` 的 ``update()/finalize()``
    同样提供 AES-GCM 的**认证加密**，但内存是常数级。

    **SHA256 的顺序**：校验和必须算在**密文**上（记进 ``sys_backup_run.checksum``），
    因为那才是 OSS 上真正存着的字节；算在明文上的话，恢复时拿远端对象比对会
    必然对不上。明文 gzip 自己的校验和仍有价值（它证明导出本身没坏），但它
    不是 OSS 上那个对象的校验和。
    """
    key = encryption_key()
    if key is None:
        raise ExportRefused("没有配置备份加密密钥，无法加密制品")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    nonce = os.urandom(_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    written = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(ENCRYPTED_MAGIC)
        fout.write(nonce)
        written += len(ENCRYPTED_MAGIC) + len(nonce)
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            data = encryptor.update(chunk)
            if data:
                fout.write(data)
                written += len(data)
        tail = encryptor.finalize()
        if tail:
            fout.write(tail)
            written += len(tail)
        fout.write(encryptor.tag)
        written += len(encryptor.tag)
    return written


def decrypt_file(src: Path, dst: Path) -> None:
    """把加密制品解开成明文文件。**验不过就拒绝，不给「尽力而为」留路。**

    认证标签对不上时，GCM 的 ``finalize()`` 抛 ``InvalidTag`` —— 那意味着
    这份文件被改过或被截断了，**绝不能**把解出来的东西灌进库里。
    """
    key = encryption_key()
    if key is None:
        raise ExportRefused(
            "这份备份是加密的，但服务端没有配置备份加密密钥（BACKUP_ENCRYPTION_KEY）；"
            "请先配置同一把密钥再恢复")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    size = src.stat().st_size
    header = len(ENCRYPTED_MAGIC) + _NONCE_BYTES
    if size < header + _TAG_BYTES:
        raise ExportRefused(f"{src.name} 太短，不是一份完整的加密备份")
    with open(src, "rb") as fin:
        if fin.read(len(ENCRYPTED_MAGIC)) != ENCRYPTED_MAGIC:
            raise ExportRefused(f"{src.name} 不是加密备份（魔数不对）")
        nonce = fin.read(_NONCE_BYTES)
        fin.seek(size - _TAG_BYTES)
        tag = fin.read(_TAG_BYTES)
        fin.seek(header)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        remaining = size - _TAG_BYTES - header
        try:
            with open(dst, "wb") as fout:
                while remaining > 0:
                    chunk = fin.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    data = decryptor.update(chunk)
                    if data:
                        fout.write(data)
                tail = decryptor.finalize()
                if tail:
                    fout.write(tail)
        except InvalidTag as exc:
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            raise ExportRefused(
                f"{src.name} 的认证标签对不上：文件被改过、被截断，或用的不是同一把密钥；已拒绝恢复") from exc


def is_encrypted(path: Path) -> bool:
    """这份制品是不是加密的（看魔数，不看别的）。"""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC
    except OSError:
        return False


def file_sha256(path: Path) -> str:
    """文件（流式）的 SHA256。**对 OSS 上真正存着的那份字节算。**"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------ 哪些表进哪份备份


@dataclass(frozen=True)
class TableScope:
    """一张表「属于哪个账套」的方式。"""
    table: Any
    # ``book``      —— 这张表就是账套本身（``book`` 表）
    # ``direct``    —— 自己带 ``book_id`` 列
    # ``indirect``  —— 自己没有 ``book_id``，但外键全部指向已判定为账套内的表
    # ``global``    —— 全系统一份（用户、参数、登录节流）
    kind: str
    # indirect 时：指向父表的那一列在本表里的名字
    fk_local: str | None = None
    fk_target: Any = None


def build_table_scopes() -> dict[str, TableScope]:
    """算出每张表归属账套的方式。**结果依赖 metadata，故按调用时算一次并缓存。**

    为什么不能只看「有没有 book_id 列」
    ---------------------------------
    报表模板的**表体**分在四张表上，其中三张 —— ``report_row`` / ``report_col`` /
    ``report_cell`` —— **自己没有 book_id**，只有 ``template_id`` 指向
    ``report_template``。只按 book_id 筛，这三张表在任何账套的备份里都是空的：
    备份照样成功、照样压缩、照样上传，界面上照样显示「已备份 12,345 行」，
    而用户精心设计的那套报表版式**从来就不在任何一份备份里**。

    这正是「漏一张表」最坏的形态：它不报错，只是安静地少备份了一类数据。

    所以规则是**沿外键递归到闭包**：一张表只要它所有的外键都指向「已在账套内」
    的表，它就在账套内。到不动点为止。

    「所有外键」而不是「任一外键」是刻意的反向约束 —— 任一的话，一张同时指向
    ``sys_user`` 与 ``book`` 的表会被算进账套，而它的用户数据就会跟着每本账备份
    一遍（冗余）。全指向才收，是宁可漏判（漏判的那张表仍会被系统级备份的
    ``book_id IS NULL`` 分支之外的情况漏掉 —— 所以下面那条自检测试是必须的）。
    """
    tables = [t for t in Base.metadata.sorted_tables if t.name not in EXCLUDED_TABLES]
    scopes: dict[str, TableScope] = {}

    for t in tables:
        if t.name == "book":
            scopes[t.name] = TableScope(t, "book")
        elif "book_id" in t.c:
            scopes[t.name] = TableScope(t, "direct")

    # 逐轮扩张，直到没有新表进来。
    changed = True
    while changed:
        changed = False
        for t in tables:
            if t.name in scopes:
                continue
            # 只要有**单列**外键就能走；复合外键（entry 的 (book_id, period_ord)
            # 指向 period）所在的那张表本来就自带 book_id，走的是 direct 分支。
            targets = []
            for fk in t.foreign_keys:
                if len(fk.constraint.columns) != 1:
                    continue
                target = fk.column.table
                if target.name in scopes and scopes[target.name].kind in (
                        "book", "direct", "indirect"):
                    targets.append((fk.parent.name, target))
            fk_list = list(t.foreign_keys)
            if fk_list and len(targets) == len(fk_list):
                scopes[t.name] = TableScope(t, "indirect",
                                            fk_local=targets[0][0],
                                            fk_target=targets[0][1])
                changed = True

    for t in tables:
        scopes.setdefault(t.name, TableScope(t, "global"))
    return scopes


_TABLE_SCOPES: dict[str, TableScope] = {}


def table_scopes() -> dict[str, TableScope]:
    global _TABLE_SCOPES
    if not _TABLE_SCOPES:
        _TABLE_SCOPES = build_table_scopes()
    return _TABLE_SCOPES


def tables_for(scope: str) -> list[TableScope]:
    """某一份备份要导哪些表（保持 FK 拓扑序，恢复时照这个顺序插）。"""
    out = []
    for t in Base.metadata.sorted_tables:
        ts = table_scopes().get(t.name)
        if ts is None:
            continue
        if scope == SCOPE_BOOK and ts.kind == "global":
            continue        # 用户/参数/登录节流不是账套的资产
        out.append(ts)
    return out


def _predicate(ts: TableScope, scope: str, book_id: Optional[int]):
    """这一行属不属于「这份备份的范围」。返回 None 表示全表。"""
    if ts.kind == "book":
        return ts.table.c.id == book_id if scope == SCOPE_BOOK else None
    if ts.kind == "global":
        return None
    if ts.kind == "direct":
        col = ts.table.c.book_id
        return col.is_(None) if scope == SCOPE_SYSTEM else col == book_id
    # indirect：父表在本周期范围内的那些行的子集
    parent = ts.fk_target
    parent_scope = table_scopes()[parent.name]
    if parent_scope.kind == "book":
        inner = parent.c.id == book_id
    elif parent_scope.kind == "direct":
        pcol = parent.c.book_id
        inner = pcol.is_(None) if scope == SCOPE_SYSTEM else pcol == book_id
    else:
        # 祖父也要再往上推一层（目前 schema 里没用到，留着免得将来一改就静默漏）。
        grand = _predicate(TableScope(parent, parent_scope.kind,
                                      parent_scope.fk_local, parent_scope.fk_target),
                           scope, book_id)
        if grand is None:
            return None
        inner = parent.c.id.in_(select(parent.c.id).where(grand))
    pk = list(parent.primary_key.columns)
    if len(pk) != 1:
        raise ExportRefused(f"表「{parent.name}」是复合主键，账套级导出暂不支持。")
    return ts.table.c[ts.fk_local].in_(select(pk[0]).where(inner))


def rows_of(conn, ts: TableScope, scope: str,
            book_id: Optional[int]) -> Iterator[dict]:
    """按批取一行行编码后的记录。"""
    stmt = select(*ts.table.c)
    where = _predicate(ts, scope, book_id)
    if where is not None:
        stmt = stmt.where(where)
    for row in conn.execute(stmt.execution_options(yield_per=BATCH)):
        record = {col.name: encode(row._mapping[col.name]) for col in ts.table.c}
        yield _redact(ts.table.name, record)


def _redact(table_name: str, record: dict) -> dict:
    """把这一行里**不能进备份制品**的值写空。

    两张表、两种判据，但**都是白名单式的**：

    * ``sys_setting`` —— 按**键名**（``REDACTED_SETTING_KEYS``）：那几个键的
      值本身即可用（AccessKey 能往桶里写，token 能调 OCR，SMTP 口令能发信）。
    * ``sys_user`` —— 按**列名**（``REDACTED_COLUMNS``）：``password_hash`` 是
      全体账号的离线爆破素材（审计 P1-2）。

    别的表别的列不碰 —— 脱敏必须是**白名单**式的：漏掉一个键是漏一个
    凭据，多脱一个是把业务数据也毁了（而后者更隐蔽：数据少了没人知道）。
    """
    if table_name == "sys_setting":
        if record.get("key") in REDACTED_SETTING_KEYS:
            record["value"] = ""
        return record
    columns = REDACTED_COLUMNS.get(table_name)
    if columns:
        for column in columns:
            if column in record:
                record[column] = ""
    return record


# ------------------------------------------------------------ 值编码


def encode(value: Any) -> Any:
    """Python 值 → 可安全进 JSON 的形态（**无损**）。

    金额必须走文本，这是本仓库的一贯口径（``audit_helper._jsonable`` 的理由）：
    ``Numeric(20,2)`` 的上界 999999999999999999.99 有 20 位有效数字，
    float 只有 15~17 位 —— 一旦越过千万级，转成 float 就已经不是原来那个数了。
    备份里的金额变成一个「差一点点」的数字，比没有备份更难发现。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return str(value)


def _coerce(value: Any, coltype: Any) -> Any:
    """按**目标列的类型**把归档里的值变回 Python 值。

    **用目标库的类型，不把类型写进归档。** 归档里存一份「这列当年是 Numeric(20,2)」
    看着更完备，实则更脆：目标库已经迁移到 Numeric(24,4) 时，照归档的旧类型构造
    会让恢复失败在一个**与数据无关**的地方。目标库的 schema 才是恢复现场的事实。
    """
    import sqlalchemy as sa

    if value is None:
        return None
    try:
        if isinstance(coltype, sa.Numeric):
            return Decimal(str(value))
        if isinstance(coltype, sa.Boolean):
            return bool(value)
        if isinstance(coltype, sa.Integer):
            return int(value)
        if isinstance(coltype, sa.Float):
            return float(value)
        if isinstance(coltype, sa.DateTime):
            return datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(coltype, sa.Date):
            return date.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError, ArithmeticError):
        # 宁可原样交给 psycopg 去报错，也不要在这里猜一个值塞进去 ——
        # 一个被静默转成 0.00 的金额，是审计级的损失。
        return value
    # JSON / JSONB 列：值已经是 encode() 产出的可 JSON 形态（JSONB 由 psycopg
    # 自动再序列化一次），原样返回即可。
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and "__b64__" in value:
        return base64.b64decode(value["__b64__"])
    return value


def export_scope(db: Session, *, scope: str, book_id: Optional[int],
                 book_label: str, out_path: Path,
                 now: Optional[datetime] = None) -> ExportStats:
    """导出一份快照并压缩落盘。

    **整个导出在一条 REPEATABLE READ 的连接里完成** —— 见下面那段注释，
    这是本函数唯一不能省的东西。``db`` 只用来取已经 commit 的数据，
    读走的是独立连接（也正因如此，它读不到 ``db`` 里未提交的改动）。

    ``scope``/``book_id`` 决定取哪些行；``book_label`` 只是写进归档头给人看的。
    """
    if scope == SCOPE_BOOK and book_id is None:
        raise ExportRefused("账套级备份必须给出 book_id。")

    tables = tables_for(scope)
    if not tables:
        raise ExportRefused("反射里一张表都没找到，这不像一个正常的数据库；"
                            "宁可什么都不备份，也不要上传一份空归档。")

    stats = ExportStats(path=out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # **导出走自己的连接，不复用调用方那个 Session。**
    #
    # 两个原因，第二个是要命的：
    #   1. 复用就得在别人可能已经开着的事务上改隔离级别。Session 早就为别的事
    #      连上了库，此时 ``execution_options(isolation_level=...)`` 会被**静默忽略**
    #      （SQLAlchemy 只发一条 SAWarning）—— 而那正是这里唯一不能失效的东西。
    #   2. 拿到一个**真正**的 REPEATABLE READ 快照，跨十几张表读出来的是同一个
    #      时间点的数据。默认的 READ COMMITTED 下，凭证表读完再读科目表，这中间
    #      别人录了一张凭证 —— 出来的是一份**从未存在过**的状态：分录在、凭证不在。
    #      它格式正确、能解开、能上传，看起来完全正常，而恢复它会得到一个插不进
    #      外键的半吊子库。拼不出来的备份比没有备份更糟。
    #
    # 代价是导出的读看不到调用方 Session 里**未提交**的改动 —— 而备份本来就该
    # 以「已落库的数据」为准，那正是出事时要拿回来的东西。
    from app.core.db import engine

    with engine.connect() as conn:
        with conn.begin():
            # 用 SET TRANSACTION 而不是 SQLAlchemy 的
            # ``execution_options(isolation_level=...)``：后者要改 autocommit，
            # 而连接此时已经 INTRANS，psycopg 直接抛
            # 「can't change 'autocommit' now」。SET TRANSACTION 只要是事务里的
            # 第一条语句就合法，而这里它确实是。
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            from app.plugins.backup import catalog
            from app.plugins.errors import PluginError
            try:
                plugin_metadata, plugin_tables = catalog(conn)
            except PluginError as exc:
                raise ExportRefused(f"插件 metadata 核验失败：{exc.code}") from exc
            plugin_scopes = {t["name"]: t["scope"] for p in plugin_metadata for t in p["tables"]}
            tables = list(tables) + [TableScope(t, "direct" if plugin_scopes[t.name] == "book" else "global")
                for t in plugin_tables]
            with open(out_path, "wb") as raw_fh:
                digest = hashlib.sha256()
                # 校验和对的是**压缩后**写进文件的那串字节 —— 那才是 OSS 上存着的
                # 东西，将来比对远端对象才有意义。中间这层 sink 就是为了拿到它。
                sink = _HashingWriter(raw_fh, digest)
                with gzip.GzipFile(fileobj=sink, mode="wb", mtime=0) as gz:
                    # mtime=0：gzip 头默认写当前时间，那会让同一份数据两次导出得到
                    # 两个不同文件、SHA256 也就不一样 —— 于是这个校验和没法用来
                    # 判断两份备份内容是否相同。固定 0 之后它是内容寻址的。
                    header = {
                        "format": FORMAT, "version": VERSION if plugin_metadata else 1, "scope": scope,
                        "book_id": book_id, "book_label": book_label,
                        "exported_at": (now or datetime.now()).isoformat(),
                    }
                    if plugin_metadata:
                        header["plugins"] = plugin_metadata
                    gz.write(b'{"header":'
                             + json.dumps(header, ensure_ascii=False).encode("utf-8")
                             + b',"tables":{')
                    first = True
                    for ts in tables:
                        table = ts.table
                        gz.write((b"" if first else b",")
                                 + json.dumps(table.name).encode("utf-8")
                                 + b':{"columns":'
                                 + json.dumps([c.name for c in table.c]).encode("utf-8")
                                 + b',"rows":[')
                        first = False
                        n = 0
                        for i, record in enumerate(rows_of(conn, ts, scope, book_id)):
                            if i:
                                gz.write(b",")
                            gz.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                            n += 1
                        gz.write(b"]}")
                        stats.tables[table.name] = n
                        stats.rows += n
                    gz.write(b"}}")

    stats.checksum = digest.hexdigest()
    stats.size_bytes = out_path.stat().st_size
    if stats.size_bytes == 0:
        raise ExportRefused(f"导出结果是 0 字节，已丢弃：{out_path}")
    return stats


def export_book(db: Session, book, out_path: Path,
                now: Optional[datetime] = None) -> ExportStats:
    label = f"{book.company_name} / {book.name}"
    return export_scope(db, scope=SCOPE_BOOK, book_id=book.id, book_label=label,
                        out_path=out_path, now=now)


def export_system(db: Session, out_path: Path,
                  now: Optional[datetime] = None) -> ExportStats:
    return export_scope(db, scope=SCOPE_SYSTEM, book_id=None,
                        book_label="系统数据（用户、授权、参数、全局基线）",
                        out_path=out_path, now=now)


# ------------------------------------------------------------ 恢复（退路本身）


def _ordered_rows(table, rows: list[dict]) -> list[dict]:
    """把同一张表的行排成「自引用父在前」。

    **为什么需要它。** 科目表自己指向自己（``subject.parent_id`` → ``subject.id``）。
    而 PostgreSQL 的外键检查是**逐行**做的：一条多行 INSERT 里，后面的行引用的
    父行如果还没插进去，当场就报 ``subject_parent_id_fkey`` 违反。

    表级拓扑排序救不了这个 —— 父子是**同一张表**，排序器看到的是自己对自己，
    只能原地不动。于是恢复一个三级科目表会在第二级就炸掉，而错误信息里只有
    一个外键名，完全看不出「其实是行顺序错了」。

    做法是对表内自引用的行做一次拓扑排序（Kahn）。引用了**不在本批里**的行
    （比如恢复一本账到已经有别的账的库里）不算阻塞 —— 那个父行已经在库里了。
    """
    pk = list(table.primary_key.columns)
    self_fks = [(fk.parent, fk.column) for fk in table.foreign_keys
                if fk.column.table is table and len(fk.constraint.columns) == 1]
    if len(pk) != 1 or not self_fks or len(rows) < 2:
        return rows

    key = pk[0].name
    by_id: dict = {}
    for i, r in enumerate(rows):
        if r.get(key) is not None:
            by_id[r[key]] = i

    indeg = [0] * len(rows)
    dependents: list[list[int]] = [[] for _ in rows]
    for i, r in enumerate(rows):
        for local, _target in self_fks:
            v = r.get(local.name)
            if v is not None and v in by_id and by_id[v] != i:
                indeg[i] += 1
                dependents[by_id[v]].append(i)

    ready = [i for i in range(len(rows)) if indeg[i] == 0]
    order: list[int] = []
    while ready:
        i = ready.pop()
        order.append(i)
        for d in dependents[i]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    if len(order) != len(rows):
        # 有环（自引用的环不该存在，但科目树要是真有了环，恢复不该静默少插几行）。
        seen = set(order)
        order.extend(i for i in range(len(rows)) if i not in seen)
    return [rows[i] for i in order]


@contextlib.contextmanager
def _open_snapshot(path: Path):
    """打开一份快照的**明文**字节流。

    加密制品先解到临时文件（解密是流式的，内存与制品大小无关），明文制品
    直接开 gzip。两条路都只交给上层一个普通二进制流，上层不需要知道它是不是
    加密的。
    """
    if not is_encrypted(path):
        with gzip.open(path, "rb") as fh:
            yield fh
        return
    # **临时文件要落在磁盘目录，不能落在 /tmp**（2026-10-24 性能审计改）。
    #
    # ``mkstemp`` 不给 ``dir=`` 时用的是系统临时目录；而
    # ``cwq-api.service`` 开着 ``PrivateTmp=true``，服务里的 ``/tmp``
    # 是**内存盘（tmpfs）**。解密出来的明文快照（= 全量账务数据）
    # 写进去 = 直接吃内存，而且大小**等于快照大小** ——
    # 内存峰值时它就是压垮进程的那一下。
    #
    # 落到哪个目录：优先用 ``CWQ_BACKUP_WORK_DIR``（运维配好的磁盘目录），
    # 没配就退到应用自己的 ``backend/data``（工作目录之下，一定可写，
    # 也在 unit 的 ReadWritePaths 白名单里）。
    workdir = _snapshot_workdir()
    fd, temp_name = tempfile.mkstemp(prefix="cwq-restore-", suffix=".gz", dir=workdir)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        decrypt_file(path, temp_path)
        with gzip.open(temp_path, "rb") as fh:
            yield fh
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def read_snapshot(path: Path) -> dict:
    """解开一份快照，先验后跑。

    和 ``db_backup.py::assert_restorable`` 同一个立场：恢复要执行的是归档里的
    内容，最坏的情况不是恢复失败，而是**恢复「成功」了**。所以解不开、版本不认识、
    根本不是这个格式的，一律拒绝，不给「尽力而为」留路。

    **加密制品先解密。** 魔数认得出密文；解密失败（密钥不对、文件被改过）
    一律拒绝 —— 与「先验后跑」同一个立场。
    """
    with _open_snapshot(path) as fh:
        try:
            data = json.load(fh)
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
            raise ExportRefused(f"归档解不开（{path.name}）：{exc}。它可能没传完，或已被截断。") from exc
    # 格式与版本都在 **header** 里（导出时是这么写的：{"header":{...},"tables":{...}}）。
    # 写成去顶层找就是「导出成功、恢复永远拒绝」—— 而且每一侧单看都成立，
    # 只有真跑一次往返才会暴露。
    header = data.get("header") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(header, dict) \
            or header.get("format") != FORMAT:
        got = header.get("format") if isinstance(header, dict) else "?"
        raise ExportRefused(f"{path.name} 不是本系统导出的备份（format={got}）。")
    # 版本查表，**不是** ``!= VERSION``。理由见 FORMAT_READERS 上方那段：
    # 月度归档永久保留，而旧写法让升 VERSION 那天起所有历史归档同时失效。
    got = header.get("version")
    reader = FORMAT_READERS.get(got) if isinstance(got, int) else None
    if reader is None:
        raise _version_refusal(path, got)
    return reader(data, path)


def _force_password_reset(payloads: list[dict]) -> int:
    """恢复出来的账号**一律重置口令**（审计 P1-2 的另一半）。

    脱敏只保证「**新的**备份不带散列」，而 OSS 上躺着的历史归档一个也没少
    —— 脱敏是后来才加的，那些归档里仍然带着当年全体账号的真实散列。所以
    这一步**不信任归档里的 ``password_hash``**：不管它写的是空还是一串真
    散列，恢复出来的账号一律置成「无口令 + 停用」。

    为什么还要置 ``active=False``：空口令的账号本来也登不进去（口令校验必然
    失败），但它在用户列表里显示成「启用」，管理员看不出「这个账号处于待
    重置状态」，于是既不会去重置、也以为它能用。置成停用之后，账号名册上
    一眼就看得出来哪些人等着被重置口令并启用。

    **代价（刻意）：恢复后的系统暂时没有人能登录。** 这是要写下来的 ——
    恢复出来的口令散列本来就不该被继续使用（它可能已经躺在别人的硬盘上、
    在 OSS 桶里躺了好几个月）。运维需要给一个管理员重置口令（``scripts/
    init_db.py`` 或直接改库），再由管理员在「用户与授权」页上逐个重置其余
    账号的口令并启用它们。

    返回被重置的账号数，供调用方写进恢复记录。
    """
    count = 0
    for payload in payloads:
        # 归档里压根没这一列（很老的归档）时补一个空串 ——
        # 那一列是 NOT NULL，不给值整批插入会失败，而我们要的正是「空的」。
        payload["password_hash"] = ""
        if "active" in payload:
            payload["active"] = False
        count += 1
    return count


def restore(db: Session, path: Path, *, dry_run: bool = False,
            _retained_backup_validation: bool = False) -> dict[str, int]:
    """把一份快照灌回数据库。**全有或全无**。

    先验后跑、整批走同一个事务：任何一张表失败就整体回滚。恢复出一半的库比
    不恢复更难收拾 —— 它既不是出事前的版本，也不是备份里的版本，而是一个
    两者都不是、外表却完全正常的东西。

    顺序按 ``metadata.sorted_tables``（父表在前），所以不会撞外键。
    最后重置序列：带显式 id 插进去之后，序列还停在旧位置，于是恢复完成后
    **第一张新凭证就会撞主键** —— 一个只在恢复之后才发作的故障。
    """
    if _retained_backup_validation and not dry_run:
        raise ValueError("保留表备份的结构核验不能用于写入恢复")
    data = read_snapshot(path)
    tables = data["tables"]
    counts: dict[str, int] = {}

    plugin_tables = []
    if data["header"]["version"] == 2:
        from app.plugins.backup import validate_restore
        plugin_tables = validate_restore(db.connection(), data, data["header"].get("scope"),
                                         require_installed=not _retained_backup_validation)

    # 预检：列对不上就先说清楚，别插到一半才发现。
    known = {t.name: t for t in Base.metadata.tables.values()
             if t.name not in EXCLUDED_TABLES}
    known.update({t.name: t for t in plugin_tables})
    for name, payload in tables.items():
        table = known.get(name)
        if table is None:
            raise ExportRefused(
                f"归档里有表「{name}」，而当前数据库没有这张表。"
                "多半是备份来自更新的版本；先升级程序，再恢复。")
        missing = [c for c in payload.get("columns", []) if c not in table.c]
        if missing:
            # 同样要可操作。**列缺失几乎总是「程序比库新」**，处置动作是明确的：
            # 先把迁移跑了。这与「表整张都不认识」是同一个病因、两种表现，
            # 所以两处必须给同一句话 —— 否则用户会以为是归档坏了而去重传，
            # 而重传的归档有同样的问题。
            raise ExportRefused(
                f"表「{name}」在归档里有、而当前数据库没有的列：{'、'.join(missing)}。\n"
                f"  这几乎总是**程序比数据库新**：归档是旧程序写的，那些列是之后才加的。\n"
                f"  执行：cd {BACKEND_ROOT} && .venv/bin/alembic upgrade head\n"
                f"  迁移跑完再恢复。**不要**去删归档里的列 —— 那些列里是账。")

    if dry_run:
        for name, payload in tables.items():
            counts[name] = len(payload.get("rows", []))
        return counts

    for table in list(Base.metadata.sorted_tables) + plugin_tables:
        payload = tables.get(table.name)
        if payload is None:
            continue
        rows = payload.get("rows", [])
        if not rows:
            counts[table.name] = 0
            continue
        cols = [c.name for c in table.c]
        payloads = []
        for record in rows:
            values = {k: _coerce(_decode(v), table.c[k].type)
                      for k, v in record.items() if k in table.c}
            # 归档里有、当前库里也有的列才插；缺的那些列留给默认值。
            payloads.append({c: values[c] for c in cols if c in values})
        if table.name == "sys_user":
            # 恢复出来的账号一律重置口令 —— 见 _force_password_reset 的说明。
            reset = _force_password_reset(payloads)
            if reset:
                logging.getLogger("cwq.backup.export").warning(
                    "恢复的 %d 个账号已置为「无密码 + 停用」：备份制品不带密码散列，"
                    "恢复后必须逐个重置密码并启用（先给一个管理员重置密码，"
                    "否则没有人能登录）。", reset)
        db.execute(table.insert(), _ordered_rows(table, payloads))
        counts[table.name] = len(payloads)
        _reset_sequence(db, table)

    return counts


def _reset_sequence(db: Session, table) -> None:
    """把自增序列推到已插入的最大 id 之后。

    **不做这一步，恢复出来的库是废的。** 带显式 id 插进去之后，序列仍停在
    出事前那个值，于是恢复后录入的**第一张凭证**就撞主键 —— 一个当场看不见、
    要等到有人真的开始记账才炸的故障。

    表名/列名一律通过**绑定参数**传，不拼进 SQL 字符串 —— 拼的话，一个带引号的
    表名就能改写这条语句，而这里处理的表名来自 metadata、引号来自将来可能更
    换的方言，不是可以随便假设的 ASCII。

    没有序列的表直接跳过：``pg_get_serial_sequence`` 返回 NULL。
    """
    pk = list(table.primary_key.columns)
    if len(pk) != 1:
        return
    ident = pk[0]
    seq = db.scalar(text("SELECT pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": ident.name})
    if not seq:
        return
    biggest = db.scalar(select(func.coalesce(func.max(ident), 0)))
    db.execute(text("SELECT setval(:seq, :val, false)"),
               {"seq": seq, "val": int(biggest) + 1})
