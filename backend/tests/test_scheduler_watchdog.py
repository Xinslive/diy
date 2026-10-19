"""调度看门狗：发现「到点该跑却没跑」的计划任务。

**这份测试盯的是一条结构性依赖。**

宿主的计划任务由**外部调度服务**每分钟调一次 ``/api/scheduler/tick`` 驱动。
于是：外部调度服务挂了 → tick 不来 → 任务不被领取 → **所有定时任务静默
停摆**（包括每天一次的账套备份 —— 那是出事后的退路）。

而这件事**应用自己发现不了**：它只在被 tick 到的时候才醒着。tick 不来的
那天夜里，服务器安静、进程健康、日志干净 —— 一切正常，除了备份没产出。
所以看门狗必须**独立于应用**地跑（``scripts/scheduler_watchdog.py`` +
systemd timer），它要发现的正是"应用没被叫醒"这件事。

**这里钉的是看门狗自己的四条性质。**

1. **只报"压根没跑"**，不重复报"跑了但失败"（那件事调度器自己会报）。
2. **同一轮逾期只告警一次** —— 不刷屏，否则告警本身会被无视。
3. **任务恢复后，下一轮逾期会重新告警** —— 去重不能把以后的告警也压掉。
4. **告警邮件发不出去，不影响看门狗本身** —— 它是旁路，且审计那一路
   才是可靠的那一份。
"""
from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog, ScheduledTask
from app.notify import events as notify_events

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _加载看门狗():
    spec = importlib.util.spec_from_file_location(
        "cwq_scheduler_watchdog", BACKEND_ROOT / "scripts" / "scheduler_watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watchdog = _加载看门狗()


@pytest.fixture
def 发出的信(monkeypatch):
    记录 = []

    def _fake_dispatch(event, subject, body, *, db=None):
        记录.append({"event": event, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(notify_events, "dispatch", _fake_dispatch)
    return 记录


@pytest.fixture
def 清任务():
    """看门狗看的是**全部**启用中的任务，所以每个用例都要一个干净起点。

    **审计记录也要清**：看门狗是**自己开会话并 commit** 的（它的存在意义
    就是住在应用之外），而 ``db`` 夹具的 rollback 只回滚它自己那个会话 ——
    于是上一条用例写下的告警记录会留在库里，而去重判据正是"有没有告警过"。
    不清的话，第二条用例一进来就被判成"已告警过"，测不到该测的东西。
    """
    with SessionLocal.begin() as db:
        db.execute(ScheduledTask.__table__.delete())
        db.execute(AuditLog.__table__.delete().where(AuditLog.action == watchdog.ACTION))
    yield
    with SessionLocal.begin() as db:
        db.execute(ScheduledTask.__table__.delete())
        db.execute(AuditLog.__table__.delete().where(AuditLog.action == watchdog.ACTION))


def _建任务(key="system:oss_backup", *, 逾期分钟=60, enabled=True):
    with SessionLocal.begin() as db:
        now = watchdog.utcnow()
        db.add(ScheduledTask(
            task_key=key, owner_type="SYSTEM", task_name="oss_backup",
            schedule={"type": "cron", "expression": "47 3 * * *"}, enabled=enabled,
            next_run_at=now - timedelta(minutes=逾期分钟),
        ))


def test_逾期任务会被发现并发告警(清任务, 发出的信):
    """到点没人领 → 告警。"""
    _建任务()

    assert watchdog.main([]) == 0

    assert len(发出的信) == 1, "任务没跑却没人告警"
    信 = 发出的信[0]
    assert 信["event"] == "TASK_MISSED"
    assert "system:oss_backup" in 信["subject"]
    assert "外部调度服务" in 信["body"], "告警没说该去查什么"


def test_同一轮逾期不重复告警(清任务, 发出的信):
    """**不刷屏。**

    告警每 15 分钟跑一次，一个没被修好的逾期会一直逾期 —— 不去重的话，
    管理员一晚上会收到几十封一样的信，然后开始无视这一类邮件。而无视了
    之后，真正的告警也一起被无视了。
    """
    _建任务()

    watchdog.main([])
    watchdog.main([])
    watchdog.main([])

    assert len(发出的信) == 1, f"同一轮逾期发了 {len(发出的信)} 封"


def test_任务恢复运行后下一轮逾期会重新告警(清任务, 发出的信, monkeypatch):
    """去重只能压住**这一轮**，不能把以后的告警也压掉。

    判据是「有没有就这个 ``next_run_at`` 告警过」。任务跑过一次之后
    ``next_run_at`` 会**前进到下一个周期**（比上一条告警的时刻更晚），
    于是上一条告警记录不再覆盖新一轮 —— 这正是真实时序。
    """
    _建任务()
    watchdog.main([])
    assert len(发出的信) == 1

    # 任务跑过了：next_run_at 前进到下一个周期
    下一周期 = watchdog.utcnow() + timedelta(hours=1)
    with SessionLocal.begin() as db:
        task = db.get(ScheduledTask, "system:oss_backup")
        task.next_run_at = 下一周期
    # 时间过去了：那个周期也到期了，而且没人来领
    monkeypatch.setattr(watchdog, "utcnow", lambda: 下一周期 + timedelta(hours=2))
    watchdog.main([])

    assert len(发出的信) == 2, "任务恢复后新一轮逾期没有再告警"


def test_停用的任务不告警(清任务, 发出的信):
    """管理员主动关掉一个任务**不是故障**。

    插件版本变化导致任务被自动关闭的同理（那是设计如此）。
    """
    _建任务(enabled=False)

    watchdog.main([])

    assert 发出的信 == [], "停用的任务被当成了故障"


def test_还没超出容忍期的不告警(清任务, 发出的信):
    """短暂延迟不是故障。

    tick 是每分钟调一次的，任务晚几分钟很正常；一次发布也可能占掉这个
    窗口。容忍期就是给这些留的余量。
    """
    _建任务(逾期分钟=5)

    watchdog.main([])

    assert 发出的信 == [], "只逾期 5 分钟就告警了"


def test_告警会留一条审计(清任务, 发出的信):
    """**审计是可靠的那一份**，邮件是尽力而为。

    告警这件事本身要能在「操作日志」页看到 —— 那是这套系统里"事后能查"
    的通道。它同时也是去重的依据。
    """
    _建任务()

    watchdog.main([])

    with SessionLocal() as db:
        条目 = db.scalars(select(AuditLog).where(
            AuditLog.action == watchdog.ACTION)).all()
    assert len(条目) == 1
    assert 条目[0].entity_id == "system:oss_backup"
    assert 条目[0].username == "watchdog"


def test_邮件发不出去也不影响看门狗(清任务, monkeypatch, capsys):
    """告警是**旁路**。

    SMTP 挂了、没配收件人 —— 这些都不该让看门狗自己失败，更不该让它
    **不再写审计**：审计才是可靠的那一份，邮件坏了它得还在。
    """
    def _炸(*a, **k):
        raise RuntimeError("SMTP 挂了")

    monkeypatch.setattr(notify_events, "dispatch", _炸)
    _建任务()

    assert watchdog.main([]) == 0, "邮件失败让看门狗失败了"

    with SessionLocal() as db:
        条目 = db.scalars(select(AuditLog).where(
            AuditLog.action == watchdog.ACTION)).all()
    assert len(条目) == 1, "邮件发不出去时审计也没留下 —— 事件本身发生过了"
    assert "告警邮件发送失败" in capsys.readouterr().err


def test_演练模式不写库也不发信(清任务, 发出的信):
    """``--dry-run`` 是演练：只看不碰。"""
    _建任务()

    assert watchdog.main(["--dry-run"]) == 0

    assert 发出的信 == []
    with SessionLocal() as db:
        条目 = db.scalars(select(AuditLog).where(
            AuditLog.action == watchdog.ACTION)).all()
    assert 条目 == []


def test_容忍期可以用环境变量调(monkeypatch):
    """systemd unit 用环境变量调它，不必改容器命令行。"""
    monkeypatch.setenv(watchdog.GRACE_ENV, "7")
    assert watchdog._宽限分钟() == 7

    monkeypatch.setenv(watchdog.GRACE_ENV, "不是数字")
    assert watchdog._宽限分钟() == watchdog.DEFAULT_GRACE_MINUTES
