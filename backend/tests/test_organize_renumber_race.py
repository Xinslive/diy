"""改号撞车的兜底（审计 P3-11）。

``organize._renumber`` 是「查一遍 → 算一遍 → 写两遍」，全程无锁：
``plan_organize`` 读到的号段是**那一刻**的快照，而并发进来的第二个整理会按
**它自己的**快照算出同一套目标号。两边后提交的那个撞上
``uq_voucher_period_no``（即时唯一约束），于是：

* **不会产生脏数据** —— 约束兜住了。这是「翻成人话」而不是「修数据错」的原因。
* 但撞出来的是一条裸的 ``IntegrityError``，走全局处理器只得到 500。
  用户刚点过「整理凭证」，屏幕上没有任何线索能让他知道是自己干的。

这里钉两件事：

1. ``apply_organize`` 会**锁期间行**（``FOR UPDATE``），把同一个期间的改号串行化。
   与 ``periods.ensure_periods`` 锁账套行是同一套写法：先锁后算，锁顺序一致。
2. 万一还是撞了（锁之前就有人插进来、或别的入口绕过锁），兜底路径给的是
   ``ValueError`` + 人话，而且回滚的只是这一批改号（savepoint），
   不连累调用方在同一事务里已经做完的事。

把「目标号已被占用」做成确定事件的办法是手写一份计划：``plan_organize``
会避开已记账凭证占着的号，所以真实流程走不到撞车那一步。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.accounting import organize as svc
from app.accounting.vouchers import create_voucher
from app.models import Voucher


def _mk(db, book, admin_user, subjects, no: str, day: int = 5):
    return create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, day),
        entries=[
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "100.00", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "100.00", "summary": "x"},
        ],
        voucher_no=no,
    )


def test_整理会锁住期间行(db, book, admin_user, subjects, make_voucher, monkeypatch):
    """锁的是**期间**行：整理是逐期发号的，锁期间就够把同一个号段串行化。

    必须造出一次**真的改号**（断号 记0003 → 压紧成 记0001）：号段本来就整齐时
    没有任何改动、没有锁，这条测试就变成在空集上跑。
    """
    _mk(db, book, admin_user, subjects, "记0003")
    db.commit()

    seen: list[str] = []
    real_execute = db.execute

    def spy(stmt, *a, **kw):
        sql = str(stmt)
        if "FOR UPDATE" in sql:
            seen.append(sql)
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", spy)
    result = svc.apply_organize(db, book_id=book.id, user=admin_user, period_id=None,
                                strategy="SORT_DATE")

    assert result["total_changed"] >= 1, "前置：这份号段本来该被压紧"
    assert any("FROM period" in s for s in seen), \
        f"整理凭证没有锁期间行，并发整理会各按各的快照算号：{seen}"


def test_锁必须在算方案之前拿到(db, book, admin_user, subjects, make_voucher, monkeypatch):
    """这条钉的是**顺序**，不是「有没有出现 FOR UPDATE」（审计 P2-1）。

    原实现是「先 plan_organize 算方案、再取行锁」，而 plan_organize 自己就是
    「读一遍算一遍」——锁拿到的时候方案早算完了，挡得住写、挡不住读。两个
    并发整理仍然各按各的快照算出同一套目标号。

    上面的 `test_整理会锁住期间行` 看着已经覆盖了这件事，其实没有：它只 spy
    「发出了 FOR UPDATE」，锁在方案之后拿它照样通过。这正是当初让并发修复
    误以为已经生效的原因 —— 所以这里断言的是**先锁后读这个次序本身**。

    做法：记录 execute 的调用序列，把那条锁语句和 plan_organize 的读语句
    各标一个序号，比较谁在前。
    """
    _mk(db, book, admin_user, subjects, "记0003")
    db.commit()

    order: list[str] = []
    real_execute = db.execute
    real_plan = svc.plan_organize

    def spy_execute(stmt, *a, **kw):
        sql = str(stmt)
        if "FOR UPDATE" in sql:
            order.append("lock")
        return real_execute(stmt, *a, **kw)

    def spy_plan(*a, **kw):
        order.append("plan")
        return real_plan(*a, **kw)

    monkeypatch.setattr(db, "execute", spy_execute)
    monkeypatch.setattr(svc, "plan_organize", spy_plan)

    svc.apply_organize(db, book_id=book.id, user=admin_user, period_id=None,
                       strategy="SORT_DATE")

    assert "lock" in order, f"没有取到期间行锁：{order}"
    assert "plan" in order, f"plan_organize 没有被调用，用例前提不对：{order}"
    assert order.index("lock") < order.index("plan"), (
        "锁取得太晚（在算方案之后）—— 它只挡得住写、挡不住读，"
        "并发整理仍会各按各的快照算出同一套目标号："
        f"{order}")


def test_指定期间时只锁那一期(db, book, admin_user, subjects, make_voucher, monkeypatch):
    """锁的粒度不能退化成「每次都锁整个账套」。

    不指定期间时锁全部（那是「哪些期会改动」在读凭证之前无从得知的代价），
    但指定了期间就该只锁那一期 —— 不同期间的整理仍然可以并行。
    """
    _mk(db, book, admin_user, subjects, "记0003")
    db.commit()

    seen: list[str] = []
    real_execute = db.execute

    def spy(stmt, *a, **kw):
        sql = str(stmt)
        if "FOR UPDATE" in sql:
            seen.append(sql)
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", spy)
    target = db.execute(select(svc.Period).where(svc.Period.book_id == book.id)).scalars().first()
    svc.apply_organize(db, book_id=book.id, user=admin_user, period_id=target.id,
                       strategy="SORT_DATE")

    locks = [s for s in seen if "FOR UPDATE" in s]
    assert locks, "没有取到期间行锁"
    assert any("period.id = " in s for s in locks), \
        f"指定了期间却没按 id 过滤，锁成了整个账套：{locks}"


def test_目标号被占时报人话而不是裸异常(db, book, admin_user, subjects):
    """兜底路径：数据库拒了也要说清是什么冲突、该怎么办。"""
    a = _mk(db, book, admin_user, subjects, "记0001")
    b = _mk(db, book, admin_user, subjects, "记0002")
    db.commit()

    plan = svc.PeriodPlan(period_id=a.period_id, label="2026-01")
    # 手写一份「撞车」的计划：把 a 改成 b 现在占着的号
    plan.changes = [
        svc.Change(a.id, a.voucher_no, b.voucher_no,
                   a.voucher_date.isoformat(), a.status, a.source),
    ]
    with pytest.raises(ValueError) as exc:
        svc._renumber(db, plan)

    detail = str(exc.value)
    assert "2026-01" in detail, f"没说清是哪个期间：{detail}"
    assert "占用" in detail or "凭证号" in detail, detail
    db.rollback()


def test_撞号不会把库里原有的号改坏(db, book, admin_user, subjects):
    """savepoint 回滚之后，库里的凭证号必须还是原来那张。"""
    a = _mk(db, book, admin_user, subjects, "记0001")
    b = _mk(db, book, admin_user, subjects, "记0002")
    db.commit()

    plan = svc.PeriodPlan(period_id=a.period_id, label="2026-01")
    plan.changes = [
        svc.Change(a.id, a.voucher_no, b.voucher_no,
                   a.voucher_date.isoformat(), a.status, a.source),
    ]
    with pytest.raises(ValueError):
        svc._renumber(db, plan)
    db.rollback()

    db.expire_all()
    assert db.get(Voucher, a.id).voucher_no == "记0001"
    assert db.get(Voucher, b.id).voucher_no == "记0002"
    # 也不能留下 ∥<id> 这种临时占位号 —— 它本来就不该出现在库里
    assert db.scalars(select(Voucher).where(
        Voucher.voucher_no.like(f"{svc.PARK_PREFIX}%"))).all() == []


def test_撞号之后同一个会话还能继续改号(db, book, admin_user, subjects):
    """回滚的只是这一批改号，不是调用方的整个事务。"""
    a = _mk(db, book, admin_user, subjects, "记0001")
    b = _mk(db, book, admin_user, subjects, "记0002")
    db.commit()

    plan = svc.PeriodPlan(period_id=a.period_id, label="2026-01")
    plan.changes = [
        svc.Change(a.id, a.voucher_no, b.voucher_no,
                   a.voucher_date.isoformat(), a.status, a.source),
    ]
    with pytest.raises(ValueError):
        svc._renumber(db, plan)

    # 同一个会话接着来一次正常改号：必须成功
    ok = svc.PeriodPlan(period_id=a.period_id, label="2026-01")
    ok.changes = [
        svc.Change(a.id, a.voucher_no, "记0005",
                   a.voucher_date.isoformat(), a.status, a.source),
    ]
    svc._renumber(db, ok)
    db.commit()
    db.expire_all()
    assert db.get(Voucher, a.id).voucher_no == "记0005"
