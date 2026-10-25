"""日志类表的保留期。**全系统「留多久」只有这一个文件说了算。**

# 为什么单独一个文件

「日志会无限长」在这套系统里有好几个地方各自长：

* ``audit_log`` —— 每一次改凭证、开期、授权、备份都留一行（**一天能几千行**）；
* ``sys_backup_run`` —— 每轮备份每个目标留一行；
* ``sys_scheduled_task_run`` —— 每次计划任务触发留一行；
* ``admin_mfa_challenge`` —— 每次管理员登录留一行；
* ``plugin_install_attempt`` —— 每次插件安装留一行。

它们的**增长速度差三个数量级**（审计一天几千行，MFA 一天几行），但有一个
共同点：**都是只增不减的**。

而「只增不减」在数据库上不是慢性病，是**定时炸弹** —— 它不会报错，只会在某天
让磁盘写满、让查询变慢，而那天的现象（页面变卡）与它真正的起因（八个月前没人
删过日志）之间隔着一条没人能追溯的链。系统看起来一直好好的。

所以保留期必须是**一张表、一处真相**，而不是散在五个模块里各写各的 ——
散着写的结果必然是「改了三个、漏了两个」，而漏掉的那个往往就是真正在涨的那个
（本项目就有一个现成的例子：``sys_scheduled_task_run`` 从一开始就有 90 天清理，
而每天写十几行的 ``audit_log`` 一直没有）。

# 为什么默认保留期是这些数

每个数都不是随手取的，也不是「先给个大的以后再调」：

* **``audit_log`` 365 天** —— 一年。凭证号可复用，审计快照是「同一个号码事后
  对应到哪一笔」的唯一依据，所以它比一般日志活得久；但它同时是**增长最快的**
  一张表（一笔一行的操作全进它），留太久就是在为「几乎不可能再查的三年前的
  一次改名」付磁盘钱。
* **``sys_backup_run`` 90 天** —— **不能低于 31 天**，这是硬约束：
  ``app/backup/tier.py::classify`` 用这张表判断「这个月/这周有没有成功过某一档」，
  而「本月」的窗口最长是一个月（31 天）。删得太干净会让月备份**每天都判成新的
  一档**，于是永久保留的对象一天一个 —— 那是「留得太少」最典型的坏处：
  不是少了几行日志，而是备份的分级语义整个塌了。90 天留了足够余量。
* **``sys_scheduled_task_run`` 90 天** —— 沿用原有的口径（领取任务时顺手删），
  搬到这里只是为了让「所有保留期在一个文件里」成立，行为不变。
* **``admin_mfa_challenge`` 30 天** —— MFA 挑战的有效期是分钟级的
  （``AdminMfaChallenge.ttl_seconds``），留 30 天已经是「够查一次谁在什么时候
  触发过二次验证」的量级。再长只是白长。
* **``plugin_install_attempt`` 90 天** —— 安装现场（staging/active 路径、
  新旧摘要）用于排查「装到一半中断」。90 天足够覆盖一次完整的上线周期。

# 怎么执行

``prune_log_tables()`` 由**应用内调度器的 tick** 驱动（外部每分钟调一次
``/api/scheduler/tick``）。选它有两个理由：

* tick 已经是系统里唯一的周期性入口 —— 再挂一个常驻清理线程就是第二套调度器
  （见 ARCHITECTURE §11.13「宿主不起自己的常驻调度循环」）；
* tick 停了会有人知道 —— ``cwq-scheduler-watchdog`` 专门盯这一件事，
  逾期就发告警邮件。清理跟着 tick 走，等于「清理停摆」这件事也是**会被发现的**。

外加**进程启动时也清一次**：没配 tick 密钥的部署（应用调度器根本没启用）
不能因此就让审计日志一直涨。

# 覆盖写法

每个表的保留期都能用环境变量覆盖，名字是表名的大写加前缀：

    CWQ_LOG_RETENTION_AUDIT_LOG_DAYS=730

设成 ``0`` = **不删这一张表**（显式选择，不是「忘了配」）。正数就是天数。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import (AdminMfaChallenge, AuditLog, BackupRun, PluginInstallAttempt,
                        ScheduledTaskRun)

logger = logging.getLogger(__name__)

#: 环境变量前缀。
ENV_PREFIX = "CWQ_LOG_RETENTION_"


@dataclass(frozen=True)
class Policy:
    """一张日志表的保留策略。

    ``column`` 是**判龄用哪一列**：绝大多数用 ``created_at``（行产生的时间），
    而 ``sys_scheduled_task_run`` 用 ``finished_at`` —— 它的 ``created_at`` 是
    被**领取**的时刻，而一次任务可能跑很久，用领取时刻判龄会让「还在跑的任务」
    看起来很老。
    """

    table: str
    model: type
    column: str
    days: int
    why: str


#: **全系统所有只增不减的日志表，都在这张表里。**
#:
#: 新增一张「只增不减」的表时，**必须**在这里加一条 —— 这是本文件存在的
#: 全部理由。反过来（给这里加了一条却忘了写 why）也一样要被打回。
POLICIES: tuple[Policy, ...] = (
    Policy(
        "audit_log", AuditLog, "created_at", 365,
        "一年。凭证号可复用，审计快照是事后对应到具体那笔的唯一依据；"
        "同时它是增长最快的一张表，留太久就是为几乎不可能再查的三年前付磁盘钱。",
    ),
    Policy(
        "sys_backup_run", BackupRun, "created_at", 90,
        "**硬约束：不能低于 31 天。** tier.py::classify 用这张表判「本月/本周"
        "有没有成功过某一档」，本月的窗口最长 31 天；删得太干净会让月备份每天都"
        "判成新的一档，永久保留的对象一天一个 —— 那不是少了几行日志，是分级语义塌了。",
    ),
    Policy(
        "sys_scheduled_task_run", ScheduledTaskRun, "finished_at", 90,
        "沿用原有口径（领取任务时顺手删），搬到这里只为让所有保留期在一处。"
        "判龄用 finished_at 而不是 created_at：created_at 是被领取的时刻。",
    ),
    Policy(
        "admin_mfa_challenge", AdminMfaChallenge, "created_at", 30,
        "MFA 挑战的有效期是分钟级的，留 30 天已足够回溯一次二次验证的触发时间。",
    ),
    Policy(
        "plugin_install_attempt", PluginInstallAttempt, "created_at", 90,
        "安装现场（新旧摘要、staging/active 路径）用于排查「装到一半中断」，"
        "90 天足够覆盖一次完整上线周期。",
    ),
)


def _days_from_env(policy: Policy) -> int:
    """环境变量覆盖。**0 = 不删这张表**（显式选择，不是「忘了配」）。"""
    raw = os.environ.get(ENV_PREFIX + policy.table.upper() + "_DAYS", "").strip()
    if not raw:
        return policy.days
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("保留期环境变量 %s%s_DAYS 不是整数（%r），用默认值 %d",
                       ENV_PREFIX, policy.table.upper(), raw, policy.days)
        return policy.days


def effective_days(policy: Policy) -> int:
    """这张表**当前生效**的保留天数（含环境变量覆盖）。"""
    return _days_from_env(policy)


def prune_log_tables(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """删掉所有超过保留期的日志行。返回 ``{表名: 删掉几行}``。

    **每张表单独 try。** 一张表删失败（缺列、表不存在、锁超时）不该让其余几张
    也清不成 —— 清理是「锦上添花」，绝不能反过来变成「因为清理失败把备份判成
    失败」（那正是 §11.10 记的那个事故形态）。

    **不抛异常。** 调用方在 tick 与启动路径上，那两条路都不该因为清理而挂。
    """
    moment = now or datetime.now(timezone.utc)
    removed: dict[str, int] = {}
    for policy in POLICIES:
        days = effective_days(policy)
        if days <= 0:
            removed[policy.table] = 0
            continue
        cutoff = moment - timedelta(days=days)
        try:
            column = getattr(policy.model, policy.column)
            # **用 SAVEPOINT（begin_nested），不用整事务 rollback。**
            #
            # 整事务 rollback 会把**前面几张表已经删掉的行一起退回去** ——
            # 于是「audit_log 删失败」会连带把 sys_backup_run 的清理也撤销，
            # 而那正是「一张表失败不牵连其余」要防的事。
            with db.begin_nested():
                result = db.execute(delete(policy.model).where(column < cutoff))
                count = int(result.rowcount or 0)
            removed[policy.table] = count
        except Exception as exc:                       # noqa: BLE001 - 一张表失败不牵连其余
            logger.warning("清理 %s 失败（已跳过，其余表照常清理）：%s: %s",
                           policy.table, type(exc).__name__, exc)
            removed[policy.table] = 0
    return removed


def prune_log_tables_standalone() -> dict[str, int]:
    """开自己的会话清一遍。给脚本用（不依赖某个请求的会话）。"""
    from app.core.db import SessionLocal

    with SessionLocal.begin() as db:
        return prune_log_tables(db)
