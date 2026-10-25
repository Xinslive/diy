"""定时任务开关写审计时**不能**把 ``task_key`` 整个塞进 ``entity_id``
（审查 §3.21）。

``audit_log.entity_id`` 是 ``String(64)``，而 ``task_key`` 最长可达
**152 字符**（``scheduler/service.py`` 里是 ``f"plugin:{plugin_id}:{name}"``，
``plugin_id`` 上限 96、任务名上限 48）。

原来 ``api/system.py`` 把 ``row.task_key`` 整个写进 ``entity_id`` ——
库里 sqlstate **22001** ⇒ **整个事务回滚**：

* ``row.enabled`` 的修改被撤销（开关没保存成功）；
* 审计行也一并消失（连「有人试图改过」都没留下）；
* 而全局异常处理器把 22001 翻成 400「提交的内容超出了字段允许的长度」
  —— 与真正的病因毫无关系。

**这是一次静默失败**：请求返回了错误，但真实原因被完全掩盖。

全仓其余写入点**早就**收敛过长度了 —— ``plugins/audit.py`` 的
``plugin_id[:64]``、``scheduler/service.py`` 的 ``_marker_id``
（sha256 截到 32 位）、``api/auth.py`` 的 ``username[:64]``。
``api/system.py`` 是**唯一的**例外。

所以下面钉的是：**开关要真的存下去**，**而且要留下一条审计**。
"""
from __future__ import annotations

import hashlib

import pytest

from app.models import AuditLog
from app.models.plugin import PluginRegistry
from app.models.scheduler import ScheduledTask
from app.scheduler import service as scheduler_service

#: 顶到上限的 plugin_id（96 字符）与任务名（48 字符）。
LONG_PLUGIN_ID = "p" * 96
LONG_TASK_NAME = "t" * 48
#: f"plugin:{plugin_id}:{name}" —— 7 + 96 + 1 + 48 = 152 字符。
LONG_TASK_KEY = f"plugin:{LONG_PLUGIN_ID}:{LONG_TASK_NAME}"


@pytest.fixture
def long_task(db):
    """一条**超长** ``task_key`` 的插件定时任务（开着）。

    配套的 ``plugin_registry`` 行是**必需**的：``sync_tasks`` 会在插件
    不存在/未启用时把任务的 ``enabled`` 强制成 False，那样就测不到
    「开关有没有存下去」了。
    """
    digest = "d" * 64
    db.add(PluginRegistry(
        plugin_id=LONG_PLUGIN_ID, version="1.0.0", plugin_digest=digest,
        manifest_version=1, desired_enabled=True, desired_revision="rev-1",
        access_default="none", runtime_status="PENDING_RESTART",
        tables=[], manifest_metadata={"id": LONG_PLUGIN_ID}))
    db.add(ScheduledTask(
        task_key=LONG_TASK_KEY, owner_type="PLUGIN", plugin_id=LONG_PLUGIN_ID,
        task_name=LONG_TASK_NAME, plugin_digest=digest,
        schedule={"type": "interval", "minutes": 30}, enabled=True,
        next_run_at=scheduler_service.utcnow()))
    db.commit()
    assert len(LONG_TASK_KEY) > 64, "这条用例的前提是 task_key 超过 64 字符"


def test_超长task_key的开关要真的存下去(client, auth, db, long_task):
    """**开关必须存下去** —— 改前它会被 22001 连着整个事务一起回滚掉。"""
    r = client.put(f"/api/system/scheduler/tasks/{LONG_PLUGIN_ID}/{LONG_TASK_NAME}",
                   json={"enabled": False}, headers=auth)
    assert r.status_code == 200, r.text

    db.expire_all()
    row = db.get(ScheduledTask, LONG_TASK_KEY)
    assert row is not None
    assert row.enabled is False, "开关没有存下去（事务被回滚了）"


def test_超长task_key的开关要留下审计(client, auth, db, long_task):
    """**审计行必须落库** —— 改前它随事务一起消失了，连痕迹都没留下。"""
    client.put(f"/api/system/scheduler/tasks/{LONG_PLUGIN_ID}/{LONG_TASK_NAME}",
               json={"enabled": False}, headers=auth)

    db.expire_all()
    rows = list(db.query(AuditLog).filter(
        AuditLog.action == "SCHEDULER_TASK_UPDATE").all())
    assert rows, "没有留下任何审计行 —— 这次改动查无实据"

    row = rows[-1]
    assert row.entity == "scheduled_task"
    # entity_id 必须**收敛**到 64 字符以内。
    assert len(row.entity_id) <= 64, f"entity_id 有 {len(row.entity_id)} 字符"
    # 口径与 scheduler/service.py 的 _marker_id 一致：sha256 截到 32 位。
    assert row.entity_id == hashlib.sha256(LONG_TASK_KEY.encode()).hexdigest()[:32]


def test_完整的task_key不能丢(client, auth, db, long_task):
    """**收敛长度不等于丢信息** —— 完整的 task_key 必须在 detail/after 里。"""
    client.put(f"/api/system/scheduler/tasks/{LONG_PLUGIN_ID}/{LONG_TASK_NAME}",
               json={"enabled": False}, headers=auth)

    db.expire_all()
    row = list(db.query(AuditLog).filter(
        AuditLog.action == "SCHEDULER_TASK_UPDATE").all())[-1]

    blob = f"{row.detail} {row.before} {row.after}"
    assert LONG_TASK_KEY in blob, "完整的 task_key 没能落进 detail/before/after"


def test_短task_key也照样留痕(client, auth, db, long_task):
    """**回归点**：短 key 的那条路没被改坏（它本来就没超长）。"""
    short_id = "short-plugin"
    short_name = "daily"
    digest = "d" * 64
    db.add(PluginRegistry(
        plugin_id=short_id, version="1.0.0", plugin_digest=digest,
        manifest_version=1, desired_enabled=True, desired_revision="rev-2",
        access_default="none", runtime_status="PENDING_RESTART",
        tables=[], manifest_metadata={"id": short_id}))
    db.add(ScheduledTask(
        task_key=scheduler_service.task_key(short_id, short_name),
        owner_type="PLUGIN", plugin_id=short_id, task_name=short_name,
        plugin_digest=digest, schedule={"type": "interval", "minutes": 30},
        enabled=True, next_run_at=scheduler_service.utcnow()))
    db.commit()

    r = client.put(f"/api/system/scheduler/tasks/{short_id}/{short_name}",
                   json={"enabled": False}, headers=auth)
    assert r.status_code == 200, r.text

    db.expire_all()
    row = list(db.query(AuditLog).filter(
        AuditLog.action == "SCHEDULER_TASK_UPDATE").all())[-1]
    assert row.entity_id == hashlib.sha256(
        scheduler_service.task_key(short_id, short_name).encode()).hexdigest()[:32]
