"""备份制品里不能有凭据（审计 P0-1）。

**为什么这条单独成文件。** 审计 P0-1 的原话是「备份制品内含『用于上传
该备份的 OSS AccessKey』明文」。它刺中的是一个**结构性的**问题，不是笔误：

    备份制品会离开这台机器 —— 传到 OSS、落在运维的笔记本上、塞进别人手里。

而 ``sys_setting`` 是**整表导出**的（``Base.metadata.sorted_tables`` 减去
排除表），于是那张表里每一个「拿起来就能用」的值，都跟着每一份备份走了
一遍。AccessKey 能往桶里写，OCR token 能调 OCR，SMTP 口令能发信 ——
带着它们的备份，等于把凭据抄送给了每一个拿到备份的人。

**本文件盯三件事。**

1. 导出制品里**没有**凭据明文（这是功能本身）。
2. 脱敏是**写空而不是删行** —— 行还在，管理员一眼看得出"这个凭据没被
   带走、恢复后要重填"；删行的话恢复后页面干干净净，与"压根没配过"长得
   一模一样。
3. **将来新加的凭据型键必须进名单** —— 这条是钉子。没有它，加一个新凭据
   （比如某天接了个新 OCR 服务）就会**静默地**开始跟着备份走，而所有测试
   照样绿。
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.backup import export as export_mod
from app.core import system_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

#: 值是**摘要**、不是可直接使用的凭据的键。
#:
#: 调度密钥在库里存的是 SHA-256 摘要：拿到它算不出原密钥，所以它不是凭据。
#: 而且它**必须**跟着备份走 —— 清掉的话，恢复之后外部调度服务带来的
#: 触发密钥全部失效，tick 直接 401，所有定时任务静默停摆。
DIGEST_KEYS = frozenset({"scheduler_trigger_secret", "scheduler_trigger_secret_previous"})

#: 键名里长这样，多半是凭据。
CREDENTIAL_LIKE = re.compile(r"(password|secret|token|access_key|api_key)", re.IGNORECASE)


@pytest.fixture
def 配好凭据(db):
    """把四种凭据都配上去（**并提交**）。

    提交是必须的：导出走自己的 REPEATABLE READ 连接，读的是**已提交**的
    数据 —— 不提交的话导出的就是一份"配置还没生效"的库，而测试照样绿。
    """
    值 = {
        "oss_access_key_id": "AKIA-REAL-ACCESS-ID",
        "oss_access_key_secret": "REAL-OSS-SECRET-0001",
        "ocr_api_token": "REAL-OCR-TOKEN-0002",
        "smtp_password": "REAL-SMTP-PASSWORD-0003",
    }
    for key, value in 值.items():
        system_settings.set_value(db, key, value, updated_by="test")
    system_settings.set_value(db, "backup_enabled", "1", updated_by="test")
    db.commit()
    return 值


def _系统级归档(db, tmp_path) -> tuple[Path, bytes]:
    out = tmp_path / "system.json.gz"
    export_mod.export_system(db, out)
    return out, out.read_bytes()


def test_导出制品里没有凭据明文(db, 配好凭据, tmp_path):
    """**整个文件**里都不许出现凭据明文。

    断言的对象是**字节**而不是解析后的结构：压缩过的字节里，明文可能因为
    恰好跨了 gzip 块边界而"看起来不在"，也可能因为编码而变形。真正要证明
    的是「任何人拿到这个文件都读不出凭据」，那就得在整个文件上找。
    """
    _out, raw = _系统级归档(db, tmp_path)

    for key, secret in 配好凭据.items():
        assert secret.encode("utf-8") not in raw, f"{key} 的明文出现在备份制品里"
        # 压缩后的字节里可能对不上原文，所以再查一次解压后的内容
        明文 = gzip.decompress(raw)
        assert secret.encode("utf-8") not in 明文, f"{key} 的明文出现在解压后的备份里"


def test_脱敏是写空而不是删行(db, 配好凭据, tmp_path):
    """行要还在，值要空。

    **删行是错的**：恢复之后页面上那个配置项干干净净，与"压根没配过"
    一模一样 —— 管理员看不出"这个凭据曾经配过、只是没被带走"，于是不会
    去重填。写空则一眼可见。
    """
    out, _raw = _系统级归档(db, tmp_path)
    data = json.loads(gzip.decompress(out.read_bytes()))

    行 = {r["key"]: r for r in data["tables"]["sys_setting"]["rows"]}
    for key in 配好凭据:
        assert key in 行, f"{key} 这一行整个没了 —— 应该是写空，不是删行"
        assert 行[key]["value"] == "", f"{key} 的值没有清空"


def test_普通配置照常导出(db, 配好凭据, tmp_path):
    """脱敏必须是**白名单**式的：只清那几把钥匙。

    脱敏过头比脱敏漏了更隐蔽 —— 漏掉一个键是多一份凭据跟着走（有人会发现），
    多脱一个是把业务数据也毁了（没人会发现，直到某天它不见了）。
    """
    out, _raw = _系统级归档(db, tmp_path)
    data = json.loads(gzip.decompress(out.read_bytes()))

    行 = {r["key"]: r for r in data["tables"]["sys_setting"]["rows"]}
    assert 行["backup_enabled"]["value"] == "1", "普通开关被一起清空了"


def test_调度密钥不跟着一起清掉(db, 配好凭据, tmp_path):
    """调度密钥存的是**摘要**，不可直接使用，而且必须能跟着恢复走。

    清掉它的后果不是"少一个配置"：恢复之后外部调度服务带来的触发密钥
    全部失效，tick 直接 401，**所有定时任务静默停摆** —— 而恢复脚本刚
    打印过"恢复完成"。
    """
    system_settings.set_value(db, "scheduler_trigger_secret", "a" * 64,
                              updated_by="test")
    db.commit()

    out, _raw = _系统级归档(db, tmp_path)
    data = json.loads(gzip.decompress(out.read_bytes()))

    行 = {r["key"]: r for r in data["tables"]["sys_setting"]["rows"]}
    assert 行["scheduler_trigger_secret"]["value"] == "a" * 64
    assert "scheduler_trigger_secret" not in export_mod.REDACTED_SETTING_KEYS


def test_新加的凭据型键必须进脱敏名单():
    """**钉子**：将来往 ``sys_setting`` 里加新凭据时，这条会红。

    扫描 ``app/`` 下所有 ``set_value(db, "字面量键", ...)`` 的调用，把名字
    长得像凭据的键（password / secret / token / access_key / api_key）与
    脱敏名单对照。

    **为什么不能靠人记得**：加一个新凭据（比如接了个新的 OCR 服务）的人，
    不会想到"这个键会被整表导出进备份"。而没有这条测试时，那把钥匙会
    **静默地**开始跟着每一份备份走 —— 所有既有测试照样绿。

    **这条扫描的局限**（写出来免得后来的人以为它能覆盖一切）：它扫的是
    ``set_value(db, "字面量键", ...)`` 这种**字面量**写法。将来若有人把键
    提到模块常量里再传进去，这条扫不到 —— 那时要把常量名补进
    :data:`DIGEST_KEYS` 或 :data:`export_mod.REDACTED_SETTING_KEYS` 旁边，
    并在这里加一行断言。
    """
    漏 = []
    for path in APP_ROOT.rglob("*.py"):
        源码 = path.read_text(encoding="utf-8")
        for match in re.finditer(r"set_value\(\s*\w+\s*,\s*[\"']([a-z0-9_]+)[\"']", 源码):
            key = match.group(1)
            if key in DIGEST_KEYS or key in export_mod.REDACTED_SETTING_KEYS:
                continue
            if CREDENTIAL_LIKE.search(key):
                漏.append(f"{path.relative_to(BACKEND_ROOT)}: {key}")

    assert not 漏, (
        "这些键长得像凭据，却不在 export_mod.REDACTED_SETTING_KEYS 里 ——\n"
        "它们会被整表导出进备份制品：\n  " + "\n  ".join(漏))


def test_恢复脚本会列出待重填的凭据(db, 配好凭据):
    """恢复完必须**说清楚**哪几项要重填。

    不说的话，运维看到的是"恢复完成"，下一次备份却因为没有 AccessKey
    而失败 —— 而那次失败发生在没人看的凌晨。
    """
    spec = importlib.util.spec_from_file_location(
        "cwq_backup_to_oss_redaction", BACKEND_ROOT / "scripts" / "backup_to_oss.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 凭据都在 → 没有要重填的
    assert mod._待重填的凭据(db) == []

    # **恢复之后**的真实状态：归档里凭据是空的（有意为之）
    for key in 配好凭据:
        system_settings.set_value(db, key, "", updated_by="test")
    db.commit()

    待填 = mod._待重填的凭据(db)
    assert sorted(待填) == sorted(配好凭据), f"待重填清单不对：{待填}"

    # 只列真的没配的，不是"所有凭据"的清单
    system_settings.set_value(db, "ocr_api_token", "配回去了", updated_by="test")
    db.commit()
    待填 = mod._待重填的凭据(db)
    assert "ocr_api_token" not in 待填
    assert "smtp_password" in 待填


# ------------------------------------------------- 用户口令散列（P1-2 的另一半）
#
# 审计 P1-2：``REDACTED_SETTING_KEYS`` 只管 ``sys_setting`` 一张表，而
# ``sys_user``（无 ``book_id`` ⇒ 判为 global ⇒ 进 ``SCOPE_SYSTEM`` 备份）
# 带着完整的 ``password_hash``。于是**系统级备份制品里含全体用户（含管理员）
# 的口令散列** —— 拿到桶读权限（或在链路上截获，见 P1-1）就等于拿到一份
# 离线爆破素材。
#
# 项目自己在 ``api/system.py::_user_snapshot`` 写过同一条判据（「散列落进去
# 等于给每个管理员送一份爆破素材」）—— 审计表用这条判据做了防护，备份表
# 没有。**同一个判据在一个仓库里只对一张表生效**，这是漏做而不是取舍。


def test_系统级备份里没有用户口令散列(db, admin_user, tmp_path):
    """制品里的 ``password_hash`` 必须是空的。"""
    out, _raw = _系统级归档(db, tmp_path)
    data = json.loads(gzip.decompress(out.read_bytes()))

    rows = data["tables"]["sys_user"]["rows"]
    assert rows, "库里明明有用户，导出里却一个都没有 —— 这条用例测了个寂寞"
    for row in rows:
        assert row["password_hash"] == "", (
            f"用户 {row['username']} 的口令散列还在备份制品里")


def test_口令散列的字节不在制品里(db, admin_user, tmp_path):
    """**整个文件**里都不许出现散列本身（连压缩前的字节也算）。

    断言对象是字节而不是解析后的结构：散列可能因为跨 gzip 块边界而
    「看起来不在」。
    """
    from sqlalchemy import select

    from app.models import SysUser

    散列 = db.scalar(select(SysUser.password_hash).limit(1))
    assert 散列, "夹具里没有用户，测不出东西"

    _out, raw = _系统级归档(db, tmp_path)
    assert 散列.encode("utf-8") not in raw, "口令散列出现在备份制品的字节里"
    明文 = gzip.decompress(raw)
    assert 散列.encode("utf-8") not in 明文, "口令散列出现在解压后的备份里"


def test_脱敏不能把业务列一起毁掉(db, admin_user, tmp_path):
    """**白名单式**：精确到**列**，不是整行清空。

    多脱一个是把业务数据也毁了 —— 而后者更隐蔽（数据少了没人知道，
    直到某天它不见了）。所以 ``username`` / ``is_admin`` / ``real_name``
    这些列**必须还在**。
    """
    out, _raw = _系统级归档(db, tmp_path)
    data = json.loads(gzip.decompress(out.read_bytes()))

    行 = {r["username"]: r for r in data["tables"]["sys_user"]["rows"]}
    assert admin_user.username in 行, "用户行整个没了 —— 应该是写空，不是删行"
    这一行 = 行[admin_user.username]
    assert 这一行["password_hash"] == ""
    assert 这一行["is_admin"] is True
    assert 这一行["real_name"] == admin_user.real_name
    assert 这一行["active"] is True
    # 列本身也要在（写空，不是把列删掉）
    assert "password_hash" in 这一行


def test_脱敏名单是白名单而不是黑名单():
    """``sys_user`` 的脱敏必须精确到 ``password_hash`` 这一列。"""
    assert export_mod.REDACTED_COLUMNS["sys_user"] == frozenset({"password_hash"})
    assert "username" not in export_mod.REDACTED_COLUMNS["sys_user"]
    assert "is_admin" not in export_mod.REDACTED_COLUMNS["sys_user"]


def test_恢复出来的账号必须重置口令(db, admin_user, tmp_path):
    """恢复之后**所有账号都要重置口令** —— 这一步在恢复路径上落实。

    脱敏只管住**新的**制品，而 OSS 上躺着的历史归档里仍然带着当年的真实
    散列 —— 所以恢复这一步**不信任归档里的 password_hash**：不管它写的是空
    还是一串真散列，恢复出来的账号一律是「无口令 + 停用」。

    这里用的是一份**只含 sys_user 的最小归档**：系统级归档里还有
    ``sys_setting`` / ``audit_log`` 等表，把它们一起灌回同一个库会撞主键
    （那些表里的行还在），而这条用例要验的是**账号**那一条。
    """
    from app.models import SysUser

    数据 = {
        "header": {"format": export_mod.FORMAT, "version": 1, "scope": "SYSTEM",
                   "book_id": None, "book_label": "测试"},
        "tables": {"sys_user": {
            "columns": ["id", "username", "password_hash", "real_name", "active", "is_admin"],
            # 归档里的行是**行式字典**（见 export.py 的模块注释）
            "rows": [{"id": 9001, "username": "恢复出来的用户",
                      "password_hash": "pbkdf2_sha256$240000$c2FsdA==$aGFzaA==",
                      "real_name": "恢复出来的用户", "active": True, "is_admin": True}],
        }},
    }
    path = tmp_path / "min.json.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(json.dumps(数据, ensure_ascii=False).encode("utf-8"))

    export_mod.restore(db, path)
    db.commit()

    行 = db.scalar(select(SysUser).where(SysUser.username == "恢复出来的用户"))
    assert 行 is not None, "恢复出来的库里没有这个账号"
    assert 行.password_hash == "", "恢复出来的账号还带着口令散列"
    assert 行.active is False, "恢复出来的账号没有标成待重置口令"


def test_恢复时连旧归档里的散列也不信(db, tmp_path):
    """**旧归档里带着真散列**也不恢复 —— 恢复这一步是无条件不信归档的。

    这一条是「脱敏只管住新制品」那个洞的补丁：OSS 上躺着的历史归档一个
    也没少，它们里面的散列仍然是真的可用素材。
    """
    from app.models import SysUser

    数据 = {
        "header": {"format": export_mod.FORMAT, "version": 1, "scope": "SYSTEM",
                   "book_id": None, "book_label": "测试"},
        "tables": {"sys_user": {
            "columns": ["id", "username", "password_hash", "real_name", "active", "is_admin"],
            "rows": [{"id": 9002, "username": "旧归档里的用户",
                      "password_hash": "pbkdf2_sha256$240000$c2FsdA==$aGFzaA==",
                      "real_name": "旧归档里的用户", "active": True, "is_admin": True}],
        }},
    }
    path = tmp_path / "old.json.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(json.dumps(数据, ensure_ascii=False).encode("utf-8"))

    export_mod.restore(db, path)
    db.commit()

    行 = db.scalar(select(SysUser).where(SysUser.username == "旧归档里的用户"))
    assert 行 is not None
    assert "pbkdf2" not in (行.password_hash or ""), "旧归档里的散列被原样恢复了"
