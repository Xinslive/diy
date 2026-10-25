"""审计修复：备份加密密钥的**运行期**闸（§3.8 范围补全）。

**背景：§3.8 的修复只盖住了 env 那一层。**

审计 §3.8 要求「配了 OSS 备份就必须配备份加密密钥」。修好的那道闸在
``app/core/config.py::validate_production_settings`` —— 它**只看 env 层**的
四个 OSS 参数，因为启动期没有库连接，看不到界面上填的那一套。

而备份管线真正用的是**生效配置**（``app/backup/config.py::oss_config``：
库里优先、env 兜底）。于是有一条完全走得通的路：

    运维在「系统管理 → 数据安全」页面上填好 OSS
      → 启动闸门不响（env 层没配，启动期也看不见库）
      → 每晚的备份以**明文 gzip** 上传到对象存储。

这条路的特别之处在于：它是**产品自己指给运维的那条路** ——
``require_ready`` 的报错原文就是「请到「系统管理 → 数据安全」填写后重试」。
所以它不是边角情况，是主路。

**修法**：同一道闸搬到**运行期**、按**生效配置**判 ——
``app/backup/config.py::require_encrypted_ready``。一道实现同时覆盖 env 与
界面两条来源。启动闸门**保留**（它挡的是「压根不该带着这个配置启动」，
而且能拦住连不上库就启动的场景），两道闸覆盖两种来源。

**判据只用生产者本人**：``app/backup/export.py::encryption_key``。
它返回 None = 没配（不加密），它抛 ``ExportRefused`` = 配了但不合格。
不另写一份「算不算配了」的判据 —— 启动闸门说「合格」而导出说「拒绝」，
那这道闸就是骗人的（而且是最坏的那种骗法：让人以为备份是加密的）。

下面每一条用例都有对应的**反向验证**（拆掉闸 → 用例必须变红），
验证记录写在 ``docs/audit-remediation-2026-10-25.md`` §5.8。
"""
from __future__ import annotations

import base64
import os

import pytest

from app.backup import config as backup_config
from app.backup import export as export_mod
from app.backup import oss_client as oss_mod
from app.backup import service as service_mod
from app.core import system_settings
from app.core.config import settings


# 一把合法的 base64 32 字节密钥。
GOOD_KEY = base64.b64encode(b"\x11" * 32).decode()

# 生效配置里的四个必填项（少了任何一个，备份管线根本走不到上传那一步）。
OSS_VALUES = {
    "oss_endpoint": "oss-cn-hangzhou.aliyuncs.com",
    "oss_access_key_id": "AK",
    "oss_access_key_secret": "SK",
    "oss_bucket": "acct-backup",
    "oss_prefix": "acct-backup",
}


def _clear_env_oss(monkeypatch) -> None:
    """把 **env 层**的 OSS 参数清空。"""
    for name in ("oss_endpoint", "oss_access_key_id", "oss_access_key_secret",
                 "oss_bucket", "oss_prefix"):
        monkeypatch.setattr(settings, name, "")


def _set_db_oss(db, **overrides) -> None:
    """把 OSS 参数写进**库里**（界面上填的那一套）。"""
    values = dict(OSS_VALUES)
    values.update(overrides)
    for key, value in values.items():
        system_settings.set_value(db, key, value)
    db.commit()


@pytest.fixture
def no_env_oss(monkeypatch):
    """env 层没有 OSS 配置 —— 「界面上配的那一套」的前置条件。"""
    _clear_env_oss(monkeypatch)
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    monkeypatch.setattr(settings, "backup_encryption_key_warn_only", False)


@pytest.fixture(autouse=True)
def _逃生口复位(monkeypatch):
    """**每个用例都把逃生口复位** —— 这份文件必须是 hermetic 的。

    ``BACKUP_ENCRYPTION_KEY_WARN_ONLY`` **只能来自进程环境**（写进 ``.env``
    会被启动闸门拒），所以「开发机的 shell profile 里带着它」是完全可能的
    场景。而它开着的时候，「缺密钥 ⇒ 拒绝」那几条会**全部失去牙齿** ——
    用例会给出与实现无关的红，**掩盖真问题**。

    修法是 autouse 复位：**每个用例只依赖它自己声明的东西**，
    不依赖跑它的那台机器的环境。（autouse 夹具先跑，用例体里显式
    ``setattr(True)`` 仍然压得住它 —— 逃生口那几条正是这么写的。）
    """
    monkeypatch.setattr(settings, "backup_encryption_key_warn_only", False)


# ------------------------------------------------------- 根因：两道闸看到的是两套配置


def test_启动闸门看不到界面上配的OSS_这正是那个洞(no_env_oss, db):
    """**先把洞钉住**：界面上配了 OSS 时，启动闸门是不响的。

    这条用例的作用是**防止有人用「删掉启动闸门」来"修"这个问题**。
    启动闸门（``validate_production_settings``）挡的是「带着一个必然
    明文上传的配置启动」，它在连不上库的部署场景里是有用的。

    它看不见界面上填的那一套，是启动期的**结构限制**（启动时没有库连接），
    不是疏忽 —— 所以补的那道闸必须放在**运行期**、按**生效配置**判。
    """
    from app.core.config import _oss_backup_configured

    _set_db_oss(db)

    # 生效配置：配上了。
    assert backup_config.oss_config(db)["bucket"] == "acct-backup"

    # 启动闸门看到的：没配（env 层是空的）→ 不响。
    assert _oss_backup_configured(settings) is False


def test_不管OSS配在哪一层_受加密闸约束(no_env_oss, db, monkeypatch):
    """**两道闸之间不能有缝。**

    OSS 参数有三个来源：env、界面（库里）、两者都配。每一个组合下，
    只要**生效配置**凑齐了四项，运行期就**必须**受加密闸约束 ——
    不存在「某一种配法能绕过去」。

    这条用例枚举三种配法，每一种都要求同一个结论：
    缺密钥 ⇒ 一律拒绝。少一个组合就多一条没被覆盖的路。
    """
    for 来源 in ("env", "界面", "两者"):
        _clear_env_oss(monkeypatch)
        monkeypatch.setattr(settings, "backup_encryption_key", "")

        if 来源 in ("env", "两者"):
            for name, value in OSS_VALUES.items():
                monkeypatch.setattr(settings, name, value)
        if 来源 in ("界面", "两者"):
            _set_db_oss(db)

        assert backup_config.oss_config(db)["bucket"] == "acct-backup", 来源
        with pytest.raises(backup_config.EncryptionKeyMissing):
            backup_config.require_encrypted_ready(db)


def test_四项不齐时压根走不到上传_加密闸不必管(no_env_oss, db, monkeypatch):
    """四项（Endpoint / AK / SK / Bucket）不齐 ⇒ 备份管线走不到上传。

    这时候**不该**拿加密闸去卡：缺 Bucket 时压根不会去建桶，
    卡它只会把「配置不全」这条消息糊成「密钥没配」，指错了方向。

    判据与启动闸门**完全一致**（都是那四项）—— 一边按四项、一边按三项，
    就会出现「一边说配了、一边说没配」的窗口期。
    """
    from app.core.config import _oss_backup_configured

    for 缺 in ("oss_endpoint", "oss_access_key_id", "oss_access_key_secret",
               "oss_bucket"):
        _clear_env_oss(monkeypatch)
        monkeypatch.setattr(settings, "backup_encryption_key", "")
        # env 层配齐（启动闸门看得到的那个来源），库里**也**缺一项。
        # 注意：库里留空会**退回 env**（生效配置是「库里优先、env 兜底」），
        # 所以 env 那一项也得清 —— 否则「缺」根本没缺上。
        for name, value in OSS_VALUES.items():
            monkeypatch.setattr(settings, name, value)
        monkeypatch.setattr(settings, 缺, "")
        _set_db_oss(db, **{缺: ""})

        assert backup_config.oss_config(db)[backup_config.OSS_SPEC[缺][0]] == "", 缺
        with pytest.raises(backup_config.ConfigIncomplete) as exc:
            backup_config.require_encrypted_ready(db)
        # 说的是「缺哪一项」，不是「密钥没配」—— 指对方向。
        assert "BACKUP_ENCRYPTION_KEY" not in str(exc.value), (缺, str(exc.value))

    # 启动闸门判的是同样那四项。
    for name, value in OSS_VALUES.items():
        monkeypatch.setattr(settings, name, value)
    assert _oss_backup_configured(settings) is True


# ----------------------------------------------------------------- 运行期那道闸


def test_界面上配了OSS但没配密钥时_运行期闸拦住(no_env_oss, db):
    """**复核方点名的那个洞**：界面上配 OSS + 没配密钥 ⇒ 拒绝。

    拒绝发生在**上传之前** —— 不是「先传上去再发现没加密」。
    """
    _set_db_oss(db)

    with pytest.raises(backup_config.EncryptionKeyMissing) as exc:
        backup_config.require_encrypted_ready(db)

    msg = str(exc.value)
    assert "BACKUP_ENCRYPTION_KEY" in msg
    assert "明文" in msg


def test_界面上配了OSS但没配密钥时_一个字节都没上传(no_env_oss, db, book, monkeypatch):
    """闸门在**上传之前**就拦住 —— 一个对象都不许传。

    「传完了才发现没加密」和「压根没传」是两件完全不同的事：
    前者已经在对象存储上留了一份明文制品，撤不干净。
    """
    建桶: list[dict] = []
    传了: list[str] = []

    monkeypatch.setattr(oss_mod, "make_bucket",
                        lambda cfg, **kw: 建桶.append(cfg) or object())
    monkeypatch.setattr(oss_mod, "put_file",
                        lambda b, k, p: 传了.append(k) or "etag")
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    _set_db_oss(db)

    with pytest.raises(backup_config.EncryptionKeyMissing):
        service_mod.run_backup(db, actor=None, book_ids=[book.id])

    assert 传了 == [], "缺密钥时竟然传出去了东西"
    # 连桶都没建 —— 拒绝发生在**任何**副作用之前。
    assert 建桶 == [], "缺密钥时竟然建了桶"


def test_env层配了OSS但没配密钥时_同样被拦(monkeypatch, db):
    """env 那条路也不能漏 —— 它是 §3.8 原本要修的那条。"""
    for name, value in OSS_VALUES.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "backup_encryption_key", "")

    with pytest.raises(backup_config.EncryptionKeyMissing):
        backup_config.require_encrypted_ready(db)


def test_配了合法密钥就放行(monkeypatch, db):
    """配了合法密钥 ⇒ 放行，并给出**生效配置**。"""
    _clear_env_oss(monkeypatch)
    monkeypatch.setattr(settings, "backup_encryption_key", GOOD_KEY)
    _set_db_oss(db)

    cfg = backup_config.require_encrypted_ready(db)

    assert cfg["bucket"] == "acct-backup"
    assert cfg["prefix"] == "acct-backup"


def test_逃生口打开时放行并记一条告警(monkeypatch, db, caplog):
    """``BACKUP_ENCRYPTION_KEY_WARN_ONLY=true`` ⇒ 放行，但**必须留一条告警**。

    逃生口的存在是为了让运维能**先跑起来**，不是为了让明文上传变成默认行为。
    所以：放行 + 一条说得清的告警（说清「制品是明文」）。
    """
    _clear_env_oss(monkeypatch)
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    monkeypatch.setattr(settings, "backup_encryption_key_warn_only", True)
    _set_db_oss(db)

    with caplog.at_level("WARNING"):
        cfg = backup_config.require_encrypted_ready(db)

    assert cfg["bucket"] == "acct-backup"
    告警 = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "BACKUP_ENCRYPTION_KEY" in 告警 or "明文" in 告警, 告警


def test_逃生口打开时上传的是明文gzip(monkeypatch, db, book):
    """逃生口下**真的**是明文 —— 这是唯一一条能传出明文的路。

    把这条钉住，是为了让「明文上传」这件事有一个**明确的、可数出来的**入口，
    而不是「配漏了就会发生」的意外。
    """
    _clear_env_oss(monkeypatch)
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    monkeypatch.setattr(settings, "backup_encryption_key_warn_only", True)

    传了: dict[str, bytes] = {}
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file",
                        lambda b, k, p: 传了.__setitem__(k, open(p, "rb").read()) or "etag")
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    _set_db_oss(db)
    service_mod.run_backup(db, actor=None, book_ids=[book.id])

    assert 传了, "逃生口下也该传出东西"
    assert all(v.startswith(b"\x1f\x8b") for v in 传了.values()), \
        "逃生口下应当是**明文 gzip**（这正是它被叫作逃生口的原因）"


def test_配了密钥时传上去的必须是密文(no_env_oss, db, book, monkeypatch):
    """配了密钥 ⇒ 上传的**必须**是密文。

    这一条是「配了密钥」方向的**对称断言**：逃生口那条钉的是「明文」，
    这条钉的是「密文」。

    **只钉一头是不够的。** 把 ``_prepare_upload`` 里的
    ``encryption_key()`` 改成永远返回 None（于是永远走明文分支），
    只钉「逃生口 ⇒ 明文」的那个文件会**一路绿灯** ——
    因为「配了密钥却传出明文」这件事**根本没被断言过**。
    两头都钉住，那次变异就会让这个文件变红。
    """
    from pathlib import Path

    _clear_env_oss(monkeypatch)
    monkeypatch.setattr(settings, "backup_encryption_key", GOOD_KEY)
    monkeypatch.setattr(settings, "backup_encryption_key_warn_only", False)

    传了: dict[str, bytes] = {}
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file",
                        lambda b, k, p: 传了.__setitem__(k, Path(p).read_bytes()) or "etag")
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    _set_db_oss(db)
    service_mod.run_backup(db, actor=None, book_ids=[book.id])

    assert 传了, "没传出东西"
    for key, data in 传了.items():
        assert data.startswith(export_mod.ENCRYPTED_MAGIC), (key, data[:16])
        assert not data.startswith(b"\x1f\x8b"), key


def test_密钥不合格时抛的是生产者自己的拒绝(monkeypatch, db):
    """配了但不合格 ⇒ 抛 ``ExportRefused``，**不是** ``EncryptionKeyMissing``。

    两种拒绝说的是两件事：「没配」与「配错了」。运维看到哪个，
    下一步动作就不同（配一把 vs 改一把）。

    判据必须用**生产者本人**（``export.encryption_key``）：启动闸门说
    「合格」而导出说「拒绝」，那道闸就是骗人的。
    """
    _clear_env_oss(monkeypatch)
    _set_db_oss(db)

    monkeypatch.setattr(settings, "backup_encryption_key", "not-base64!!")
    with pytest.raises(export_mod.ExportRefused):
        backup_config.require_encrypted_ready(db)

    monkeypatch.setattr(settings, "backup_encryption_key",
                        base64.b64encode(b"short").decode())
    with pytest.raises(export_mod.ExportRefused):
        backup_config.require_encrypted_ready(db)


def test_encryption_ready永远不抛异常(monkeypatch):
    """``encryption_ready()`` 是给界面用的，**只回答「是/否」**。

    它不抛异常：不合格的密钥算「不 ready」，而不是把一个异常抛给
    「测试连接」那个按钮 —— 那会让「能不能连通」这件事被一把坏密钥挡住，
    而这两件事本来就不该互相挡。
    """
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    assert backup_config.encryption_ready() is False

    monkeypatch.setattr(settings, "backup_encryption_key", "not-base64!!")
    assert backup_config.encryption_ready() is False

    monkeypatch.setattr(settings, "backup_encryption_key",
                        base64.b64encode(b"short").decode())
    assert backup_config.encryption_ready() is False

    monkeypatch.setattr(settings, "backup_encryption_key", GOOD_KEY)
    assert backup_config.encryption_ready() is True


# --------------------------------------------------------------------- 界面两条路由


def test_测试连接不拦但必须说清备份跑不起来(no_env_oss, db, client, auth, monkeypatch):
    """「测试连接」验的是**连通性**，不该被一把没配的密钥挡住。

    但连通性通过、备份跑不起来，是最坏的一种组合（界面说「连接正常」，
    运维就以为退路是好的）。所以响应里必须带上 ``encryption_ready``，
    并且消息里要说清。
    """
    from app.api import system as system_api

    monkeypatch.setattr(system_api.oss_client, "check_connection",
                        lambda cfg: "可写入 acct-backup/acct-backup")
    _set_db_oss(db)

    resp = client.post("/api/system/backup/test", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encryption_ready"] is False
    assert "BACKUP_ENCRYPTION_KEY" in body["message"], body["message"]
    assert "备份" in body["message"]


def test_立即备份在缺密钥时返回400(no_env_oss, db, client, auth):
    """「立即备份」在**派发后台任务之前**就拒绝。

    拒绝在后台任务之前是关键：后台任务里抛的异常会被 Starlette 吞进
    stderr，界面上只看到「没有新记录」，而这条 400 会直接告诉运维
    「先去配密钥」。
    """
    _set_db_oss(db)

    resp = client.post("/api/system/backup/run", headers=auth)

    assert resp.status_code == 400, resp.text
    assert "BACKUP_ENCRYPTION_KEY" in resp.text


def test_立即备份在配了密钥时放行(no_env_oss, db, client, auth, monkeypatch):
    """配了密钥 ⇒ 放行。"""
    from app.api import system as system_api

    跑过: list[bool] = []
    monkeypatch.setattr(settings, "backup_encryption_key", GOOD_KEY)
    monkeypatch.setattr(system_api.backup_service, "run_backup",
                        lambda *a, **kw: 跑过.append(True))
    _set_db_oss(db)

    resp = client.post("/api/system/backup/run", headers=auth)

    assert resp.status_code == 200, resp.text
    assert 跑过 == [True]


def test_测试连接在配了密钥时报告就绪(no_env_oss, db, client, auth, monkeypatch):
    """配了密钥 ⇒ ``encryption_ready`` 为真，消息里不再有那句提醒。"""
    from app.api import system as system_api

    monkeypatch.setattr(settings, "backup_encryption_key", GOOD_KEY)
    monkeypatch.setattr(system_api.oss_client, "check_connection",
                        lambda cfg: "可写入 acct-backup/acct-backup")
    _set_db_oss(db)

    body = client.post("/api/system/backup/test", headers=auth).json()

    assert body["encryption_ready"] is True
    assert "BACKUP_ENCRYPTION_KEY" not in body["message"], body["message"]


# ------------------------------------------------------------------- 脚本那条路


def test_脚本把配置不全变成退出码2而不是裸异常(monkeypatch, db, book, tmp_path):
    """``scripts/backup_to_oss.py`` 撞上这道闸时**退出码必须是 2**。

    裸异常会落成 exit 1（「脚本崩了」），而真相是「配置没配全」——
    这在排障时是两条完全不同的路。退出码 2 的含义本来就是
    「配置不全 / 没有任何东西可备」。
    """
    import importlib.util
    import sys

    脚本 = tmp_path / "backup_to_oss.py"
    源 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "backup_to_oss.py")
    with open(源, encoding="utf-8") as fh:
        脚本.write_text(fh.read())
    spec = importlib.util.spec_from_file_location("backup_to_oss_under_test", 脚本)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backup_to_oss_under_test"] = mod
    spec.loader.exec_module(mod)

    _clear_env_oss(monkeypatch)
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    _set_db_oss(db)

    # **把 OSS 换成本地假实现。** 这条用例在闸门被拆掉（反向验证）时，
    # 脚本会真的往下跑 —— 那时它会**连真实的阿里云 OSS**，
    # 拿一个不存在的桶去撞 404。这既慢又不该发生：
    # 一条用例失败时不该去打公网。换成假的之后，失败会**本地**发生，
    # 报出来的是「退出码不是 2」这个真正关心的结论。
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file", lambda b, k, p: "etag")
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    # 通知邮件发不出去不能影响退出码（脚本里那个 except 已经处理了），
    # 这里把它按「发不出去」处理，免得真的去连 SMTP。
    monkeypatch.setattr(mod, "_通知备份失败", lambda message: None)

    assert mod.main(["--book", str(book.id)]) == 2
