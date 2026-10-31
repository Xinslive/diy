"""应用级计划任务、租约领取与运行记录。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import and_, func, or_, select, text, update

from app.accounting.audit_helper import log as write_audit
from app.backup import service as backup_service
from app.core import system_settings
from app.core.db import SessionLocal
from app.maintenance import prune_log_tables
from app.models import (SCOPE_SYSTEM, STATUS_FAILED, TIER_DAILY, AuditLog, BackupRun,
                        ScheduledTask, ScheduledTaskRun)
from app.models.plugin import PluginRegistry, PluginUserAccess
from app.models.system import SysUser
from app.plugins.imports import purge_expired
from app.plugins.workers import status as plugin_status

log = logging.getLogger(__name__)
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
LEASE_SECONDS = 600
#: 租约续期的间隔（秒）。**必须显著小于租约本身** —— 续期失败到租约过期
#: 之间留出的时间，就是「另一个 worker 接管而前一个还在跑」的窗口。
LEASE_RENEW_SECONDS = 60
#: 运行记录的保留期**不在这里** —— 它和其余日志类表的保留期一起放在
#: ``app/maintenance.py``，那才是「全系统留多久」的唯一真相。
SCHEDULER_LOCK = 0x4357515343484544
SYSTEM_TASKS = {
    "oss_backup": {"schedule": {"type": "cron", "expression": "47 3 * * *"}},
    "plugin_import_cleanup": {"schedule": {"type": "interval", "minutes": 60}},
}

#: 系统任务幂等标记的审计动作名。
#:
#: **为什么落在审计日志上，而不是 ``sys_scheduled_task_run``。**
#: 那一行在**接管**时会被重置（``_claim`` 把 status/finished_at/error_code/
#: message 清成「重新开始」的样子），所以它记不住「上一次的副作用到底做没做」。
#: 审计行不会被重置，也不会被清理 —— 审计是留痕，不是缓存。
SYSTEM_TASK_MARK_ACTION = "SCHEDULER_SYSTEM_TASK"


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


def task_default_enabled(plugin, name):
    """插件是否声明了「装好就开」。

    manifest 的任务项可以写 ``"enabled_default": true``，表示这个任务应该随插件
    一起启用，不必让管理员装完插件再去另一个页面上找开关。默认是 False。

    只影响**新建**任务行。已有行的 enabled 是管理员的决定或上一次声明的结果，
    同步过程不得改写它 —— 否则管理员手动关掉一个任务，下次任意一次 tick 都会
    把它悄悄打开。
    """
    tasks = (getattr(plugin, "manifest_metadata", None) or {}).get("tasks") or []
    for task in tasks:
        if isinstance(task, dict) and task.get("name") == name:
            return task.get("enabled_default") is True
    return False


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
                                     task_name=name, plugin_digest=digest, schedule=schedule,
                                     enabled=task_default_enabled(plugin, name),
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
        # **日志类表的清理在这里顺手做。**
        #
        # 为什么放在领取任务这一步：它是系统里唯一的周期性入口 —— 再挂一个常驻
        # 清理线程就是第二套调度器（见 ARCHITECTURE §11.13「宿主不起自己的常驻
        # 调度循环」）；而 tick 停了会有人知道（cwq-scheduler-watchdog 专盯这
        # 件事），于是「清理停摆」这件事也是会被发现的。
        #
        # 保留期全部在 app/maintenance.py 里，这里不写任何数字。
        prune_log_tables(db, now=now)
    return claimed


def _renew_lease(claim):
    with SessionLocal.begin() as db:
        result = db.execute(update(ScheduledTask).where(
            ScheduledTask.task_key == claim["task_key"], ScheduledTask.lease_token == claim["token"]
        ).values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
        return result.rowcount == 1


def system_idempotency_key(claim) -> str:
    """一次触发的幂等键：``{task_key}:{scheduled_for}``。

    **为什么需要它。** 租约（600 秒）由后台线程每 60 秒续期。续期失败
    （DB 抖动、GC 长暂停、进程被 SIGSTOP）之后租约过期，**另一个 worker 会
    接管同一个 ``run_id``** —— 而前一个 worker 可能还在跑它的副作用。
    ``_finish`` 用 ``row.lease_token != claim["token"]`` 挡住了**结果写入**，
    但**副作用本身**没有被挡住：备份会上传两次、清理会跑两次。

    所以幂等键必须是「**同一个触发点**」的身份，而不是「同一个进程」的。
    ``scheduled_for`` 就是它 —— 触发点一旦过去就不会重来。
    """
    scheduled = claim.get("scheduled_for")
    stamp = scheduled.isoformat() if hasattr(scheduled, "isoformat") else str(scheduled)
    return f"{claim.get('task_key') or ''}:{stamp}"


def _marker_id(key: str) -> str:
    """幂等键 → 审计行 id（``audit_log.entity_id`` 只有 64 个字符）。"""
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def claim_system_task(db, key: str) -> bool:
    """占住这个触发点。**True = 已经有人动过手**，本次应当空转。

    标记写在**副作用开始之前** —— 这是它能挡住重复执行的原因：两个 worker
    同时接管时，只有一个能拿到咨询锁并写下标记，另一个看到标记就空转。

    代价要说明白：第一次执行**中途失败或被中止**时，标记仍然在，接管者
    不会重跑这个触发点（宁可少做一次，也不要重复上传/重复清理）。下一个
    周期是新的 ``scheduled_for``、新的幂等键，会正常执行。

    用**事务级咨询锁**把「查 + 写」做成原子的 —— 否则两个 worker 同时
    接管时可能都查不到标记、都去执行副作用。
    """
    marker = _marker_id(key)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": int(marker[:15], 16)})
    existing = db.scalar(select(AuditLog.id).where(
        AuditLog.action == SYSTEM_TASK_MARK_ACTION,
        AuditLog.entity == "scheduled_task_run",
        AuditLog.entity_id == marker))
    if existing is not None:
        return True
    write_audit(db, action=SYSTEM_TASK_MARK_ACTION, entity="scheduled_task_run",
                entity_id=marker, user=SimpleNamespace(id=None, username="scheduler"),
                after={"idempotency_key": key})
    db.commit()
    return False


def _run_system(name, *, idempotency_key="", lease=None):
    db = SessionLocal()
    actor = SimpleNamespace(id=None, username="scheduler")
    try:
        # **租约先判**：租约已经丢了就轮不到「占坑」—— 否则白占一个触发点，
        # 让接管者也跑不成。
        if lease is not None and lease.is_set():
            log.warning("scheduled system task abandoned (lease lost) name=%s", name)
            return {"status": "SKIPPED", "error_code": "LEASE_LOST",
                    "message": "本任务的租约已丢失（已被其它 worker 接管），本次不再执行"}
        if idempotency_key and claim_system_task(db, idempotency_key):
            log.info("scheduled system task skipped (already started) key=%s", idempotency_key)
            return {"status": "SKIPPED", "error_code": "ALREADY_STARTED",
                    "message": f"该触发点已经由另一次执行开始（幂等键 {idempotency_key}），"
                               "为避免重复执行，本次未再运行"}
        if name == "oss_backup":
            try:
                if lease is None:
                    # 直接调用（没有租约上下文）时保持**原来的调用形状** ——
                    # 少传一个关键字，替身与既有调用方都不必改。
                    result = backup_service.run_backup(db, actor=actor)
                else:
                    result = backup_service.run_backup(
                        db, actor=actor, should_stop=lambda: lease.is_set())
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
            stopped = getattr(result, "stopped", 0)
            if stopped:
                # **提前停止不是成功**：还有目标没备份。记成失败，让运行记录
                # 与告警邮件都看得见（而不是「看起来成功了」的假象）。
                raise RuntimeError(
                    f"备份轮次提前停止：还有 {stopped} 个目标未备份（租约已丢失，"
                    "本次由接管者接手；未备份的目标等下一个周期）")
        elif name == "plugin_import_cleanup":
            purge_expired(db)
            db.commit()
        else:
            raise RuntimeError("未知系统任务")
        return {"status": "SUCCESS"}
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


def _run_plugin(runtime, pid, name, handler, claim, *, lease=None):
    user_ids = _plugin_task_users(pid, claim["plugin_digest"], runtime)
    if not user_ids:
        return {"status": "SKIPPED", "error_code": "NO_AUTHORIZED_USERS",
                "message": "没有启用且仍获插件授权的用户", "user_results": []}

    from app.plugins.access import require_plugin_access
    from app.plugins.public_api import current_principal, current_request_id, principal_scope

    user_results = []
    for user_id in user_ids:
        result = {"user_id": user_id, "status": "SUCCESS", "error_code": "", "message": ""}
        try:
            # 租约丢了就**放弃本次执行**，把活儿让给接管者（尽力停止）。
            if lease is not None and lease.is_set():
                result.update(status="SKIPPED", error_code="LEASE_LOST",
                              message="租约已丢失（已被其它 worker 接管），本次未执行")
                user_results.append(result)
                continue
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
                # 「解析调用者」在这一次任务执行里**只做一次**。
                # 上面那道闸已经校验过一遍；任务里再逐次解析是重复。
                with principal_scope():
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
    if not active and any(item.get("error_code") == "LEASE_LOST" for item in user_results):
        return {"status": "SKIPPED", "error_code": "LEASE_LOST",
                "message": "租约已丢失（已被其它 worker 接管），本次未执行",
                "user_results": user_results}
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


def _notify_failure(claim, error_code: str, message: str) -> None:
    """任务失败时发一封告警邮件。

    **为什么不直接在失败分支里写邮件代码**：备份任务失败时，说服力最强
    的那句话是「备份是出事后的退路」，而对插件任务来说这句话是错的。
    两种失败共用一条出口，所以在这里按任务种类分流。

    **为什么用 async 版本**：这里本身就在后台线程里，同步发其实也不影响
    任何请求 —— 但统一走 async 有另一个好处：**同一条路径、同一套兜底**。
    邮件发不出去时 ``notify.events.dispatch`` 自己吞掉并记日志，绝不把
    「邮件服务器挂了」变成「任务被判成失败之外还多了一个异常」。
    """
    from app.notify import events as notify_events

    task_key = claim.get("task_key") or ""
    if task_key.startswith("system:oss_backup"):
        notify_events.backup_failed(scope="账套备份（每日 OSS）",
                                    message=f"[{error_code}] {message}")
    else:
        notify_events.task_failed(task_key=task_key, error_code=error_code,
                                  message=message)


def execute_claim(claim, runtime=None):
    if not _renew_lease(claim):
        return
    stop = threading.Event()
    #: 置位 = **租约已经丢了**（被别人接管，或续期失败到租约过期）。
    #:
    #: 以前续期线程发现续不上只是 ``return``，主任务**照跑不误** —— 于是两个
    #: worker 会同时对同一个触发点跑副作用。现在主任务看到它就**放弃本次
    #: 执行**，把活儿让给接管者。
    #:
    #: 这是**尽力停止**：主任务是一个同步函数，Python 里没法安全地把它打断，
    #: 所以只能在**安全点**上检查（副作用开始前、逐个目标之间）。最终不重复
    #: 靠的是幂等键（``system_idempotency_key`` + ``claim_system_task``）——
    #: 那才是兜底，本条只是让「重复」尽量不发生。
    lease_lost = threading.Event()

    def renew():
        last_ok = time.monotonic()
        while not stop.wait(LEASE_RENEW_SECONDS):
            try:
                if not _renew_lease(claim):
                    # 租约令牌已经变了 —— 有人接管了。**立刻**通知主任务。
                    lease_lost.set()
                    log.warning("scheduled task lease taken over key=%s run_id=%s",
                                claim["task_key"], claim["run_id"])
                    return
                last_ok = time.monotonic()
            except Exception:
                log.exception("scheduled task lease renewal failed key=%s", claim["task_key"])
                if time.monotonic() - last_ok > LEASE_SECONDS:
                    # 续期一直失败 ⇒ 租约早已过期 ⇒ 别人随时可能接管。
                    lease_lost.set()
                    log.warning("scheduled task lease renewal expired key=%s run_id=%s",
                                claim["task_key"], claim["run_id"])
                    return

    heartbeat = threading.Thread(target=renew, name="scheduled-task-lease", daemon=True)
    heartbeat.start()
    try:
        if claim["owner_type"] == "SYSTEM":
            outcome = _run_system(claim["task_name"],
                                  idempotency_key=system_idempotency_key(claim),
                                  lease=lease_lost)
        else:
            task = runtime.tasks[claim["plugin_id"]][claim["task_name"]]
            outcome = _run_plugin(runtime, claim["plugin_id"], claim["task_name"], task[1], claim,
                                  lease=lease_lost)
        _finish(claim, outcome["status"], utcnow(), error_code=outcome.get("error_code", ""),
                message=outcome.get("message", ""), user_results=outcome.get("user_results"))
        if outcome["status"] == "SKIPPED":
            log.warning("scheduled plugin task skipped key=%s run_id=%s code=%s",
                        claim["task_key"], claim["run_id"], outcome.get("error_code", ""))
        if outcome["status"] == "FAILED":
            # 任务失败是**要有人知道**的：它跑在凌晨，界面上没人盯着，
            # 发现延迟等于「到第二天有人问为什么昨天的数据没同步」。
            _notify_failure(claim, outcome.get("error_code", ""),
                            outcome.get("message", ""))
    except Exception as exc:
        log.exception("scheduled task failed key=%s run_id=%s", claim["task_key"], claim["run_id"])
        _finish(claim, "FAILED", utcnow(), error_code=type(exc).__name__, message=str(exc))
        _notify_failure(claim, type(exc).__name__, str(exc))
    finally:
        stop.set()
        heartbeat.join(timeout=2)


async def execute_claims(claims, runtime=None):
    from starlette.concurrency import run_in_threadpool

    await asyncio.gather(*(run_in_threadpool(execute_claim, claim, runtime) for claim in claims))
