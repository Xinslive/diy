"""应用级计划任务、租约领取与运行记录。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import and_, delete, func, or_, select, update

from app.backup import service as backup_service
from app.core import system_settings
from app.core.db import SessionLocal
from app.models import (SCOPE_SYSTEM, STATUS_FAILED, TIER_DAILY, BackupRun,
                        ScheduledTask, ScheduledTaskRun)
from app.models.plugin import PluginRegistry, PluginUserAccess
from app.models.system import SysUser
from app.plugins.imports import purge_expired
from app.plugins.workers import status as plugin_status

log = logging.getLogger(__name__)
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
LEASE_SECONDS = 600
RUN_RETENTION_DAYS = 90
SCHEDULER_LOCK = 0x4357515343484544
SYSTEM_TASKS = {
    "oss_backup": {"schedule": {"type": "cron", "expression": "47 3 * * *"}},
    "plugin_import_cleanup": {"schedule": {"type": "interval", "minutes": 60}},
}


def utcnow():
    return datetime.now(timezone.utc)


def task_key(plugin_id, name):
    return f"plugin:{plugin_id}:{name}" if plugin_id else f"system:{name}"


def _next(schedule, after, *, include_current_minute=False):
    if schedule["type"] == "interval":
        return after + timedelta(minutes=schedule["minutes"])
    local = after.astimezone(BUSINESS_TZ)
    if include_current_minute:
        local = local.replace(second=0, microsecond=0) - timedelta(minutes=1)
    return croniter(schedule["expression"], local).get_next(datetime).astimezone(timezone.utc)


def _next_after_claim(schedule, scheduled_for, now):
    """跳过本次触发前错过的周期，但不让延迟执行造成持续补跑。"""
    if schedule["type"] == "interval":
        interval = timedelta(minutes=schedule["minutes"])
        elapsed = max(now - scheduled_for, timedelta())
        periods = int(elapsed // interval) + 1
        return scheduled_for + periods * interval
    return _next(schedule, now)


def trigger_secret_digests(db):
    current = system_settings.get_value(db, "scheduler_trigger_secret", "")
    previous = system_settings.get_value(db, "scheduler_trigger_secret_previous", "")
    return current, previous


def rotate_trigger_secret(db, *, updated_by):
    current, _previous = trigger_secret_digests(db)
    value = secrets.token_urlsafe(32)
    system_settings.set_value(db, "scheduler_trigger_secret_previous", current, updated_by=updated_by)
    system_settings.set_value(db, "scheduler_trigger_secret", secret_digest(value), updated_by=updated_by)
    return value


def valid_trigger(value, expected):
    if not value:
        return False
    digest = secret_digest(value)
    return any(candidate and hmac.compare_digest(digest, candidate) for candidate in expected)


def clear_previous_trigger_secret(db, *, updated_by):
    system_settings.set_value(db, "scheduler_trigger_secret_previous", "", updated_by=updated_by)


def secret_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def lock_scheduler_sync(db):
    """Serialize task discovery so concurrent ticks cannot race on new task rows."""
    db.execute(select(func.pg_advisory_xact_lock(SCHEDULER_LOCK)))


def sync_system_tasks(db, now):
    from app.backup.config import is_enabled

    for name, spec in SYSTEM_TASKS.items():
        key = task_key(None, name)
        enabled = is_enabled(db) if name == "oss_backup" else True
        row = db.get(ScheduledTask, key)
        if row is None:
            db.add(ScheduledTask(task_key=key, owner_type="SYSTEM", task_name=name,
                                 schedule=spec["schedule"], enabled=enabled,
                                 next_run_at=_next(spec["schedule"], now, include_current_minute=True)))
        else:
            changed = row.schedule != spec["schedule"] or row.enabled != enabled
            row.schedule, row.enabled = spec["schedule"], enabled
            if changed and enabled:
                row.next_run_at = _next(spec["schedule"], now, include_current_minute=True)


def sync_plugin_tasks(db, runtime, now):
    persisted = list(db.scalars(select(ScheduledTask).where(ScheduledTask.owner_type == "PLUGIN")))
    rows_by_plugin = {}
    for row in persisted:
        rows_by_plugin.setdefault(row.plugin_id, {})[row.task_name] = row

    runtime_tasks = getattr(runtime, "tasks", {}) if runtime is not None else {}
    ready_keys = set()
    for pid in set(rows_by_plugin) | set(runtime_tasks):
        plugin = db.get(PluginRegistry, pid)
        existing = rows_by_plugin.get(pid, {})
        if not plugin or plugin.deleted_at is not None or not plugin.desired_enabled:
            for row in existing.values():
                row.enabled = False
            continue
        # A restart/barrier delay must not erase an administrator's enabled setting.
        # A different package always needs a fresh enable decision, even before READY.
        for row in existing.values():
            if row.plugin_digest != plugin.plugin_digest:
                row.enabled = False
        if runtime is None or plugin_status(db, plugin, runtime.generation)[0] != "READY":
            continue
        declared = runtime_tasks.get(pid, {})
        for name, row in existing.items():
            if name not in declared:
                row.enabled = False
        for name, (schedule, _handler, digest) in declared.items():
            key = task_key(pid, name)
            ready_keys.add(key)
            row = existing.get(name)
            if row is None:
                db.add(ScheduledTask(task_key=key, owner_type="PLUGIN", plugin_id=pid,
                                     task_name=name, plugin_digest=digest, schedule=schedule, enabled=False,
                                     next_run_at=_next(schedule, now, include_current_minute=True)))
            elif row.plugin_digest != digest or row.schedule != schedule:
                row.plugin_digest, row.schedule, row.enabled = digest, schedule, False
                row.next_run_at = _next(schedule, now, include_current_minute=True)
    return ready_keys


def sync_tasks(db, runtime, now):
    lock_scheduler_sync(db)
    sync_system_tasks(db, now)
    ready_keys = sync_plugin_tasks(db, runtime, now)
    # SessionLocal disables autoflush: discovery/disable changes must precede claims.
    db.flush()
    return ready_keys


def _claim(db, row, now):
    stale = bool(row.current_run_id and (row.lease_until is None or row.lease_until <= now))
    run = db.get(ScheduledTaskRun, row.current_run_id) if stale else None
    token = uuid.uuid4().hex

    if run is not None:
        run.claim_count += 1
        run.status, run.finished_at, run.error_code, run.message = "RUNNING", None, "", ""
        run.user_results = []
        scheduled_for = run.scheduled_for
    else:
        scheduled_for = row.next_run_at
        row.next_run_at = _next_after_claim(row.schedule, scheduled_for, now)
        # Re-enabling a cron task in the same minute must not repeat a finished
        # occurrence or violate its unique key (which would roll back other claims).
        if db.scalar(select(ScheduledTaskRun.run_id).where(
                ScheduledTaskRun.task_key == row.task_key,
                ScheduledTaskRun.scheduled_for == scheduled_for)):
            return None
        run_id = uuid.uuid4().hex
        run = ScheduledTaskRun(
            run_id=run_id, task_key=row.task_key, owner_type=row.owner_type,
            plugin_id=row.plugin_id, task_name=row.task_name, plugin_digest=row.plugin_digest,
            scheduled_for=scheduled_for, started_at=now, status="RUNNING", claim_count=1,
        )
        db.add(run)
        row.current_run_id = run_id

    row.lease_token = token
    row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    return {"task_key": row.task_key, "owner_type": row.owner_type, "plugin_id": row.plugin_id,
            "task_name": row.task_name, "plugin_digest": row.plugin_digest,
            "schedule": row.schedule, "token": token, "run_id": run.run_id,
            "scheduled_for": scheduled_for, "claim_count": run.claim_count}


def _skip_stale_run(db, row, runtime, now):
    if not row.current_run_id or (row.lease_until and row.lease_until > now):
        return False
    run = db.get(ScheduledTaskRun, row.current_run_id)
    if run is None:
        return False
    if row.enabled:
        if run.owner_type == "SYSTEM":
            return False
        tasks = getattr(runtime, "tasks", {}) if runtime is not None else {}
        task = tasks.get(row.plugin_id, {}).get(row.task_name)
        if task is not None and task[2] == run.plugin_digest == row.plugin_digest:
            return False
    run.status, run.finished_at = "SKIPPED", now
    run.error_code, run.message = "TASK_UNAVAILABLE", "任务已停用、移除或版本变化，未重放旧执行"
    row.last_run_at = now
    row.lease_token, row.lease_until, row.current_run_id = "", None, None
    log.warning("stale scheduled task skipped key=%s run_id=%s", row.task_key, run.run_id)
    return True


def claim_due(runtime=None, *, now=None, max_tasks=20):
    now = now or utcnow()
    claimed = []
    with SessionLocal.begin() as db:
        ready_keys = sync_tasks(db, runtime, now)
        runnable = or_(ScheduledTask.owner_type == "SYSTEM", ScheduledTask.task_key.in_(ready_keys))
        rows = list(db.scalars(select(ScheduledTask).where(
            or_(and_(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now, runnable),
                and_(ScheduledTask.current_run_id.is_not(None),
                     or_(runnable, ScheduledTask.enabled.is_(False)),
                     or_(ScheduledTask.lease_until.is_(None), ScheduledTask.lease_until <= now))),
            or_(ScheduledTask.lease_until.is_(None), ScheduledTask.lease_until <= now),
        ).order_by(ScheduledTask.next_run_at).with_for_update(skip_locked=True)
            .execution_options(populate_existing=True).limit(max_tasks)).all())
        for row in rows:
            if not _skip_stale_run(db, row, runtime, now):
                claim = _claim(db, row, now)
                if claim is not None:
                    claimed.append(claim)
        db.execute(delete(ScheduledTaskRun).where(
            ScheduledTaskRun.finished_at.is_not(None),
            ScheduledTaskRun.finished_at < now - timedelta(days=RUN_RETENTION_DAYS)))
    return claimed


def _renew_lease(claim):
    with SessionLocal.begin() as db:
        result = db.execute(update(ScheduledTask).where(
            ScheduledTask.task_key == claim["task_key"], ScheduledTask.lease_token == claim["token"]
        ).values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
        return result.rowcount == 1


def _run_system(name):
    db = SessionLocal()
    actor = SimpleNamespace(id=None, username="scheduler")
    try:
        if name == "oss_backup":
            try:
                result = backup_service.run_backup(db, actor=actor)
            except Exception as exc:
                db.rollback()
                db.add(BackupRun(
                    book_id=None, scope=SCOPE_SYSTEM, book_label="（整轮）", tier=TIER_DAILY,
                    status=STATUS_FAILED, message=f"调度备份异常：{type(exc).__name__}: {exc}",
                    started_at=utcnow(), finished_at=utcnow(),
                ))
                db.commit()
                raise
            failures = len(result.failed)
            if failures or result.prune_error:
                raise RuntimeError(f"备份失败 {failures} 项；清理异常：{result.prune_error or '无'}")
        elif name == "plugin_import_cleanup":
            purge_expired(db)
            db.commit()
        else:
            raise RuntimeError("未知系统任务")
    finally:
        db.close()


def _plugin_task_users(pid, plugin_digest, runtime):
    with SessionLocal() as db:
        plugin = db.get(PluginRegistry, pid)
        if (plugin is None or plugin.deleted_at is not None or not plugin.desired_enabled or
                plugin.plugin_digest != plugin_digest):
            raise RuntimeError("插件任务版本或启用状态已变化")
        current, code = plugin_status(db, plugin, runtime.generation)
        if current != "READY":
            raise RuntimeError(f"PLUGIN_{code or current}")
        return list(db.scalars(select(SysUser.id).join(
            PluginUserAccess, PluginUserAccess.user_id == SysUser.id
        ).where(PluginUserAccess.plugin_id == pid,
                PluginUserAccess.revoked_at.is_(None), SysUser.active.is_(True))
          .order_by(SysUser.id)))


def _run_plugin(runtime, pid, name, handler, claim):
    user_ids = _plugin_task_users(pid, claim["plugin_digest"], runtime)
    if not user_ids:
        return {"status": "SKIPPED", "error_code": "NO_AUTHORIZED_USERS",
                "message": "没有启用且仍获插件授权的用户", "user_results": []}

    from app.plugins.access import require_plugin_access
    from app.plugins.public_api import current_principal, current_request_id

    user_results = []
    for user_id in user_ids:
        result = {"user_id": user_id, "status": "SUCCESS", "error_code": "", "message": ""}
        try:
            with SessionLocal() as db:
                user = db.get(SysUser, user_id)
                grant = db.get(PluginUserAccess, (pid, user_id))
                if user is None or not user.active:
                    result.update(status="SKIPPED", error_code="USER_INACTIVE", message="用户已停用")
                    user_results.append(result)
                    continue
                if grant is None or grant.revoked_at is not None:
                    result.update(status="SKIPPED", error_code="ACCESS_REVOKED", message="插件授权已撤销")
                    user_results.append(result)
                    continue
                plugin = db.get(PluginRegistry, pid)
                current, code = plugin_status(db, plugin, runtime.generation) if plugin else ("UNAVAILABLE", "PLUGIN_MISSING")
                if (plugin is None or plugin.deleted_at is not None or not plugin.desired_enabled or
                        plugin.plugin_digest != claim["plugin_digest"] or current != "READY"):
                    raise RuntimeError(f"插件执行条件已变化：{code or current}")
                require_plugin_access(db, pid, user, generation=runtime.generation)

            idempotency_key = f"{claim['run_id']}:{user_id}"
            context = SimpleNamespace(
                plugin_id=pid, task_name=name, run_id=claim["run_id"],
                scheduled_for=claim["scheduled_for"], user_id=user_id,
                attempt=claim["claim_count"], idempotency_key=idempotency_key,
                scope="per_user",
            )
            principal_token = current_principal.set((pid, user_id, runtime.generation))
            request_token = current_request_id.set(idempotency_key)
            try:
                handler(context)
            finally:
                current_principal.reset(principal_token)
                current_request_id.reset(request_token)
        except Exception as exc:
            result.update(status="FAILED", error_code=type(exc).__name__[:64], message=str(exc)[:512])
            log.exception("scheduled plugin task failed key=%s run_id=%s user_id=%s",
                          claim["task_key"], claim["run_id"], user_id)
        user_results.append(result)

    failed = sum(item["status"] == "FAILED" for item in user_results)
    active = sum(item["status"] == "SUCCESS" for item in user_results)
    if failed:
        return {"status": "FAILED", "error_code": "PLUGIN_USER_TASK_FAILED",
                "message": f"{failed}/{len(user_results)} 个用户执行失败", "user_results": user_results}
    if not active:
        return {"status": "SKIPPED", "error_code": "NO_AUTHORIZED_USERS",
                "message": "执行时所有用户均已停用或撤销授权", "user_results": user_results}
    return {"status": "SUCCESS", "error_code": "", "message": "", "user_results": user_results}


def _finish(claim, status_value, now, *, error_code="", message="", user_results=None):
    with SessionLocal.begin() as db:
        row = db.scalar(select(ScheduledTask).where(
            ScheduledTask.task_key == claim["task_key"]).with_for_update())
        if not row or row.lease_token != claim["token"] or row.current_run_id != claim["run_id"]:
            return
        run = db.get(ScheduledTaskRun, claim["run_id"])
        if not run:
            return
        run.status, run.finished_at = status_value, now
        run.error_code, run.message = error_code[:64], message[:4000]
        run.user_results = user_results or []
        row.last_run_at = now
        row.lease_token, row.lease_until, row.current_run_id = "", None, None


def execute_claim(claim, runtime=None):
    if not _renew_lease(claim):
        return
    stop = threading.Event()

    def renew():
        while not stop.wait(60):
            try:
                if not _renew_lease(claim):
                    return
            except Exception:
                log.exception("scheduled task lease renewal failed key=%s", claim["task_key"])

    heartbeat = threading.Thread(target=renew, name="scheduled-task-lease", daemon=True)
    heartbeat.start()
    try:
        if claim["owner_type"] == "SYSTEM":
            _run_system(claim["task_name"])
            outcome = {"status": "SUCCESS"}
        else:
            task = runtime.tasks[claim["plugin_id"]][claim["task_name"]]
            outcome = _run_plugin(runtime, claim["plugin_id"], claim["task_name"], task[1], claim)
        _finish(claim, outcome["status"], utcnow(), error_code=outcome.get("error_code", ""),
                message=outcome.get("message", ""), user_results=outcome.get("user_results"))
        if outcome["status"] == "SKIPPED":
            log.warning("scheduled plugin task skipped key=%s run_id=%s code=%s",
                        claim["task_key"], claim["run_id"], outcome.get("error_code", ""))
    except Exception as exc:
        log.exception("scheduled task failed key=%s run_id=%s", claim["task_key"], claim["run_id"])
        _finish(claim, "FAILED", utcnow(), error_code=type(exc).__name__, message=str(exc))
    finally:
        stop.set()
        heartbeat.join(timeout=2)


async def execute_claims(claims, runtime=None):
    from starlette.concurrency import run_in_threadpool

    await asyncio.gather(*(run_in_threadpool(execute_claim, claim, runtime) for claim in claims))
