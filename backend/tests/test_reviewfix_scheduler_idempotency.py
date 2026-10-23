"""审查修复：调度任务的幂等键与租约放弃（H-08）。

**H-08（中）：租约超时后被并发接管 → 重复执行。**

租约（600 秒）由后台线程每 60 秒续期。续期失败（DB 抖动、GC 长暂停、
SIGSTOP）之后租约过期，**另一个 worker 会接管同一个 ``run_id``**，而前一个
worker 若还在跑，副作用已经发生。``_finish`` 用 ``row.lease_token !=
claim["token"]`` 挡住了**结果写入**，但**副作用本身**没有被挡住。

这里钉住两件事：

1. **同一个触发点（``{task_key}:{scheduled_for}``）只做一次副作用** ——
   第二次是空转（no-op）；
2. **续期线程发现租约丢了 ⇒ 主任务放弃本次执行**，把活儿让给接管者。
"""
from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.models import AuditLog, ScheduledTask, ScheduledTaskRun, SystemSetting
from app.scheduler import service


@pytest.fixture
def scheduler_db(db):
    db.execute(delete(ScheduledTaskRun))
    db.execute(delete(ScheduledTask))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.execute(delete(ScheduledTaskRun))
        db.execute(delete(ScheduledTask))
        db.execute(delete(SystemSetting).where(SystemSetting.key.in_([
            "scheduler_trigger_secret", "scheduler_trigger_secret_previous", "backup_enabled",
        ])))
        db.commit()


def _system_claim() -> dict:
    return {"task_key": "system:oss_backup", "owner_type": "SYSTEM", "task_name": "oss_backup",
            "run_id": uuid.uuid4().hex, "token": uuid.uuid4().hex, "plugin_id": None,
            "plugin_digest": "", "scheduled_for": service.utcnow(), "claim_count": 1}


def _claim(task_key: str, now) -> dict:
    return {"task_key": task_key, "scheduled_for": now, "run_id": uuid.uuid4().hex,
            "owner_type": "SYSTEM", "plugin_id": None, "plugin_digest": "",
            "claim_count": 1, "token": uuid.uuid4().hex}


def test_幂等键是触发点的身份_同一个scheduled_for同一个键(scheduler_db):
    """幂等键 = ``{task_key}:{scheduled_for}``。

    同一个触发点 ⇒ 同一个键；换了一个触发点 ⇒ 换了一个键（那是新一轮，
    本来就该再做一次）。
    """
    now = service.utcnow()
    first = _claim("system:oss_backup", now)
    second = _claim("system:oss_backup", now)
    other = _claim("system:oss_backup", now + timedelta(minutes=1))

    assert service.system_idempotency_key(first) == service.system_idempotency_key(second)
    assert service.system_idempotency_key(first) != service.system_idempotency_key(other)
    assert service.system_idempotency_key(first).startswith("system:oss_backup:")


def test_同一个触发点重复触发时第二次是空转(scheduler_db, monkeypatch):
    """**副作用只发生一次。** 第二次是 no-op，不产生第二次副作用。"""
    calls: list[int] = []

    def fake_backup(db, **kwargs):
        calls.append(1)
        return SimpleNamespace(failed=[], prune_error=None, stopped=0, runs=[], deleted=[])

    monkeypatch.setattr(service.backup_service, "run_backup", fake_backup)

    claim = _claim("system:oss_backup", service.utcnow())
    key = service.system_idempotency_key(claim)

    first = service._run_system("oss_backup", idempotency_key=key)
    second = service._run_system("oss_backup", idempotency_key=key)

    assert len(calls) == 1, f"同一个触发点跑了 {len(calls)} 次副作用"
    assert first["status"] == "SUCCESS", first
    assert second["status"] == "SKIPPED", second
    assert second["error_code"] == "ALREADY_STARTED", second
    assert "幂等键" in second["message"], second


def test_换一个触发点就会再做一次(scheduler_db, monkeypatch):
    """新的一轮（新的 ``scheduled_for``）**不受**上一轮的幂等标记影响。"""
    calls: list[int] = []

    def fake_backup(db, **kwargs):
        calls.append(1)
        return SimpleNamespace(failed=[], prune_error=None, stopped=0, runs=[], deleted=[])

    monkeypatch.setattr(service.backup_service, "run_backup", fake_backup)

    now = service.utcnow()
    service._run_system("oss_backup", idempotency_key=service.system_idempotency_key(
        _claim("system:oss_backup", now)))
    service._run_system("oss_backup", idempotency_key=service.system_idempotency_key(
        _claim("system:oss_backup", now + timedelta(minutes=1))))

    assert len(calls) == 2, "新一轮必须真的再做一次"


def test_幂等标记落在审计里可查(scheduler_db, monkeypatch):
    """标记要**可审计** —— 事后能查到这个触发点被谁开始过。"""
    monkeypatch.setattr(service.backup_service, "run_backup",
                        lambda db, **kw: SimpleNamespace(failed=[], prune_error=None, stopped=0))
    claim = _claim("system:oss_backup", service.utcnow())
    key = service.system_idempotency_key(claim)
    service._run_system("oss_backup", idempotency_key=key)

    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(
            AuditLog.action == service.SYSTEM_TASK_MARK_ACTION)).all()
        assert rows, "幂等标记必须留下审计"
        assert any(row.after.get("idempotency_key") == key for row in rows), \
            [row.after for row in rows]


def test_租约丢了主任务放弃本次执行(scheduler_db, monkeypatch):
    """续期线程发现租约被接管 ⇒ 主任务**不执行**副作用，把活儿让给接管者。"""
    calls: list[int] = []

    def fake_backup(db, **kwargs):
        calls.append(1)
        return SimpleNamespace(failed=[], prune_error=None, stopped=0)

    monkeypatch.setattr(service.backup_service, "run_backup", fake_backup)

    lease = threading.Event()
    lease.set()   # 租约已经丢了

    outcome = service._run_system("oss_backup",
                                  idempotency_key=service.system_idempotency_key(
                                      _claim("system:oss_backup", service.utcnow())),
                                  lease=lease)

    assert outcome["status"] == "SKIPPED", outcome
    assert outcome["error_code"] == "LEASE_LOST", outcome
    assert not calls, "租约都丢了还跑副作用 —— 那正是重复执行的来源"


def test_一开始就续不上租约时主任务根本不启动(scheduler_db, monkeypatch):
    """``execute_claim`` 开头那次续期就失败 ⇒ 什么都不做。"""
    executed: list[str] = []
    monkeypatch.setattr(service, "_renew_lease", lambda claim: False)
    monkeypatch.setattr(service, "_run_system",
                        lambda name, **kw: executed.append(name) or {"status": "SUCCESS"})

    service.execute_claim(_system_claim())
    assert not executed, "租约一开始就续不上，却仍然跑了主任务"


def test_续期线程发现租约被接管就通知主任务放弃(scheduler_db, monkeypatch):
    """**续期线程不再只是 ``return``** —— 它要让主任务知道。

    以前续期失败只是 ``return``，主任务**照跑不误**：于是两个 worker 同时
    对同一个触发点跑副作用。
    """
    monkeypatch.setattr(service, "LEASE_RENEW_SECONDS", 0.05)
    seen: dict = {}

    def fake_run_system(name, *, idempotency_key="", lease=None):
        for _ in range(300):
            if lease is not None and lease.is_set():
                seen["lost"] = True
                break
            threading.Event().wait(0.01)
        return {"status": "SKIPPED", "error_code": "LEASE_LOST", "message": "租约已丢失"}

    state = {"n": 0}

    def renewing(claim_):
        # 第一次（execute_claim 开头那次）成功；之后续不上 ⇒ 租约已被接管。
        state["n"] += 1
        return state["n"] == 1

    monkeypatch.setattr(service, "_renew_lease", renewing)
    monkeypatch.setattr(service, "_run_system", fake_run_system)

    service.execute_claim(_system_claim())
    assert seen.get("lost") is True, "续期线程发现租约被接管后没有通知主任务"


def test_备份轮次中途停止要如实报出来(scheduler_db, monkeypatch):
    """备份跑到一半发现租约丢了 ⇒ **停**，并且如实记下「还有几个没备份」。

    「提前停止」不能报成成功 —— 剩下的目标确实没备份。
    """
    def fake_backup(db, **kwargs):
        assert kwargs.get("should_stop") is not None, "调度路径必须把停止钩子传进来"
        return SimpleNamespace(failed=[], prune_error=None, stopped=2, runs=[], deleted=[])

    monkeypatch.setattr(service.backup_service, "run_backup", fake_backup)

    with pytest.raises(RuntimeError) as exc:
        service._run_system("oss_backup",
                            idempotency_key=service.system_idempotency_key(
                                _claim("system:oss_backup", service.utcnow())),
                            lease=threading.Event())
    assert "提前停止" in str(exc.value), str(exc.value)
    assert "2" in str(exc.value), str(exc.value)
