#!/usr/bin/env python
"""调度器现状：应用内调度器有没有在真的做离站备份。

    python scripts/scheduler_state.py            # 人读
    python scripts/scheduler_state.py --json     # 机器读
    退出码 0 = 应用调度器正在做离站备份；1 = 没有（或看不出来）

# 为什么需要这个脚本

2026-10-18 起，离站备份（OSS）由**应用内调度器**的 ``system:oss_backup``
任务负责（cron ``47 3 * * *``，Asia/Shanghai），外部调度服务每分钟调一次
``/api/scheduler/tick`` 驱动它。而更早还有一条 systemd 路径：
``cwq-oss-backup.timer`` → ``scripts/backup_to_oss.py``。

**两条路径干的是同一件事。** 同时启用 = 每天备份两次 = OSS 上同一份数据传两遍、
``sys_backup_run`` 每天多出 10 行记录（每轮 5 个目标）、备份时间多占一倍。
ARCHITECTURE §11.13 与 ``docs/plugin-roadmap.md`` 都写着它们**不能同时启用**。

# 判据：看**做没做**，不看**配没配**

「触发密钥已配置」只是一个**声明**：它说明运维打算用应用调度器，但不说明
外部调度服务真的在调 tick。于是本脚本不用它当判据 —— 它可能配好了而外部
cron 压根没部署，那种情况下停掉旧 timer 就等于**离站备份彻底没了**。

真正的判据是**最近有没有成功跑过** ``system:oss_backup``：跑过就说明应用
调度器确实在干活，旧 timer 是**确凿的重复**；没跑过就说明还不知道谁在负责，
此时**保留旧 timer**（宁可多备一次，也别一次都不备）。

这个判据的错误方向是**安全的**：判错了最多多备一次（多传一份、多几行记录），
而不会漏备。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import ScheduledTask, ScheduledTaskRun  # noqa: E402

TASK_KEY = "system:oss_backup"

#: 「最近」取多久。取一天是因为备份是每天一次 —— 昨天的成功就是「它还活着」。
DEFAULT_MAX_AGE_HOURS = 48


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def collect(max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict:
    now = _utcnow()
    cutoff = now - timedelta(hours=max_age_hours)
    with SessionLocal() as db:
        task = db.scalar(select(ScheduledTask).where(ScheduledTask.task_key == TASK_KEY))
        last = db.scalar(
            select(ScheduledTaskRun)
            .where(ScheduledTaskRun.task_key == TASK_KEY,
                   ScheduledTaskRun.status == "SUCCESS")
            .order_by(ScheduledTaskRun.finished_at.desc())
            .limit(1)
        )
        secret_configured = False
        try:
            from app.core import system_settings

            secret_configured = bool(
                system_settings.get_value(db, "scheduler_trigger_secret", "")
                or system_settings.get_value(db, "scheduler_trigger_secret_previous", "")
            )
        except Exception:                       # noqa: BLE001 - 查不到就当没配
            secret_configured = False

    age_hours = None
    if last is not None and last.finished_at is not None:
        finished = last.finished_at
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        age_hours = round((now - finished).total_seconds() / 3600, 2)

    active = bool(last is not None and last.finished_at is not None
                  and age_hours is not None and age_hours <= max_age_hours)
    return {
        "task_key": TASK_KEY,
        "task_enabled": bool(task is not None and task.enabled),
        "trigger_secret_configured": secret_configured,
        "last_success_at": (last.finished_at.isoformat() if last is not None
                             and last.finished_at else None),
        "last_success_age_hours": age_hours,
        "max_age_hours": max_age_hours,
        "scheduler_owns_backup": active,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="应用内调度器有没有在做离站备份")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS,
                    help=f"「最近」取多久（默认 {DEFAULT_MAX_AGE_HOURS} 小时）")
    args = ap.parse_args(argv)

    try:
        state = collect(args.max_age_hours)
    except Exception as exc:                    # noqa: BLE001 - 查不到就说查不到
        if args.json:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}",
                              "scheduler_owns_backup": False}, ensure_ascii=False))
        else:
            print(f"查不到调度器状态：{type(exc).__name__}: {exc}")
        return 1

    if args.json:
        print(json.dumps(state, ensure_ascii=False))
    else:
        print(f"任务 {state['task_key']}：{'启用' if state['task_enabled'] else '未启用'}")
        print(f"触发密钥：{'已配置' if state['trigger_secret_configured'] else '未配置'}")
        if state["last_success_at"]:
            print(f"最近一次成功：{state['last_success_at']}"
                  f"（{state['last_success_age_hours']} 小时前）")
        else:
            print("最近一次成功：从未")
        print("应用调度器正在做离站备份："
              + ("是" if state["scheduler_owns_backup"] else "否（看不出来）"))

    return 0 if state["scheduler_owns_backup"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
