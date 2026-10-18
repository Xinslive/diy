"""数据库事实驱动的逐 worker barrier；没有请求间授权/READY 缓存。"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from app.core.config import settings
from app.core.db import SessionLocal
from app.plugins.errors import PluginError
from app.plugins.models import PluginCohort, PluginRegistry, PluginWorkerRuntime


def now():
    return datetime.now(timezone.utc)


def live_cutoff():
    return now() - timedelta(seconds=settings.plugin_lease_seconds)


def status(db, plugin: PluginRegistry, generation=None) -> tuple[str, str]:
    if plugin.deleted_at is not None:
        return "ORPHANED", "PLUGIN_REMOVED"
    if plugin.runtime_status == "ORPHANED":
        return "UNAVAILABLE", plugin.error_code or "PACKAGE_MISSING"
    cohort = db.get(PluginCohort, 1)
    if not settings.plugin_enabled or cohort is None:
        return "UNAVAILABLE", "PLATFORM_DISABLED"
    if generation is not None and generation != cohort.generation:
        return "UNAVAILABLE", "STALE_GENERATION"
    # 新 cohort 不能在旧进程仍持有有效租约时先开放，避免重启窗口交替 200/503。
    if db.scalar(select(PluginWorkerRuntime.worker_id).where(
        PluginWorkerRuntime.plugin_id == "", PluginWorkerRuntime.generation != cohort.generation,
        PluginWorkerRuntime.lease_at > live_cutoff()).limit(1)):
        return "UNAVAILABLE", "OLD_WORKERS_ACTIVE"
    # 本批次内的安装/开关变更必须等下一次 supervisor 重启，不能靠心跳热加载。
    if plugin.runtime_status == "PENDING_RESTART" and plugin.generation == cohort.generation:
        return "PENDING_RESTART", "RESTART_REQUIRED"
    hosts = list(db.scalars(select(PluginWorkerRuntime).where(
        PluginWorkerRuntime.generation == cohort.generation, PluginWorkerRuntime.plugin_id == "",
        PluginWorkerRuntime.lease_at > live_cutoff())))
    if len(hosts) != cohort.expected_workers:
        return "UNAVAILABLE", "WORKER_BARRIER_PENDING"
    rows = list(db.scalars(select(PluginWorkerRuntime).where(
        PluginWorkerRuntime.generation == cohort.generation, PluginWorkerRuntime.plugin_id == plugin.plugin_id,
        PluginWorkerRuntime.lease_at > live_cutoff())))
    if {w.worker_id for w in hosts} != {w.worker_id for w in rows}:
        return "UNAVAILABLE", "WORKER_BARRIER_PENDING"
    if any(w.desired_revision != plugin.desired_revision or w.plugin_digest != plugin.plugin_digest for w in rows):
        return "PENDING_RESTART", "RESTART_REQUIRED"
    if any(w.status == "FAILED" for w in rows):
        return "UNAVAILABLE", next((w.error_code for w in rows if w.status == "FAILED"), "LOAD_FAILED")
    if not plugin.desired_enabled:
        return ("DISABLED", "") if all(w.status == "DISABLED" for w in rows) else ("PENDING_RESTART", "RESTART_REQUIRED")
    if not all(w.status == "LOADED" for w in rows):
        return "UNAVAILABLE", "WORKER_BARRIER_PENDING"
    return "READY", ""


def require_stopped(db, plugin):
    current, _ = status(db, plugin)
    old = db.scalar(select(PluginWorkerRuntime.worker_id).where(
        PluginWorkerRuntime.plugin_id == plugin.plugin_id,
        PluginWorkerRuntime.status.in_(["LOADED", "LOADING", "FAILED"]),
        PluginWorkerRuntime.lease_at > live_cutoff()).limit(1))
    if plugin.desired_enabled or current != "DISABLED" or old:
        raise PluginError("DISABLE_AND_RESTART_REQUIRED", "请先停用、重启，并等待所有旧 worker 退出", 409)


class WorkerRuntime:
    def __init__(self):
        self.generation = os.environ.get("CWQ_PLUGIN_GENERATION", "")
        self.worker_id = uuid.uuid4().hex
        self.started_at = int(os.environ.get("CWQ_PLUGIN_STARTED_AT", "0"))
        self.expected = int(os.environ.get("CWQ_PLUGIN_EXPECTED_WORKERS", "0"))
        self._stop = threading.Event()
        self._thread = None
        self.handlers = {}
        self.hooks = {}
        self.tasks = {}

    def join(self):
        if not re.fullmatch(r"[a-f0-9]{32,64}", self.generation) or not self.started_at or not 1 <= self.expected <= 64:
            raise PluginError("INVALID_WORKER_CONFIGURATION", "开启插件必须通过 supervisor 注入共享 generation、启动时间和 worker 数量", 503)
        if settings.plugin_heartbeat_seconds <= 0 or settings.plugin_lease_seconds < settings.plugin_heartbeat_seconds * 3:
            raise PluginError("INVALID_WORKER_CONFIGURATION", "插件租约必须至少为 heartbeat 的三倍", 503)
        with SessionLocal.begin() as db:
            # 单行排他锁序列化新批次接管；时间只比较 supervisor 纳秒时间，不使用 worker 到达顺序。
            db.execute(insert(PluginCohort).values(id=1, generation=self.generation, started_at=self.started_at,
                                                  expected_workers=self.expected).on_conflict_do_nothing())
            cohort = db.scalar(select(PluginCohort).where(PluginCohort.id == 1).with_for_update())
            if cohort.generation != self.generation:
                if cohort.started_at >= self.started_at:
                    raise PluginError("STALE_GENERATION", "旧 supervisor 不能接管新代次", 503)
                cohort.generation, cohort.started_at, cohort.expected_workers = self.generation, self.started_at, self.expected
            elif cohort.started_at != self.started_at or cohort.expected_workers != self.expected:
                raise PluginError("WORKER_CONFIGURATION_MISMATCH", "同代次 worker 配置不一致", 503)
            db.add(PluginWorkerRuntime(generation=self.generation, worker_id=self.worker_id,
                                      plugin_id="", status="HOST", lease_at=now()))

    def register(self, plugin, result, code=""):
        with SessionLocal.begin() as db:
            db.merge(PluginWorkerRuntime(generation=self.generation, worker_id=self.worker_id,
                plugin_id=plugin.plugin_id, plugin_digest=plugin.plugin_digest,
                desired_revision=plugin.desired_revision, status=result, error_code=code, lease_at=now()))

    def start_heartbeat(self):
        self._thread = threading.Thread(target=self._heartbeat, name="plugin-lease", daemon=True)
        self._thread.start()

    def _heartbeat(self):
        while not self._stop.wait(settings.plugin_heartbeat_seconds):
            try:
                with SessionLocal.begin() as db:
                    db.execute(update(PluginWorkerRuntime).where(PluginWorkerRuntime.generation == self.generation,
                        PluginWorkerRuntime.worker_id == self.worker_id).values(lease_at=now()))
            except Exception:
                # 故障时租约自然过期，barrier 关闭；不能在 DB 故障时继续宣称 READY。
                pass

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with SessionLocal.begin() as db:
            db.execute(update(PluginWorkerRuntime).where(PluginWorkerRuntime.generation == self.generation,
                PluginWorkerRuntime.worker_id == self.worker_id).values(lease_at=now() - timedelta(days=1)))
