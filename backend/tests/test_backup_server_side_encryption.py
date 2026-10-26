"""备份制品的**静态加密**由 OSS 服务端加密（SSE）承担（2026-10-26 起）。

**背景：客户端那一层被整段拿掉了。**

原来备份管线在上传前会用 AES-256-GCM 在**客户端**加密制品
（``app/backup/export.py`` 里的 ``encrypt_file`` / ``decrypt_file`` /
``encryption_key``），并配一道「配了 OSS 就必须配备份加密密钥」的启动闸与
运行期闸（以及一个逃生口 ``BACKUP_ENCRYPTION_KEY_WARN_ONLY``）。

现在那一层**没有了**：桶上开了**默认加密规则**（SSE-OSS / AES256），
不带 SSE 头的普通上传同样被服务端加密 —— 实测：不带头传的对象，HEAD 回来
同样带 ``x-oss-server-side-encryption: AES256``。所以本项目再加密一遍是
同一件事做两遍，还要自己管一把钥匙。

**代价要说清楚（这是运维侧显式选的取舍，不是本项目默认的姿态）**：
SSE 保护的是**静态数据**，它**不**保护「拿到桶读权限的人」—— 那种人照样
能让 OSS 替他解密。客户端加密配一把**单独存放**的密钥时，那才是连 AK 泄露
都不怕。

**所以这一份文件守的是「新的事实」**：

1. 制品**不再**在客户端加密（上传的字节就是 gzip 本身）；
2. 「桶上开了 SSE 没有」是**看得见的事实**（不是配置承诺）；
3. 开了合规保留（WORM）的桶里，**删不掉是正常的** —— 那是保留策略在工作，
   不是故障。
"""
from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest

from app.backup import export as export_mod
from app.backup import oss_client as oss_mod
from app.backup import service as service_mod
from app.core.escape_hatches import APP_SIDE_ESCAPE_HATCHES


# --------------------------------------------------------------------- 假桶


class _FakeBucket:
    """只实现本文件用到的那几个方法。"""

    def __init__(self, *, algorithm: str | None = "AES256", immutable: bool = False):
        self.algorithm = algorithm
        self.immutable = immutable
        self.deleted: list[str] = []
        self.put: list[tuple[str, bytes]] = []

    def get_bucket_encryption(self):
        import oss2

        if self.algorithm is None:
            raise oss2.exceptions.NoSuchServerSideEncryptionRule(404, {}, b"", {})
        return SimpleNamespace(sse_algorithm=self.algorithm, kms_id=None)

    def put_object(self, key, data):
        self.put.append((key, data))
        return SimpleNamespace(etag="etag")

    def delete_object(self, key):
        import oss2

        self.deleted.append(key)
        if self.immutable:
            raise oss2.exceptions.OssError(
                409, {"x-oss-request-id": "req-1"}, b"",
                {"Code": "FileImmutable",
                 "Message": "The object is WORM protected and cannot be deleted."})

    def session(self):
        return None


# --------------------------------------------------------------------- 一、客户端不再加密


def test_上传的字节就是gzip本身_不是密文(monkeypatch, db, book, admin_user):
    """制品**不在客户端加密** —— 上传到 OSS 的就是导出层写出来的那份 gzip。

    钉住的是「**没有第二层**」：一旦有人又加回客户端加密，这条会立刻红
    （密文的前两个字节不是 gzip 魔数）。
    """
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    uploaded: dict[str, bytes] = {}

    def fake_put_file(bucket, key, path):
        with open(path, "rb") as fh:
            uploaded[key] = fh.read()
        return "etag"

    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file", fake_put_file)
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])

    assert uploaded, "备份没有走文件上传"
    for key, data in uploaded.items():
        assert data.startswith(b"\x1f\x8b"), (key, data[:8])
        # gzip 里能读出这一份归档 —— 它是**可读的 gzip**，不是被包过的密文。
        assert json.loads(gzip.decompress(data))["header"]["format"] == export_mod.FORMAT


def test_校验和就是上传那份字节的sha256(monkeypatch, db, book, admin_user):
    """``sys_backup_run.checksum`` 记的是 **OSS 上真正存着的那串字节**的 SHA256。

    客户端加密那层没了之后，「压缩后写进文件的那串字节」与「上传上去的那串
    字节」**是同一串** —— 所以校验和不需要另算一遍，也不会出现
    「校验和与对象对不上」那个经典坑。
    """
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    uploaded: dict[str, bytes] = {}

    def fake_put_file(bucket, key, path):
        with open(path, "rb") as fh:
            uploaded[key] = fh.read()
        return "etag"

    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file", fake_put_file)
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    result = service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])

    import hashlib

    for run in result.runs:
        if run.status != "success":
            continue
        assert run.checksum == hashlib.sha256(uploaded[run.oss_key]).hexdigest(), run.oss_key


def test_配置里已经没有加密密钥这一项了():
    """``BACKUP_ENCRYPTION_KEY`` / 它的豁免开关**都不存在了**。

    留一个「配了也不生效」的配置项，比删掉更糟 —— 运维会以为配了就加密了。
    """
    from app.core.config import Settings

    fields = Settings.model_fields
    assert "backup_encryption_key" not in fields
    assert "backup_encryption_key_warn_only" not in fields
    # 逃生口清单里也不该再有它 —— 那个名字对应的那道闸已经不存在了。
    assert "BACKUP_ENCRYPTION_KEY_WARN_ONLY" not in APP_SIDE_ESCAPE_HATCHES


def test_明文制品可以照常恢复(tmp_path, db, book):
    """制品是 gzip，恢复路径就**直接读 gzip** —— 退路没有因为去掉加密而断。"""
    out = tmp_path / "book.json.gz"
    export_mod.export_book(db, book, out)

    with gzip.open(out, "rb") as fh:
        data = json.load(fh)
    assert data["header"]["format"] == export_mod.FORMAT
    assert data["tables"]


# --------------------------------------------------------------------- 二、SSE 是看得见的事实


def test_读得到桶的服务端加密算法():
    """开了 SSE ⇒ 读得出算法名。"""
    bucket = _FakeBucket(algorithm="AES256")
    assert oss_mod.server_side_encryption(bucket) == "AES256"


def test_桶上没配默认加密规则时读出来是None():
    """没开 SSE ⇒ None（不是异常）—— 「没开」是一个状态，不是故障。"""
    bucket = _FakeBucket(algorithm=None)
    assert oss_mod.server_side_encryption(bucket) is None


def test_备份时会把SSE状态记进日志(monkeypatch, db, book, admin_user, caplog):
    """**开了**：INFO 说清楚；**没开**：WARNING 说清楚。

    SSE 是**桶上的开关**，不在本项目的配置里 —— 有人把它关掉，备份就静悄悄地
    变成「静态不加密」。所以它必须**在日志里被看见**。
    """
    import logging

    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    monkeypatch.setattr(oss_mod, "put_file", lambda b, k, p: "etag")
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: _FakeBucket(algorithm="AES256"))
    with caplog.at_level(logging.INFO, logger="app.backup.service"):
        service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])
    assert any("服务端加密已开启" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]

    caplog.clear()
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: _FakeBucket(algorithm=None))
    with caplog.at_level(logging.WARNING, logger="app.backup.service"):
        service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])
    assert any("没有开启服务端加密" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_读不到SSE状态不该让备份失败(monkeypatch, db, book, admin_user, caplog):
    """SSE 状态读不出来时，**备份照跑** —— 它只是让我们少一个事实。

    判错的代价是「备份因为读不到一个状态而没跑」：那是拿退路去换一个
    无关紧要的报告。
    """
    import logging

    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    class _坏桶(_FakeBucket):
        def get_bucket_encryption(self):
            raise RuntimeError("网络抖了一下")

    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: _坏桶())
    monkeypatch.setattr(oss_mod, "put_file", lambda b, k, p: "etag")
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    with caplog.at_level(logging.WARNING, logger="app.backup.service"):
        result = service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])

    assert result.ok, [(r.status, r.message[:120]) for r in result.runs]
    assert any("读不到" in r.message for r in caplog.records)


def test_测试连接会报出桶的服务端加密状态(client, auth, db, monkeypatch):
    """「测试连接」的回答里带着桶**实际的**加密算法。

    这条取代了原来那个 ``encryption_ready`` 字段 —— 后者说的是
    「本项目配没配密钥」，而现在真正要紧的是「桶上开没开 SSE」。
    """
    from app.api import system as system_api
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    bucket = _FakeBucket(algorithm="AES256")
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: bucket)
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(oss_mod, "delete_object", lambda b, k: None)
    monkeypatch.setattr(system_api.oss_client, "oss_bucket",
                        lambda cfg, **kw: _nullcontext(bucket))

    resp = client.post("/api/system/backup/test", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["server_side_encryption"] == "AES256"
    assert "服务端加密已开启" in resp.json()["message"]


# --------------------------------------------------------------------- 三、保留策略（WORM）


def test_删不掉的对象被认成受保护而不是故障():
    """``FileImmutable`` ⇒ :class:`ObjectProtected`，**不是**普通失败。

    开了合规保留的桶里，删一份过期备份会被服务端拒掉 —— 那**不是故障**，
    那是保留策略在正常工作。
    """
    bucket = _FakeBucket(immutable=True)
    with pytest.raises(oss_mod.ObjectProtected) as exc:
        oss_mod.delete_object(bucket, "backup/daily/book-1/x.json.gz")
    assert "保留策略" in str(exc.value)


def test_清理时遇到受保护对象_跳过它而不是报错(monkeypatch):
    """清理遇到受保护对象 ⇒ 跳过、不记进「已删」、**不让异常冒出去**。

    算成「清理失败」的代价是每轮备份都报一个假故障，而真实情况是
    「该留的都留下了」。
    """
    from app.backup import retention as ret

    老对象 = SimpleNamespace(key="acct-backup/daily/book-1/20200101-000000.json.gz",
                             size=1, last_modified=None)
    monkeypatch.setattr(ret, "iter_objects", lambda b, p: [老对象])
    monkeypatch.setattr(ret, "plan_deletions",
                        lambda objs, tier, keep, now: [(objs[0].key, 999.0)])

    def 删不掉(bucket, key):
        raise oss_mod.ObjectProtected(f"受保护：{key}")

    monkeypatch.setattr(ret, "delete_object", 删不掉)

    deleted = ret.apply_retention(object(), "acct-backup", {"daily": 30})
    assert deleted == [], deleted


def test_测试连接在保留策略下仍然可用(monkeypatch):
    """探针对象删不掉 ⇒ **按钮照样是绿的**。

    这个按钮要回答的是「能不能写」—— 而写入**已经成功了**。
    """
    bucket = _FakeBucket(immutable=True)
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: bucket)
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")

    message = oss_mod.check_connection({"bucket": "acct963", "prefix": "backup",
                                        "endpoint": "https://oss-cn-x.aliyuncs.com",
                                        "access_key_id": "AK", "access_key_secret": "SK"})
    assert "可写入" in message
    assert "受保留策略保护" in message


def test_探针key每次都是新的():
    """探针 key **必须每次不同** —— 已受保护的对象不可覆盖。

    用固定 key 的话，第二次点这个按钮就会在 PUT 上被服务端拒掉，
    按钮从此永远是红的，而实际上「能不能写」早就验证过了。
    """
    import re

    from app.backup import oss_client as real

    keys: list[str] = []
    bucket = _FakeBucket()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(real, "make_bucket", lambda cfg, **kw: bucket)
        monkey.setattr(real, "put_bytes", lambda b, k, d: keys.append(k))
        monkey.setattr(real, "delete_object", lambda b, k: None)
        for _ in range(3):
            real.check_connection({"bucket": "b", "prefix": "backup",
                                   "endpoint": "https://oss-cn-x.aliyuncs.com",
                                   "access_key_id": "AK", "access_key_secret": "SK"})
    finally:
        monkey.undo()

    assert len(keys) == 3
    assert len(set(keys)) == 3, keys
    assert all(re.search(r"\.healthcheck-[0-9a-f]{16}$", k) for k in keys), keys


class _nullcontext:
    """oss_bucket 的最小替身。"""

    def __init__(self, bucket):
        self.bucket = bucket

    def __enter__(self):
        return self.bucket

    def __exit__(self, *exc):
        return False
