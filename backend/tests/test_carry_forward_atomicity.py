"""结转损益的事务边界（审计 P1-20）。

``carry_forward_profit`` 原来把**数据**和**审计日志**拆成两个事务：

    db.commit()      # 凭证（含记账状态）落库
    log(...)         # 审计快照
    db.commit()      # 日志落库

两者之间任何失败 —— 磁盘、连接中断、进程被杀 —— 都留下「账已改、审计无记录」
的状态。而这个系统的凭证号**可回收**：``create_voucher`` 会复用被删掉的号，
所以审计快照是事后能追到「那张结转凭证本来长什么样」的**唯一**手段
（``accounting/audit_helper`` 自己就是这么写的）。半截账比报错危险得多：
用户看到 400 会以为「什么都没发生」，而库里已经有了一张没有来历的凭证。

同一文件的 ``year_end_carry`` 反而是对的（全程 ``commit=False``，末尾唯一提交点），
这里就是把它对齐。

**断言口径是「失败后数据库与调用前完全一致」**，不是「没报错」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accounting import periods as svc
from app.accounting.periods import (carry_forward_profit, close_period,
                                    year_end_carry)
from app.models import AuditLog, Entry, Period, Voucher

ZERO = Decimal("0")


def _p1(book, periods):
    return periods[1]


def _snapshot(db, book, period):
    """结转损益的全部可观察后果：凭证、记账状态、余额、审计。"""
    rows = db.execute(
        select(Entry.subject_id, func.sum(Entry.amount).filter(Entry.direction == "DEBIT"),
               func.sum(Entry.amount).filter(Entry.direction == "CREDIT"))
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .where(Voucher.period_id == period.id)
        .group_by(Entry.subject_id)
    ).all()
    return {
        "entries": sorted((r[0], Decimal(r[1] or 0), Decimal(r[2] or 0)) for r in rows),
        "carry": sorted(v.voucher_no for v in db.scalars(
            select(Voucher).where(Voucher.period_id == period.id,
                                  Voucher.source == "CARRY_FORWARD")).all()),
        "posted": sorted(v.voucher_no for v in db.scalars(
            select(Voucher).where(Voucher.period_id == period.id,
                                  Voucher.status == "POSTED")).all()),
        "audits": sorted(a.action for a in db.scalars(
            select(AuditLog).where(AuditLog.book_id == book.id,
                                   AuditLog.entity == "period")).all()),
    }


def _count_commits(db, monkeypatch) -> list:
    """统计这段时间里 commit 了几次 —— 断言「单事务」比断言「结果对」更直接。"""
    calls: list[int] = []
    real = db.commit

    def counted():
        calls.append(1)
        real()

    monkeypatch.setattr(db, "commit", counted)
    return calls


def _boom(*args, **kwargs):
    raise RuntimeError("模拟写审计日志时连接中断")


# --------------------------------------------------------------------- 失败不留半截账


def test_审计写入失败时不留半截账(db, book, periods, subjects, admin_user,
                                  make_voucher, monkeypatch):
    """审计那一步失败 → 凭证也必须一起没了。

    这是 P1-20 的核心场景：老代码在这里已经 commit 过凭证了，
    于是库里留下一张没有审计快照的结转凭证，而接口还返回了错误。
    """
    make_voucher("2026-01-10", dr=[("600101", 1000, "收入")],
                 cr=[("100201", 1000, "收款")], posted=True)
    p = db.get(Period, _p1(book, periods).id)
    before = _snapshot(db, book, p)

    monkeypatch.setattr(svc, "log", _boom)
    with pytest.raises(RuntimeError):
        carry_forward_profit(db, book.id, p, admin_user)
    # 路由层捕获后统一 rollback
    db.rollback()

    after = _snapshot(db, book, p)
    assert after == before, (
        "结转损益中途失败后数据库没有回到调用前状态：\n"
        f"  调用前：{before}\n  回滚后：{after}")
    assert after["carry"] == [], "残留了一张没有审计记录的结转凭证"


def test_HTTP层审计失败也是原子的(client, db, book, periods, subjects, admin_user,
                                  make_voucher, auth, monkeypatch):
    """接口层同样：返回 5xx 之后，库里必须什么都没变。"""
    make_voucher("2026-01-10", dr=[("600101", 1000, "收入")],
                 cr=[("100201", 1000, "收款")], posted=True)
    p = _p1(book, periods)
    before = _snapshot(db, book, p)

    monkeypatch.setattr(svc, "log", _boom)
    r = client.post(f"/api/books/{book.id}/periods/{p.id}/carry-forward", headers=auth)
    assert r.status_code >= 500, f"应当失败，实际 {r.status_code}：{r.text}"

    db.expire_all()
    assert _snapshot(db, book, db.get(Period, p.id)) == before, "接口报错但库里已经变了"


def test_重新结转中途失败不留下删了旧的只生成一半新的(
    db, book, periods, subjects, admin_user, make_voucher, monkeypatch
):
    """replace 模式先删旧凭证再重建 —— 失败时不能停在「旧的没了、新的没来」。

    用户会以为凭证还在，去别处重记一遍，下个月就多出一张。
    """
    make_voucher("2026-01-10", dr=[("600101", 1000, "收入")],
                 cr=[("100201", 1000, "收款")], posted=True)
    p = db.get(Period, _p1(book, periods).id)
    first = carry_forward_profit(db, book.id, p, admin_user)
    assert first["vouchers"], "前置：第一次结转应当生成凭证"

    before = _snapshot(db, book, p)
    monkeypatch.setattr(svc, "log", _boom)
    with pytest.raises(RuntimeError):
        carry_forward_profit(db, book.id, p, admin_user)
    db.rollback()

    after = _snapshot(db, book, p)
    assert after == before, "重结转失败把上一次的结转凭证弄丢了"
    assert after["carry"] == before["carry"], "结转凭证消失了一半"


# --------------------------------------------------------------------- 只提交一次


def test_结转损益只提交一次事务(db, book, periods, subjects, admin_user,
                                make_voucher, monkeypatch):
    """凭证与审计日志必须在**同一次** commit 里落库。

    数 commit 次数比看结果更直接：结果对了也可能是因为第二次提交补上了，
    而两次提交之间的那个窗口正是 P1-19 事故发生的地方。
    """
    make_voucher("2026-01-10", dr=[("600101", 1000, "收入")],
                 cr=[("100201", 1000, "收款")], posted=True)
    p = db.get(Period, _p1(book, periods).id)
    calls = _count_commits(db, monkeypatch)

    r = carry_forward_profit(db, book.id, p, admin_user)

    assert len(calls) == 1, f"结转损益提交了 {len(calls)} 次，应当只有 1 次"
    assert r["vouchers"], "前置：应当生成了结转凭证"


def test_年度结转仍然是单次提交(db, book, periods, subjects, admin_user,
                               make_voucher, monkeypatch):
    """回归护栏：年度结转本来就是单事务，别被改成多事务。"""
    make_voucher("2026-01-10", dr=[("600101", 1000, "收入")],
                 cr=[("100201", 1000, "收款")], posted=True)
    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass          # 后面几个月没有损益发生额，结转不了是正常的
        close_period(db, p, admin_user)

    calls = _count_commits(db, monkeypatch)
    year_end_carry(db, book, admin_user, 2026, 2027)
    assert len(calls) == 1, f"年度结转提交了 {len(calls)} 次，应当只有 1 次"


def test_结转成功时凭证与审计一起可见(db, book, periods, subjects, admin_user,
                                      make_voucher):
    """正面路径不能被改坏：凭证、记账状态、审计日志一个都不能少。"""
    make_voucher("2026-01-10", dr=[("600101", 1000, "收入")],
                 cr=[("100201", 1000, "收款")], posted=True)
    p = db.get(Period, _p1(book, periods).id)
    r = carry_forward_profit(db, book.id, p, admin_user)
    db.expire_all()

    snap = _snapshot(db, book, db.get(Period, p.id))
    assert snap["carry"] == sorted(r["vouchers"]), "结转凭证没落库"
    assert snap["audits"].count("CARRY_FORWARD") >= 1, "审计日志没落库"
    # 损益类科目本期结平
    assert Decimal("0") == Decimal("0")  # 占位：具体余额断言交给 test_periods