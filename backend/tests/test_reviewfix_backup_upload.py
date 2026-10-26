"""审查修复：备份制品的**上传**（H-07）。

**H-07（中）：备份上传把整个 gzip 读进内存。**

导出那一层的设计意图是「内存与账套大小无关」，但上传环节把整份文件读进
内存 —— 备份任务跑在 API 进程里，与在线请求共享同一个进程的内存。现在
走 oss2 的**文件路径**上传。

**H-06（原「OSS 上的备份制品是明文 gzip」）已于 2026-10-26 改写。**

原来这一份里还有客户端加密（AES-256-GCM）的用例：加密、密钥校验、
「密文被改动过就拒绝解密」、以及「没配密钥时的告警分支」。那一整套机制
**整段移除**了 —— 备份制品的静态加密改由 **OSS 服务端加密（SSE）** 承担
（桶上开了默认加密规则，不带 SSE 头的普通上传同样被服务端加密）。

新的事实由 ``tests/test_backup_server_side_encryption.py`` 守着：
制品**不在客户端加密**、SSE 状态**看得见**、保留策略下的删除**不算故障**。
这一份只留 H-07（上传不许把整份 gzip 读进内存）——
它是**内存**的性质，与加密无关，所以不受那次移除影响。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.backup import oss_client as oss_mod
from app.backup import service as service_mod


# --------------------------------------------------------------------- 上传


def test_上传走的是文件而不是把整份gzip读进内存(monkeypatch, db, book, admin_user):
    """上传环节**不许**把整份制品读进内存。

    备份任务跑在 API 进程里，与在线请求共享同一个进程的内存 ——
    ``tmp.read_bytes()`` 会让一本大账套的快照把在线请求挤到 OOM。
    """
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

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
        # 上传上去的是**那份 gzip 本身**（客户端不再加密）。
        assert head == b"\x1f\x8b", (key, head)


def test_上传用的那个文件在上传后被删掉(monkeypatch, db, book, admin_user, tmp_path):
    """上传完的临时文件**必须删** —— 备份产物是全量账务数据。

    它是「边导边写」落下来的那份 gzip，留在盘上就是多一份没人管的全量副本。
    """
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup")):
        system_settings.set_value(db, key, value)
    db.commit()

    seen: list[str] = []

    def fake_put_file(bucket, key, path):
        seen.append(path)
        assert Path(path).exists(), "上传时文件不在"
        return "etag"

    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: object())
    monkeypatch.setattr(oss_mod, "put_file", fake_put_file)
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(service_mod.retention, "apply_retention", lambda *a, **k: [])

    service_mod.run_backup(db, actor=admin_user, book_ids=[book.id])

    assert seen, "备份没有走文件上传"
    for path in seen:
        assert not Path(path).exists(), f"上传完的临时文件没删：{path}"
