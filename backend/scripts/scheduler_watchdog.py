#!/usr/bin/env python
"""调度看门狗：发现「到点该跑却没跑」的计划任务。

    # 检查一次，有逾期就发一封告警邮件（幂等：同一轮逾期只发一次）
    python scripts/scheduler_watchdog.py

    # 只查不发（演练用）
    python scripts/scheduler_watchdog.py --dry-run

**为什么它必须住在应用外面。**

宿主的计划任务由**外部调度服务**每分钟调一次 ``/api/scheduler/tick`` 驱动
（见 ARCHITECTURE §11.13）。于是有一条很要命的依赖：

    外部调度服务挂了 → tick 不来 → 任务不被领取 → **所有定时任务静默停摆**

而这件事**应用自己发现不了**：应用只在被 tick 到的时候才醒着，tick 不来的
那天夜里，它连"今天没被叫醒"都不知道。等到有人发现"备份怎么没产出"，往往
已经过了几天 —— 而备份是出事后的退路（P0-1 与本脚本关心的是同一件事的两面）。

所以看门狗要**独立于应用**地跑在这台机器上：应用负责干活，看门狗负责
「应用有没有被叫醒」。

**它不替代任何东西。** 任务真的跑了但失败时，告警由调度器自己发
（``events.task_failed``）；看门狗只管**压根没跑**这一种 —— 那一类在
应用内是**结构性看不见**的。

**去重靠审计日志。** 每告警一次就写一条 ``SCHEDULER_TASK_MISSED`` 审计记录，
下一次检查时用「这个任务、这一轮逾期（``next_run_at`` 之后）告警过没有」
来判重。于是：

* 同一轮逾期只发一封信（不刷屏）；
* 任务恢复运行、``next_run_at`` 前进之后，下一轮逾期**会**重新告警；
* 告警本身在「操作日志」页可见 —— 那是这套系统里可靠的那一份记录。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.accounting.audit_helper import log  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import AuditLog, ScheduledTask  # noqa: E402
from app.notify import events as notify_events  # noqa: E402

#: 审计动作名。也是去重的依据（见模块说明）。
ACTION = "SCHEDULER_TASK_MISSED"

#: 默认容忍：任务逾期多久算「没跑」。
#:
#: 30 分钟不是随便取的：tick 是**每分钟**调一次的，正常情况下任务最多晚
#: 几分钟；而一次发布/维护的窗口也可能长到这个量级。取 30 分钟意味着
#: 「短暂的维护不会被误报」，而「tick 真的死了」会在半小时内被发现 ——
#: 对每天一次的备份来说，仍是当天上午就知道，而不是几天后。
DEFAULT_GRACE_MINUTES = 30

#: 环境变量名（给 systemd unit 用，省得改容器的命令行）。
GRACE_ENV = "CWQ_WATCHDOG_GRACE_MINUTES"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _宽限分钟() -> int:
    raw = os.environ.get(GRACE_ENV, "")
    try:
        return max(1, int(raw)) if raw.strip() else DEFAULT_GRACE_MINUTES
    except ValueError:
        return DEFAULT_GRACE_MINUTES


def _逾期任务(db, now: datetime, grace: timedelta) -> list[ScheduledTask]:
    """**启用中**、且已经过了点还没被领取的任务。

    ``next_run_at`` 在任务被领取时就会前进到下一个周期（``_next_after_claim``），
    所以「它还在过去」= 「它到点了却没人来领」= **tick 没来**。

    停用的任务不在内：管理员主动关掉一个任务不是故障。插件版本变化导致
    任务被自动关闭的同理（那是设计如此，不是坏了）。
    """
    tasks = db.scalars(select(ScheduledTask)
                       .where(ScheduledTask.enabled.is_(True))).all()
    逾期 = []
    for task in tasks:
        due = task.next_run_at
        if due is None:
            continue
        if now - due <= grace:
            continue
        逾期.append(task)
    return 逾期


def _这一轮告警过(db, task: ScheduledTask) -> bool:
    """这个任务、**这一轮逾期**（``next_run_at`` 之后）告警过没有。

    判据用 ``created_at >= next_run_at`` 而不是「最近有没有告警过」：任务
    恢复运行后 ``next_run_at`` 会**前进到下一个周期**（那是未来的时刻），
    旧的那条告警记录落在它前面，不再覆盖新一轮 —— 于是下一轮该报还是会报，
    而同一轮不会重复报。
    """
    次数 = db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == ACTION,
        AuditLog.entity == "scheduled_task",
        AuditLog.entity_id == task.task_key,
        AuditLog.created_at >= task.next_run_at,
    ))
    return bool(次数)


def _时间文本(value) -> str:
    if value is None:
        return "从未"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _告警(session, task: ScheduledTask, now: datetime, dry_run: bool) -> bool:
    """告警一次。**先发信，再留审计** —— 顺序不能反。

    反过来的话：审计写成功、邮件发失败 → 标记成"已告警" → 这一轮就再也不会
    重发，而邮件正是这件事唯一的主动通知渠道，等于把告警吞了。

    反过来则只是「多发一封」，那可以接受。
    """
    逾期 = now - task.next_run_at
    逾期文本 = f"{逾期.days} 天 {逾期.seconds // 3600} 小时" if 逾期.days else \
        f"{逾期.seconds // 60} 分钟"
    due_text = _时间文本(task.next_run_at)
    last_text = _时间文本(task.last_run_at)

    if dry_run:
        print(f"[演练] {task.task_key} 逾期 {逾期文本}"
              f"（本该 {due_text} 运行，最近一次 {last_text}）")
        return False

    # 邮件发不出去也要留审计：事件**确实发生**了，审计是可靠的那一份。
    try:
        notify_events.task_missed_blocking(
            task.task_key, task.task_name, task.owner_type,
            due_text, 逾期文本, last_text)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  告警邮件发送失败（已忽略，不影响看门狗本身）：{exc}",
              file=sys.stderr)

    log(session, action=ACTION, entity="scheduled_task", entity_id=task.task_key,
        user=SimpleNamespace(id=None, username="watchdog"),
        detail=f"计划任务到点没有运行：逾期 {逾期文本}，本该 {due_text} 运行，"
               f"最近一次运行 {last_text}")
    session.commit()
    print(f"已告警：{task.task_key} 逾期 {逾期文本}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="调度看门狗：发现到点没跑的计划任务")
    ap.add_argument("--dry-run", action="store_true", help="只查不发")
    ap.add_argument("--grace-minutes", type=int, default=None,
                    help=f"逾期多久算没跑（默认 {DEFAULT_GRACE_MINUTES} 分钟，"
                         f"也可用环境变量 {GRACE_ENV}）")
    args = ap.parse_args(argv)

    grace_minutes = args.grace_minutes if args.grace_minutes is not None \
        else _宽限分钟()
    grace = timedelta(minutes=max(1, grace_minutes))
    now = utcnow()

    session = SessionLocal()
    try:
        逾期 = _逾期任务(session, now, grace)
        if not 逾期:
            print(f"没有逾期的计划任务（{now.isoformat()}，容忍 {grace_minutes} 分钟）")
            return 0
        告警数 = 0
        for task in 逾期:
            if _这一轮告警过(session, task):
                print(f"已告警过（同一轮逾期，不重复发）：{task.task_key}")
                continue
            if _告警(session, task, now, args.dry_run):
                告警数 += 1
        print(f"检查完成：逾期 {len(逾期)} 个，本次新告警 {告警数} 个")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
