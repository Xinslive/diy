"""日志类表的保留期清理（``app/maintenance.py``）。

这一组用例钉的是**三件事**：

1. **过期的删掉、没到期的留下** —— 保留期机制的基本动作；
2. **删掉备份记录不会破坏备份分级** —— 这是最危险的一条：
   ``app/backup/tier.py::classify`` 用 ``sys_backup_run`` 判断「这个月/这周
   有没有成功过某一档」，而清理正好删这张表。删得太干净，月备份会**每天都判成
   新的一档**，于是永久保留的对象一天一个 —— 那不是少了几行日志，是备份的
   分级语义整个塌了。
3. **一张表清理失败不牵连其余表** —— 清理是「锦上添花」，不能反过来变成
   「因为清理失败把别的事判成失败」。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import maintenance
from app.backup import tier as tier_mod
from app.models import (SCOPE_BOOK, SCOPE_SYSTEM, STATUS_SUCCESS, TIER_DAILY, TIER_MONTHLY,
                        TIER_WEEKLY, AuditLog, BackupRun, Book, ScheduledTaskRun)

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)


def _audit(db, when: datetime, action="CREATE") -> AuditLog:
    row = AuditLog(action=action, entity="voucher", entity_id="1", detail="x",
                    username="test", created_at=when)
    db.add(row)
    db.commit()
    return row


def _backup_run(db, when: datetime, *, book_id=None, scope=SCOPE_SYSTEM,
                tier=TIER_DAILY, status=STATUS_SUCCESS) -> BackupRun:
    row = BackupRun(book_id=book_id, scope=scope, book_label="测试", tier=tier,
                    status=status, oss_key="", started_at=when, finished_at=when,
                    created_at=when)
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# 基本动作
# ---------------------------------------------------------------------------

def test_过期行被删_没到期的留下(db):
    """保留期的分界线就在 cutoff 上。"""
    老 = _audit(db, NOW - timedelta(days=400))
    新 = _audit(db, NOW - timedelta(days=10))
    db.commit()

    maintenance.prune_log_tables(db, now=NOW)

    还在 = {r.id for r in db.scalars(select(AuditLog)).all()}
    assert 老.id not in 还在, "400 天前的审计记录没被删掉"
    assert 新.id in 还在, "10 天前的审计记录被误删了（保留期是 365 天）"


def test_每张策略表都真的被清(db):
    """**每一条策略都要被钉住。** 新加一张策略忘了写判龄列的，这里会红。"""
    db.add(AuditLog(action="X", entity="e", entity_id="1", detail="",
                    username="t", created_at=NOW - timedelta(days=400)))
    db.add(BackupRun(book_id=None, scope=SCOPE_SYSTEM, book_label="x", tier=TIER_DAILY,
                     status=STATUS_SUCCESS, oss_key="", started_at=NOW - timedelta(days=400),
                     finished_at=NOW - timedelta(days=400),
                     created_at=NOW - timedelta(days=400)))
    db.add(ScheduledTaskRun(run_id="r-old", task_key="system:oss_backup", owner_type="SYSTEM",
                            task_name="oss_backup", plugin_digest="", status="SUCCESS",
                            scheduled_for=NOW - timedelta(days=400),
                            started_at=NOW - timedelta(days=400),
                            finished_at=NOW - timedelta(days=400)))
    db.commit()

    removed = maintenance.prune_log_tables(db, now=NOW)

    assert removed["audit_log"] >= 1
    assert removed["sys_backup_run"] >= 1
    assert removed["sys_scheduled_task_run"] >= 1


def test_环境变量能覆盖保留期(db, monkeypatch):
    monkeypatch.setenv("CWQ_LOG_RETENTION_AUDIT_LOG_DAYS", "7")
    老 = _audit(db, NOW - timedelta(days=30))
    db.commit()

    maintenance.prune_log_tables(db, now=NOW)

    assert 老.id not in {r.id for r in db.scalars(select(AuditLog)).all()}, \
        "环境变量把保留期改成 7 天，30 天前的记录就该被删掉"


def test_保留期设成零表示不删这张表(db, monkeypatch):
    """0 = **显式选择不删**，不是「忘了配」。"""
    monkeypatch.setenv("CWQ_LOG_RETENTION_AUDIT_LOG_DAYS", "0")
    老 = _audit(db, NOW - timedelta(days=4000))
    db.commit()

    maintenance.prune_log_tables(db, now=NOW)

    assert 老.id in {r.id for r in db.scalars(select(AuditLog)).all()}, \
        "保留期 0 意味着这张表不删，4000 天前的记录也该留着"


# ---------------------------------------------------------------------------
# 硬约束：清理不能破坏备份分级
# ---------------------------------------------------------------------------

def test_备份记录的保留期不能低于一个月(db):
    """**这条是硬约束，不是建议。**

    ``tier.py::classify`` 判「本月有没有成功过 monthly」时看的是
    ``sys_backup_run`` 里本月（最长 31 天）的成功记录。保留期一旦短于 31 天，
    月备份就会**每天都判成新的一档** —— 永久保留的对象一天一个，而页面上
    全是绿的。

    所以这里直接钉住数字下限：改小了这条用例就红。
    """
    policy = next(p for p in maintenance.POLICIES if p.table == "sys_backup_run")
    assert policy.days >= 31, (
        f"sys_backup_run 的保留期是 {policy.days} 天，低于 31 天 —— "
        "月备份会每天判成新的一档，永久保留的语义就塌了")


def test_清理之后备份分级判重仍然正确(db, book):
    """真正的那条：**清完再判，档位必须和清之前一样。**

    分级的阶梯是 monthly → weekly → daily（``tier.py::classify`` 取「够格的
    最高那一档」）。造一个「本月已经成功过 monthly」的账套 —— 于是它判成
    **weekly**（monthly 名额已占，weekly 还没占）。

    然后清掉一条 200 天前的记录（默认 90 天保留期 ⇒ 该被删），
    再判一次 —— 必须还是 weekly，而且**本月的 monthly 记录必须还在**。
    """
    本月初 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    月度记录 = _backup_run(db, 本月初, book_id=book.id, scope=SCOPE_BOOK, tier=TIER_MONTHLY)
    老记录 = _backup_run(db, NOW - timedelta(days=200), book_id=book.id, scope=SCOPE_BOOK,
                         tier=TIER_DAILY)
    db.commit()

    清理前 = tier_mod.classify(db, SCOPE_BOOK, book.id, NOW)
    assert 清理前 == TIER_WEEKLY, (
        f"本月 monthly 已成功过，应该判成 weekly（实际 {清理前}）")

    maintenance.prune_log_tables(db, now=NOW)

    剩下的 = {r.id for r in db.scalars(select(BackupRun)).all()}
    assert 月度记录.id in 剩下的, \
        "本月的 monthly 记录被删了 —— 备份分级被破坏，永久保留的对象会一天一个"
    assert 老记录.id not in 剩下的, "200 天前那条没被删掉"

    清理后 = tier_mod.classify(db, SCOPE_BOOK, book.id, NOW)
    assert 清理后 == TIER_WEEKLY, (
        f"清理前后档位不一致（{清理前} → {清理后}）—— "
        "清理把决定档位的记录删掉了")


def test_清掉的那条不影响本月的档位判定(db, book):
    """被删掉的是「上个月」的记录，它本来就不该影响本月的判定。"""
    上个月 = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)
    _backup_run(db, 上个月, book_id=book.id, scope=SCOPE_BOOK, tier=TIER_MONTHLY)
    db.commit()

    maintenance.prune_log_tables(db, now=NOW)

    # 9 月那条被删了（默认 90 天保留期），本月没有 monthly 记录 →
    # 现在应该判成 monthly（本月还没成功过 monthly）。
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_MONTHLY


# ---------------------------------------------------------------------------
# 一张表失败不牵连其余
# ---------------------------------------------------------------------------

def test_一张表清理失败不影响其余表(db, monkeypatch):
    """**清理失败不许把别的事带下水。**

    这里把 ``audit_log`` 那一格的删除弄成抛异常（用一个不存在的列模拟），
    其余几张表必须照常清掉 —— 而不是「audit_log 失败 → 整轮清理回滚」。
    """
    _audit(db, NOW - timedelta(days=400))
    _backup_run(db, NOW - timedelta(days=400))
    db.commit()

    原策略 = list(maintenance.POLICIES)

    def 坏的(policy):
        if policy.table == "audit_log":
            return maintenance.Policy(policy.table, policy.model, "不存在的列",
                                      policy.days, policy.why)
        return policy

    monkeypatch.setattr(maintenance, "POLICIES", tuple(坏的(p) for p in 原策略))

    removed = maintenance.prune_log_tables(db, now=NOW)

    assert removed["audit_log"] == 0, "坏掉的那张表不该报删了行"
    assert removed["sys_backup_run"] >= 1, \
        "一张表失败把其余表的清理也连累了 —— 那正是这条用例要拦的事"


def test_清理本身不抛异常(db, monkeypatch):
    """清理跑在 tick 与启动路径上，那两条路都不该因为清理而挂。"""
    monkeypatch.setattr(maintenance, "POLICIES", (
        maintenance.Policy("audit_log", AuditLog, "不存在的列", 365, "故意弄坏"),
    ))
    assert maintenance.prune_log_tables(db, now=NOW) == {"audit_log": 0}
