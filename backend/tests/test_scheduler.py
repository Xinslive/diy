from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from starlette.requests import Request
from starlette.responses import Response

from app.core.db import SessionLocal
from app.core import system_settings
from app.main import request_id_middleware
from app.models import AuditLog, ScheduledTask, ScheduledTaskRun, SysUser, SystemSetting
from app.scheduler import service
from tests.plugin_fixtures import plugin_env


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


@pytest.mark.parametrize("metadata,name,expected", [
    (None, "nightly", False),
    ({}, "nightly", False),
    ({"tasks": []}, "nightly", False),
    ({"tasks": [{"name": "nightly"}]}, "nightly", False),
    ({"tasks": [{"name": "nightly", "enabled_default": False}]}, "nightly", False),
    ({"tasks": [{"name": "nightly", "enabled_default": True}]}, "nightly", True),
    # 别的任务声明了默认启用，不代表这个任务也启用。
    ({"tasks": [{"name": "other", "enabled_default": True}]}, "nightly", False),
    ({"tasks": [{"name": "other", "enabled_default": True}, {"name": "nightly"}]}, "nightly", False),
    # 清单里没有任务声明这一节（老插件）时按未声明处理。
    ({"version": "1.0.0"}, "nightly", False),
])
def test_task_default_enabled_reads_only_the_named_declaration(metadata, name, expected):
    assert service.task_default_enabled(SimpleNamespace(manifest_metadata=metadata), name) is expected


def test_schedule_next_occurrence_and_coalesces_missed_intervals():
    now = datetime(2026, 10, 17, 19, 46, tzinfo=timezone.utc)
    assert service._next({"type": "cron", "expression": "47 3 * * *"}, now) == datetime(
        2026, 10, 17, 19, 47, tzinfo=timezone.utc
    )
    assert service._next_after_claim(
        {"type": "interval", "minutes": 15}, now, now + timedelta(minutes=37)
    ) == now + timedelta(minutes=45)


def test_tick_auth_and_background_dispatch(client, monkeypatch):
    token = "scheduler-test-token"
    digest = service.secret_digest(token)
    monkeypatch.setattr(service, "trigger_secret_digests", lambda _db: (digest, ""))
    monkeypatch.setattr(service, "claim_due", lambda _runtime: [
        {"run_id": "a" * 32}, {"run_id": "b" * 32}
    ])
    calls = []

    async def execute(claims, runtime):
        calls.append((claims, runtime))

    monkeypatch.setattr(service, "execute_claims", execute)
    assert client.get("/api/scheduler/tick").status_code == 401
    assert client.get("/api/scheduler/tick?key=wrong").status_code == 401
    response = client.get(f"/api/scheduler/tick?key={token}")
    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True, "accepted": 2, "run_ids": ["a" * 32, "b" * 32]}
    assert len(calls) == 1 and len(calls[0][0]) == 2


def test_tick_reports_unconfigured_secret(client, monkeypatch):
    monkeypatch.setattr(service, "trigger_secret_digests", lambda _db: ("", ""))
    response = client.get("/api/scheduler/tick?key=anything")
    assert response.status_code == 503


def test_tick_middleware_keeps_key_out_of_downstream_query():
    key = "never-log-this-value"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "path": "/api/scheduler/tick",
        "raw_path": b"/api/scheduler/tick", "query_string": f"key={key}".encode(),
        "root_path": "", "headers": [(b"host", b"example.test")],
        "client": ("127.0.0.1", 1234), "server": ("example.test", 443),
    }
    seen = {}

    async def call_next(request):
        seen["token"] = request.state.scheduler_token
        seen["query_string"] = request.scope["query_string"]
        return Response("ok")

    asyncio.run(request_id_middleware(Request(scope), call_next))
    assert seen == {"token": key, "query_string": b""}


def test_claim_lease_retry_reuses_same_occurrence(scheduler_db):
    db = scheduler_db
    key = f"system:test_{uuid.uuid4().hex}"
    now = service.utcnow()
    db.add(ScheduledTask(
        task_key=key, owner_type="SYSTEM", task_name="test",
        schedule={"type": "interval", "minutes": 5}, enabled=True,
        next_run_at=now - timedelta(minutes=1),
    ))
    db.commit()
    try:
        first = [item for item in service.claim_due(now=now, max_tasks=1) if item["task_key"] == key]
        assert len(first) == 1
        assert first[0]["claim_count"] == 1
        first_id = first[0]["run_id"]

        retry = [item for item in service.claim_due(now=now + timedelta(seconds=601), max_tasks=1)
                 if item["task_key"] == key]
        assert len(retry) == 1
        assert retry[0]["run_id"] == first_id
        assert retry[0]["claim_count"] == 2

        db.expire_all()
        run = db.get(ScheduledTaskRun, first_id)
        assert run.status == "RUNNING"
        assert run.scheduled_for == first[0]["scheduled_for"]
        service._finish(first[0], "FAILED", now + timedelta(seconds=602), message="stale owner")
        db.expire_all()
        assert db.get(ScheduledTaskRun, first_id).status == "RUNNING"
        assert db.get(ScheduledTask, key).lease_token == retry[0]["token"]
        service._finish(retry[0], "SUCCESS", now + timedelta(seconds=602))
        db.expire_all()
        assert db.get(ScheduledTaskRun, first_id).status == "SUCCESS"
    finally:
        db.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.task_key == key))
        db.execute(delete(ScheduledTask).where(ScheduledTask.task_key == key))
        db.commit()


def test_plugin_task_fans_out_to_current_authorized_users_and_records_outcomes(
    plugin_env, client, auth, admin_user, plain_user, book, scheduler_db, monkeypatch
):
    db = scheduler_db
    build, restart = plugin_env
    pid = f"com.test.sched{uuid.uuid4().hex[:8]}"
    source = (
        "def register(registrar, ctx):\n"
        "    def nightly(task):\n"
        f"        ctx.book_write({book.id})\n"
        "        ctx.audit_write('scheduled_run')\n"
        "    registrar.task('nightly', nightly)\n"
    ).encode()
    edit = lambda manifest: manifest.update(
        api_version=">=1.2.0 <2.0.0", permissions=["audit:write", "book:write"],
        tasks=[{"name": "nightly", "schedule": {"type": "interval", "minutes": 60}}],
    )
    raw, _, _, _ = build(plugin_id=pid, tables=False, files={
        "backend/main.py": source,
        "frontend/index.js": b"export default host => ({render: () => host.h('div')});\n",
    }, edit=edit)
    try:
        installed = client.post("/api/system/plugins/install", files={"file": ("scheduled.cwp", raw)}, headers=auth)
        assert installed.status_code == 200, installed.text
        runtime = restart()
        granted = client.put(f"/api/system/plugins/{pid}/users", headers=auth,
                             json={"user_ids": [admin_user.id, plain_user.id]})
        assert granted.status_code == 200, granted.text

        from app.models.system import UserBook
        db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
        db.commit()
        # Enabling works before the task list was ever requested (autoflush is off).
        enabled = client.put(f"/api/system/scheduler/tasks/{pid}/nightly", headers=auth,
                             json={"enabled": True})
        assert enabled.status_code == 200, enabled.text
        tasks = client.get("/api/system/scheduler/tasks", headers=auth)
        task = next(row for row in tasks.json() if row["task_key"] == f"plugin:{pid}:nightly")
        assert task["enabled"] is True
        # A temporary worker barrier defers execution without disabling the task.
        with SessionLocal.begin() as session:
            session.get(ScheduledTask, task["task_key"]).next_run_at = service.utcnow() - timedelta(minutes=1)
        original_status = service.plugin_status
        with monkeypatch.context() as temporary:
            temporary.setattr(service, "plugin_status", lambda *args: ("UNAVAILABLE", "WORKER_BARRIER_PENDING"))
            with SessionLocal.begin() as session:
                assert service.sync_tasks(session, runtime, service.utcnow()) == set()
                assert session.get(ScheduledTask, task["task_key"]).enabled is True
            assert not any(item["plugin_id"] == pid for item in service.claim_due(runtime))
        assert service.plugin_status is original_status

        schedule, handler, digest = runtime.tasks[pid]["nightly"]
        observed = []

        def recording_handler(context):
            observed.append((context.user_id, context.idempotency_key, context.attempt))
            handler(context)

        runtime.tasks[pid]["nightly"] = (schedule, recording_handler, digest)
        now = service.utcnow()
        with SessionLocal.begin() as session:
            session.get(ScheduledTask, task["task_key"]).next_run_at = now - timedelta(minutes=1)
        claim = next(item for item in service.claim_due(runtime, now=now) if item["plugin_id"] == pid)
        run_id = claim["run_id"]
        service.execute_claim(claim, runtime)
        db.expire_all()
        run = db.get(ScheduledTaskRun, run_id)
        outcome = {"status": run.status, "user_results": run.user_results}
        assert outcome["status"] == "FAILED"
        assert {result["user_id"] for result in outcome["user_results"]} == {admin_user.id, plain_user.id}
        assert sum(result["status"] == "FAILED" for result in outcome["user_results"]) == 1
        assert {uid: key for uid, key, _attempt in observed} == {
            admin_user.id: f"{run_id}:{admin_user.id}",
            plain_user.id: f"{run_id}:{plain_user.id}",
        }
        assert set(db.scalars(select(AuditLog.user_id).where(
            AuditLog.action == "plugin.event", AuditLog.entity_id == pid
        ))) == {admin_user.id}

        claim["claim_count"] = 2
        service._run_plugin(runtime, pid, "nightly", recording_handler, claim)
        assert {(uid, key) for uid, key, _attempt in observed[2:]} == {
            (admin_user.id, f"{run_id}:{admin_user.id}"),
            (plain_user.id, f"{run_id}:{plain_user.id}"),
        }

        plain = db.get(SysUser, plain_user.id)
        plain.active = False
        db.commit()
        inactive = service._run_plugin(runtime, pid, "nightly", recording_handler, claim)
        assert [result["user_id"] for result in inactive["user_results"]] == [admin_user.id]

        plain.active = True
        db.commit()
        revoked = client.delete(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth)
        assert revoked.status_code == 200, revoked.text
        after_revoke = service._run_plugin(runtime, pid, "nightly", recording_handler, claim)
        assert [result["user_id"] for result in after_revoke["user_results"]] == [admin_user.id]

        client.delete(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth)
        no_users = service._run_plugin(runtime, pid, "nightly", recording_handler, claim)
        assert no_users["status"] == "SKIPPED"
        assert no_users["error_code"] == "NO_AUTHORIZED_USERS"
        assert no_users["user_results"] == []

        # New grants participate without enabling the task again.
        assert client.put(f"/api/system/plugins/{pid}/users", headers=auth,
                          json={"user_ids": [admin_user.id]}).status_code == 200
        added = service._run_plugin(runtime, pid, "nightly", recording_handler, claim)
        assert added["status"] == "SUCCESS"
        assert [item["user_id"] for item in added["user_results"]] == [admin_user.id]

        # An abandoned run from a different package is never replayed after upgrade.
        from app.models.plugin import PluginRegistry
        with SessionLocal.begin() as session:
            session.get(ScheduledTask, task["task_key"]).next_run_at = now
        abandoned = next(item for item in service.claim_due(runtime, now=now) if item["plugin_id"] == pid)
        with SessionLocal.begin() as session:
            session.get(PluginRegistry, pid).plugin_digest = "f" * 64
        assert not any(item["plugin_id"] == pid for item in service.claim_due(
            runtime, now=now + timedelta(seconds=service.LEASE_SECONDS + 1)))
        db.expire_all()
        assert db.get(ScheduledTask, task["task_key"]).enabled is False
        assert db.get(ScheduledTaskRun, abandoned["run_id"]).status == "SKIPPED"

    finally:
        with SessionLocal.begin() as cleanup:
            cleanup.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.plugin_id == pid))
            cleanup.execute(delete(ScheduledTask).where(ScheduledTask.plugin_id == pid))


def test_declared_default_enabled_starts_the_task_but_never_survives_an_upgrade(
    plugin_env, client, auth, book, scheduler_db
):
    """插件声明 ``enabled_default: true`` 时，装好就是开的。

    但**升级**后必须重新变成关闭的：换包等于换了代码，上一次的开启动作
    是针对旧包做的决定，不能悄悄延续到新包上 —— 否则管理员装新版时，
    一个自己都没看过的新包会立刻开始自动跑。
    """
    db = scheduler_db
    build, restart = plugin_env
    pid = f"com.test.deflt{uuid.uuid4().hex[:8]}"
    source = (
        "def register(registrar, ctx):\n"
        "    def nightly(task):\n"
        f"        ctx.book_write({book.id})\n"
        "        ctx.audit_write('scheduled_run')\n"
        "    registrar.task('nightly', nightly)\n"
    ).encode()
    edit = lambda manifest: manifest.update(
        api_version=">=1.2.0 <2.0.0", permissions=["audit:write", "book:write"],
        tasks=[{"name": "nightly", "schedule": {"type": "interval", "minutes": 60},
                "enabled_default": True}],
    )
    raw, _, _, _ = build(plugin_id=pid, tables=False, files={
        "backend/main.py": source,
        "frontend/index.js": b"export default host => ({render: () => host.h('div')});\n",
    }, edit=edit)
    try:
        assert client.post("/api/system/plugins/install", files={"file": ("scheduled.cwp", raw)},
                           headers=auth).status_code == 200
        runtime = restart()
        # Never touched the switch: the declaration alone has to do it.
        task = next(row for row in client.get("/api/system/scheduler/tasks", headers=auth).json()
                    if row["task_key"] == f"plugin:{pid}:nightly")
        assert task["enabled"] is True
        assert task["available"] is True
        assert task["unavailable_reason"] == ""

        # Switching it off, then syncing again, must not re-open it behind the admin's back.
        assert client.put(f"/api/system/scheduler/tasks/{pid}/nightly", headers=auth,
                          json={"enabled": False}).status_code == 200
        task = next(row for row in client.get("/api/system/scheduler/tasks", headers=auth).json()
                    if row["task_key"] == f"plugin:{pid}:nightly")
        assert task["enabled"] is False

        from app.models.plugin import PluginRegistry
        with SessionLocal.begin() as session:
            session.get(ScheduledTask, task["task_key"]).enabled = True
            session.get(PluginRegistry, pid).plugin_digest = "a" * 64
        # A package change (upgrade or rollback) forces a fresh enable decision.
        with SessionLocal.begin() as session:
            service.sync_tasks(session, runtime, service.utcnow())
        db.expire_all()
        assert db.get(ScheduledTask, task["task_key"]).enabled is False
    finally:
        with SessionLocal.begin() as cleanup:
            cleanup.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.plugin_id == pid))
            cleanup.execute(delete(ScheduledTask).where(ScheduledTask.plugin_id == pid))


def test_task_list_explains_why_a_switch_is_unavailable(plugin_env, client, auth, book, scheduler_db):
    """开关点不动时要能在列表里看到原因，而不是让人猜。"""
    db = scheduler_db
    build, restart = plugin_env
    pid = f"com.test.block{uuid.uuid4().hex[:8]}"
    source = (
        "def register(registrar, ctx):\n"
        "    def nightly(task):\n"
        f"        ctx.book_write({book.id})\n"
        "        ctx.audit_write('scheduled_run')\n"
        "    registrar.task('nightly', nightly)\n"
    ).encode()
    edit = lambda manifest: manifest.update(
        api_version=">=1.2.0 <2.0.0", permissions=["audit:write", "book:write"],
        tasks=[{"name": "nightly", "schedule": {"type": "interval", "minutes": 60}}],
    )
    raw, _, _, _ = build(plugin_id=pid, tables=False, files={
        "backend/main.py": source,
        "frontend/index.js": b"export default host => ({render: () => host.h('div')});\n",
    }, edit=edit)
    try:
        assert client.post("/api/system/plugins/install", files={"file": ("scheduled.cwp", raw)},
                           headers=auth).status_code == 200
        restart()
        tasks = client.get("/api/system/scheduler/tasks", headers=auth).json()
        task = next(row for row in tasks if row["task_key"] == f"plugin:{pid}:nightly")
        # No declaration: the task still starts closed.
        assert task["enabled"] is False
        assert task["available"] is True
        # System tasks are never switchable here.
        system = next(row for row in tasks if row["owner_type"] == "SYSTEM")
        assert system["available"] is False
        assert "系统任务" in system["unavailable_reason"]

        # Disabling the plugin itself is reported instead of a bare 409.
        from app.models.plugin import PluginRegistry
        with SessionLocal.begin() as session:
            session.get(PluginRegistry, pid).desired_enabled = False
        blocked = next(row for row in client.get("/api/system/scheduler/tasks", headers=auth).json()
                       if row["task_key"] == f"plugin:{pid}:nightly")
        assert blocked["available"] is False
        assert "扩展管理" in blocked["unavailable_reason"]
    finally:
        with SessionLocal.begin() as cleanup:
            cleanup.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.plugin_id == pid))
            cleanup.execute(delete(ScheduledTask).where(ScheduledTask.plugin_id == pid))


def test_nginx_disables_access_logging_for_secret_bearing_tick():
    root = Path(__file__).resolve().parents[2]
    for config in (root / "deploy/nginx/cwq.conf", root / "deploy/nginx/acct.xins.live.conf"):
        source = config.read_text(encoding="utf-8")
        assert "location ~ ^/api/scheduler/tick/?$" in source
        location = source.split("location ~ ^/api/scheduler/tick/?$", 1)[1].split("location /api/", 1)[0]
        assert "access_log off;" in location


def test_secret_rotation_admin_boundary_and_redaction(client, auth, plain_auth, scheduler_db, monkeypatch, caplog):
    monkeypatch.setattr(service, "claim_due", lambda _runtime: [])
    endpoints = [
        ("get", "/api/system/scheduler/tasks", {}),
        ("get", "/api/system/scheduler/runs", {}),
        ("put", "/api/system/scheduler/tasks/com.example.any/task", {"json": {"enabled": True}}),
        ("post", "/api/system/scheduler/secret/rotate", {}),
        ("delete", "/api/system/scheduler/secret/previous", {}),
    ]
    for method, path, kwargs in endpoints:
        assert getattr(client, method)(path, headers=plain_auth, **kwargs).status_code == 403
    first_response = client.post("/api/system/scheduler/secret/rotate", headers=auth)
    assert first_response.status_code == 200, first_response.text
    assert first_response.headers["cache-control"] == "no-store"
    first = first_response.json()["secret"]
    second = client.post("/api/system/scheduler/secret/rotate", headers=auth).json()["secret"]
    scheduler_db.expire_all()
    assert service.trigger_secret_digests(scheduler_db) == (service.secret_digest(second), service.secret_digest(first))
    for secret in (first, second):
        for suffix in ("", "/"):
            response = client.get(f"/api/scheduler/tick{suffix}", params={"key": secret})
            assert response.status_code == 202
            assert response.headers["cache-control"] == "no-store"
            assert secret not in response.text
    retired = client.delete("/api/system/scheduler/secret/previous", headers=auth)
    assert retired.status_code == 200
    assert client.get("/api/scheduler/tick", params={"key": first}).status_code == 401
    assert client.get("/api/scheduler/tick", params={"key": second}).status_code == 202
    # Audit details and settings never persist either plaintext credential.
    scheduler_db.expire_all()
    audit = list(scheduler_db.scalars(select(AuditLog).where(AuditLog.action.like("SCHEDULER_%"))))
    stored = repr([(item.detail, item.before, item.after) for item in audit])
    stored += repr(list(scheduler_db.execute(select(SystemSetting.key, SystemSetting.value))))
    assert first not in stored and second not in stored
    # httpx's client-side request log is outside the server redaction boundary.
    server_logs = "\n".join(record.getMessage() for record in caplog.records if not record.name.startswith("httpx"))
    assert first not in server_logs and second not in server_logs


def test_concurrent_first_ticks_discover_and_claim_only_once(scheduler_db, monkeypatch):
    monkeypatch.setattr(service, "SYSTEM_TASKS", {
        "test_concurrent": {"schedule": {"type": "cron", "expression": "* * * * *"}},
    })
    now = service.utcnow()
    barrier = threading.Barrier(2)

    def tick():
        barrier.wait(timeout=10)
        return service.claim_due(now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(tick) for _ in range(2)]
        claims = [claim for future in futures for claim in future.result(timeout=20)]
    assert len(claims) == 1
    assert claims[0]["task_key"] == "system:test_concurrent"
    assert len(list(scheduler_db.scalars(select(ScheduledTask)))) == 1
    assert len(list(scheduler_db.scalars(select(ScheduledTaskRun)))) == 1


def test_backup_switch_stops_expired_run_replay(scheduler_db):
    now = service.utcnow()
    key = "system:oss_backup"
    scheduler_db.add(ScheduledTask(
        task_key=key, owner_type="SYSTEM", task_name="oss_backup",
        schedule=service.SYSTEM_TASKS["oss_backup"]["schedule"], enabled=True,
        next_run_at=now - timedelta(minutes=1),
    ))
    system_settings.set_value(scheduler_db, "backup_enabled", "1")
    scheduler_db.commit()
    claim = next(item for item in service.claim_due(now=now) if item["task_key"] == key)
    system_settings.set_value(scheduler_db, "backup_enabled", "0")
    scheduler_db.commit()
    after_expiry = now + timedelta(seconds=service.LEASE_SECONDS + 1)
    assert not any(item["task_key"] == key for item in service.claim_due(now=after_expiry))
    scheduler_db.expire_all()
    assert scheduler_db.get(ScheduledTask, key).enabled is False
    run = scheduler_db.get(ScheduledTaskRun, claim["run_id"])
    assert run.status == "SKIPPED" and run.error_code == "TASK_UNAVAILABLE"


def test_system_tasks_use_existing_services_and_report_failures(scheduler_db, monkeypatch):
    calls = []
    monkeypatch.setattr(service.backup_service, "run_backup", lambda db, actor: (
        calls.append(actor.username) or SimpleNamespace(failed=[], prune_error=None)
    ))
    service._run_system("oss_backup")
    assert calls == ["scheduler"]
    monkeypatch.setattr(service, "purge_expired", lambda db: calls.append("cleanup"))
    service._run_system("plugin_import_cleanup")
    assert calls == ["scheduler", "cleanup"]
    monkeypatch.setattr(service.backup_service, "run_backup", lambda *args, **kwargs: SimpleNamespace(
        failed=[1], prune_error=None))
    with pytest.raises(RuntimeError, match="备份失败 1 项"):
        service._run_system("oss_backup")


def test_reenabling_cron_in_same_minute_keeps_completed_occurrence(scheduler_db, monkeypatch):
    monkeypatch.setattr(service, "SYSTEM_TASKS", {
        "test_reenable": {"schedule": {"type": "cron", "expression": "* * * * *"}},
    })
    now = service.utcnow()
    claim = service.claim_due(now=now)[0]
    service._finish(claim, "SUCCESS", now)
    scheduler_db.expire_all()
    row = scheduler_db.get(ScheduledTask, claim["task_key"])
    # An administrator re-enabling cron in the same minute resets to that minute.
    row.next_run_at = service._next(row.schedule, now, include_current_minute=True)
    scheduler_db.commit()
    assert service.claim_due(now=now) == []
    scheduler_db.expire_all()
    assert scheduler_db.get(ScheduledTask, claim["task_key"]).next_run_at > now
    runs = list(scheduler_db.scalars(select(ScheduledTaskRun)))
    assert len(runs) == 1 and runs[0].status == "SUCCESS"


# ------------------------------------------------------------------ 失败告警的接线


def _拦下告警(monkeypatch):
    """把通知派发换成同步记录器。

    拦在 ``dispatch_async`` 这一层：事件函数（``task_failed`` /
    ``backup_failed``）自己会调它，而那一层是**同步**的 —— 于是断言是
    确定的，不依赖守护线程跑到哪一步（跑线程是 flaky 事）。
    """
    from app.notify import events as notify_events

    发出的 = []
    monkeypatch.setattr(notify_events, "dispatch_async",
                        lambda event, subject, body: 发出的.append((event, subject, body)))
    return 发出的


def _建到期任务(db, task_key, task_name):
    now = service.utcnow()
    db.add(ScheduledTask(
        task_key=task_key, owner_type="SYSTEM", task_name=task_name,
        schedule={"type": "cron", "expression": "47 3 * * *"}, enabled=True,
        next_run_at=now - timedelta(minutes=1),
    ))
    db.commit()
    return now


def test_任务失败会发告警邮件(scheduler_db, monkeypatch):
    """**接线**的证据：任务失败 → 真的发出一封告警邮件。

    ``test_notify.py`` 测的是事件函数**本身**；这里测的是**调用点** ——
    ``scheduler/service.py::_notify_failure`` 是不是真的在任务失败时被调到。

    少了这条，将来有人重构 ``execute_claim`` 把那两行删掉，通知就会
    **静默消失**：任务失败照旧记进 run 记录，界面照旧看得到，但管理员
    再也收不到信 —— 而 ``test_notify.py`` 照样全绿（它测的是那个函数，
    不是谁在调它）。
    """
    发出的 = _拦下告警(monkeypatch)

    def _炸(name, **kwargs):
        raise RuntimeError("任务炸了")

    monkeypatch.setattr(service, "_run_system", _炸)
    now = _建到期任务(scheduler_db, "system:custom_task", "custom_task")

    claim = next(item for item in service.claim_due(now=now)
                 if item["task_key"] == "system:custom_task")
    service.execute_claim(claim)

    assert len(发出的) == 1, f"任务失败了却没有告警：{发出的}"
    event, subject, body = 发出的[0]
    assert event == "TASK_FAILED"
    assert "system:custom_task" in subject
    assert "任务炸了" in body
    # run 记录仍然是失败的 —— 两条通道各司其职：审计是证据，邮件是检测
    scheduler_db.expire_all()
    run = scheduler_db.get(ScheduledTaskRun, claim["run_id"])
    assert run.status == "FAILED"


def test_备份任务失败发的是备份告警(scheduler_db, monkeypatch):
    """备份失败走**另一个事件类型**（BACKUP_FAILED）。

    分流不是洁癖：备份失败时该说的那句是「备份是出事后的退路」，而对
    插件任务说这句话是错的。分开还让两条告警**不共用限流桶** —— 锁定
    告警刷屏不该把备份告警一起压掉。
    """
    发出的 = _拦下告警(monkeypatch)

    def _炸(name, **kwargs):
        raise RuntimeError("AccessKey 无效")

    monkeypatch.setattr(service, "_run_system", _炸)
    system_settings.set_value(scheduler_db, "backup_enabled", "1", updated_by="test")
    scheduler_db.commit()
    now = _建到期任务(scheduler_db, "system:oss_backup", "oss_backup")

    claim = next(item for item in service.claim_due(now=now)
                 if item["task_key"] == "system:oss_backup")
    service.execute_claim(claim)

    assert len(发出的) == 1, f"备份失败了却没有告警：{发出的}"
    event, subject, body = 发出的[0]
    assert event == "BACKUP_FAILED"
    assert "AccessKey 无效" in body
    assert "退路" in body, f"备份告警里没有点明它是退路：{body!r}"
