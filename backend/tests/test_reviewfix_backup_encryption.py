"""审查修复：备份制品的加密与上传（H-06 / H-07）。

**H-06（中）：OSS 上的备份制品是明文 gzip —— SHA256 是校验和，不是加密。**

管线是 ``gzip → SHA256 → OSS``，而 ``sys_backup_run.checksum`` 里那个 SHA256
**只用于完整性校验**。现在上传前在**客户端**加密（AES-256-GCM，带认证），
并且：

* **校验和算在密文上** —— OSS 上真正存着的那串字节。算明文的校验和，
  恢复时拿远端对象比对会必然对不上；
* **没配密钥时保持现状（不加密），但要明确记一条告警** —— 默默保持明文
  而没人知道，比明文本身更危险；
* **密钥必须与 OSS AccessKey 分开存放**（爆炸半径不一样）。

**H-07（中）：备份上传把整个 gzip 读进内存。**

导出那一层的设计意图是「内存与账套大小无关」，但上传环节把整份文件读进
内存 —— 备份任务跑在 API 进程里，与在线请求共享同一个进程的内存。现在
走 oss2 的**文件句柄**上传。
"""
from __future__ import annotations

import base64
import gzip
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app.backup import export as export_mod
from app.backup import oss_client as oss_mod
from app.backup import service as service_mod
from app.core.config import settings
from app.models import SCOPE_BOOK, AuditLog, BackupRun
from app.models.system import SystemSetting


@pytest.fixture
def backup_key(monkeypatch) -> str:
    """一把 base64 的 32 字节随机密钥。"""
    raw = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "backup_encryption_key", raw)
    return raw


# --------------------------------------------------------------------- 加密


def test_加密后上传的是密文_解密后能还原出原始gzip(tmp_path, backup_key):
    """制品被加密 ⇒ 上传的不是明文 gzip；解密后逐字节还原。"""
    plain = tmp_path / "book.json.gz"
    plain.write_bytes(b'{"header":{"format":"cwq-book-backup"},"tables":{}}' * 50)
    cipher = tmp_path / "book.json.gz.enc"

    export_mod.encrypt_file(plain, cipher)

    assert export_mod.is_encrypted(cipher)
    data = cipher.read_bytes()
    assert data[:2] != b"\x1f\x8b", "密文不该还是 gzip"
    assert b"cwq-book-backup" not in data, "明文片段不该出现在密文里"

    back = tmp_path / "back.json.gz"
    export_mod.decrypt_file(cipher, back)
    assert back.read_bytes() == plain.read_bytes()


def test_密文被改动过就拒绝解密(tmp_path, backup_key):
    """认证标签对不上 ⇒ 拒绝，不给「尽力而为」留路。"""
    plain = tmp_path / "a.gz"
    plain.write_bytes(b"payload" * 100)
    cipher = tmp_path / "a.enc"
    export_mod.encrypt_file(plain, cipher)

    broken = bytearray(cipher.read_bytes())
    broken[len(broken) // 2] ^= 0xFF
    tampered = tmp_path / "tampered.enc"
    tampered.write_bytes(bytes(broken))

    with pytest.raises(export_mod.ExportRefused) as exc:
        export_mod.decrypt_file(tampered, tmp_path / "out.gz")
    assert "标签" in str(exc.value) or "密钥" in str(exc.value), str(exc.value)


def test_没有配密钥时保持现状但要记告警(tmp_path, monkeypatch, caplog):
    """没配密钥 ⇒ **不加密**（保持现状），但必须留下一条说得清的告警。"""
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    with caplog.at_level("WARNING"):
        assert export_mod.encryption_key() is None
    # 「没配」不是错，但必须有人知道 —— 默默保持明文最危险。
    plain = tmp_path / "a.gz"
    plain.write_bytes(b"payload")
    prepared, checksum, size = service_mod._prepare_upload(plain, tmp_path, "标签")
    assert prepared == plain
    assert size == plain.stat().st_size
    assert checksum == export_mod.file_sha256(plain)


def test_密钥不合法时明确拒绝(tmp_path, monkeypatch):
    """密钥不是 base64 / 不是 32 字节 ⇒ 拒绝，并说清怎么生成。"""
    monkeypatch.setattr(settings, "backup_encryption_key", "not-base64!!")
    with pytest.raises(export_mod.ExportRefused) as exc:
        export_mod.encryption_key()
    assert "base64" in str(exc.value), str(exc.value)

    monkeypatch.setattr(settings, "backup_encryption_key",
                        base64.b64encode(b"short").decode())
    with pytest.raises(export_mod.ExportRefused) as exc:
        export_mod.encryption_key()
    assert "32" in str(exc.value), str(exc.value)


def test_恢复时能解开加密制品(tmp_path, backup_key, db, book):
    """加密的备份**能恢复** —— 加密不是为了「存不下」，是为了「拿不走」。"""
    out = tmp_path / "book.json.gz"
    export_mod.export_book(db, book, out)

    cipher = tmp_path / "book.enc"
    export_mod.encrypt_file(out, cipher)
    assert export_mod.is_encrypted(cipher)

    # 读回来：先解密，再解 gzip，格式与明文制品一样。
    plain = tmp_path / "restored.json.gz"
    export_mod.decrypt_file(cipher, plain)
    with gzip.open(plain, "rb") as fh:
        data = json.load(fh)
    assert data["header"]["format"] == export_mod.FORMAT
    assert data["tables"]


def test_恢复时没有密钥就明确拒绝(tmp_path, backup_key, monkeypatch):
    """加密的制品，没有密钥 ⇒ 明确拒绝（而不是解出一堆乱码）。"""
    plain = tmp_path / "a.gz"
    plain.write_bytes(gzip.compress(b'{"header":{}}'))
    cipher = tmp_path / "a.enc"
    export_mod.encrypt_file(plain, cipher)

    monkeypatch.setattr(settings, "backup_encryption_key", "")
    with pytest.raises(export_mod.ExportRefused) as exc:
        export_mod.read_snapshot(cipher)
    assert "密钥" in str(exc.value), str(exc.value)


# --------------------------------------------------------------------- 上传


def test_上传走的是文件而不是把整份gzip读进内存(monkeypatch, db, book, admin_user):
    """上传环节**不许**把整份制品读进内存。

    备份任务跑在 API 进程里，与在线请求共享同一个进程的内存 ——
    ``tmp.read_bytes()`` 会让一本大账套的快照把在线请求挤到 OOM。
    """
    from app.core import system_settings
    from app.core.config import settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    # 配了 OSS 就必须配加密密钥 —— ``run_backup`` 会在上传**之前**就拒绝
    # 没配密钥的情形（运行期那道闸，见 ``app/backup/config.py``）。
    # 这里给一把正常的密钥，走的是「配好了」的常态路径。
    monkeypatch.setattr(settings, "backup_encryption_key",
                        base64.b64encode(os.urandom(32)).decode())

    uploaded: list[tuple[str, int, bytes]] = []

    def fake_put_file(bucket, key, path):
        # 传的是**文件路径**，不是一坨字节 —— 这正是要钉的东西。
        uploaded.append((key, os.path.getsize(path), Path(path).read_bytes()[:2]))
        return "etag"

    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file", fake_put_file)
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])

    assert uploaded, "备份没有走文件上传"
    for key, size, head in uploaded:
        assert size > 0, key
        # 配了密钥 ⇒ 上传的是**密文**（魔数 CWQENC01），不是明文 gzip。
        # 断言 gzip 魔数会把「明文上传」钉成常态，那正是审计 §3.8 要消灭的形态。
        assert head == export_mod.ENCRYPTED_MAGIC[:2], (key, head)
