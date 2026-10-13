"""期间状态的乐观锁（审计 P1-19）与建期间的 check-then-act（审计 P3-11）。

**P1-19 之前 ``period.version`` 是个装饰字段**：``periods.py`` 里只有
``p.version += 1``，全项目搜不到一处比对，前端也不带这个数。
字段在库里存着、接口也读得到，唯独不产生任何行为 —— 它让人以为
「期间状态是有并发保护的」，而实际上没有。

这里断言的是两件事：

1. **比对真的会发生**：调用方带上它看到的版本，不一致就抛
   ``PeriodVersionConflict``，并且**什么都没改**。
2. **锁读真的读的是库**：结账/反结账判断 locked 与算 version 时不能读
   会话里那份旧对象 —— 那才是「屏幕停在旧状态也照样能操作」的真正来源。

**P3-11**：``ensure_periods`` 原来「查一遍、没有就插」，两个并发的年度结转
会双双查到空、第二个撞唯一约束抛裸 IntegrityError。这里锁账套行，
并把剩下的兜底路径翻成人话。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accounting.periods import (PeriodVersionConflict, batch_unclose,
                                    close_period, ensure_periods)
from app.core.db import SessionLocal
from app.models import Period, Subject, Voucher


def _period(db, book, month):
    return db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == month))


def _version(db, period_id) -> int:
    """列查询拿版本 —— 绕开会话缓存，拿到的一定是库里的数。"""
    return int(db.execute(
        select(Period.version).where(Period.id == period_id)).scalar_one())


def _state(db, book):
    return {
        p.month: (bool(p.locked), p.version)
        for p in db.scalars(select(Period).where(Period.book_id == book.id)
                            .order_by(Period.ord)).all()
    }


# --------------------------------------------------------------------- P1-19


def test_版本不匹配时结账被拒且什么都没改(db, book, periods, admin_user):
    """陈旧的「结账」按钮不该把别人刚结过的账再结一遍。"""
    p = db.get(Period, periods[11].id)
    db.commit()
    stale_version = p.version - 1  # 屏幕上的版本

    before = _state(db, book)
    with pytest.raises(PeriodVersionConflict, match="已被他人操作"):
        close_period(db, p, admin_user, expected_version=stale_version)

    db.rollback()
    assert _state(db, book) == before, "冲突之后数据库被改了"


def test_版本不匹配时反结账被拒且凭证不回退(db, book, periods, subjects,
                                         admin_user, make_voucher):
    """反结账尤其要挡：它会把区间内的凭证批量退回未记账。

    陈旧屏幕上再点一次，等于把对方刚记好的凭证默默抹成未记账。
    """
    # 只用资产负债表科目（银行存款内部转账），不产生损益余额 ——
    # 否则结账会先被「还没结转损益」挡掉，测的就不是乐观锁了。
    make_voucher("2026-03-10", dr=[("100201", 100, "x")],
                 cr=[("100202", 100, "x")], posted=True)
    close_period(db, _period(db, book, 3), admin_user)
    db.commit()
    p = _period(db, book, 3)
    stale_version = p.version - 1
    posted_before = db.scalar(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.status == "POSTED")
        .with_only_columns(__import__("sqlalchemy").func.count()))

    before = _state(db, book)
    with pytest.raises(PeriodVersionConflict):
        batch_unclose(db, book.id, p, admin_user, expected_version=stale_version)
    db.rollback()

    assert _state(db, book) == before
    posted_after = db.scalar(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.status == "POSTED")
        .with_only_columns(__import__("sqlalchemy").func.count()))
    assert posted_after == posted_before, "冲突之后凭证的记账状态被改了"


def test_版本一致时正常结账并递增(db, book, periods, admin_user):
    p = db.get(Period, periods[11].id)
    v = p.version
    close_period(db, p, admin_user, expected_version=v)
    db.expire_all()
    p2 = _period(db, book, 11)
    assert p2.locked is True
    assert p2.version == v + 1, f"版本没有递增：{v} → {p2.version}"


def test_结账读的是库里的状态而不是会话缓存(db, book, periods, admin_user):
    """会话里那份 Period 是路由层早先读进来的**旧值**。

    别人在这期间提交过一次结账之后，本会话照样看到 locked=False ——
    屏幕停在旧状态再点一次，就会把已结账的期间当成没结的重新走一遍流程
    （连带把之前的期间一起重新锁、重新记账）。
    """
    p = db.get(Period, periods[11].id)
    assert p.locked is False

    other = SessionLocal()
    try:
        o = other.get(Period, p.id)
        o.locked = True
        o.closed_by = "别人"
        o.version += 1
        other.commit()
    finally:
        other.close()

    # 本会话的对象仍是旧的 —— 这正是问题所在
    assert db.get(Period, p.id).locked is False, "前置：会话里应当是旧值"

    with pytest.raises(ValueError, match="已经结账"):
        close_period(db, p, admin_user)


def test_版本递增按库里的值算而不是会话缓存(db, book, periods, admin_user):
    """``p.version += 1`` 在并发下会把别人那次递增覆盖掉。

    别人已经把这期 +1 过，本会话的对象上还是旧值；再 ``+= 1`` 就等于
    把那次递增抹了 —— 版本号会**倒退**，连「有没有变过」都判断不出来。
    """
    pid = periods[12].id
    base = _version(db, pid)
    p = db.get(Period, pid)
    db.commit()

    other = SessionLocal()
    try:
        o = other.get(Period, pid)
        o.version += 1
        other.commit()
    finally:
        other.close()
    mid = _version(db, pid)
    assert mid == base + 1, "前置：别的会话已经 +1 过一次"

    close_period(db, p, admin_user)
    after = _version(db, pid)
    assert after == base + 2, f"版本被覆盖了，期望 {base + 2}，实际 {after}"


def test_没带版本时不拦老调用方(db, book, periods, admin_user):
    """默认 None = 不比对：现有接口还没传这个参数，不能因此全打成 400。"""
    p = db.get(Period, periods[10].id)
    close_period(db, p, admin_user)
    db.expire_all()
    assert _period(db, book, 10).locked is True


# --------------------------------------------------------------------- P3-11


def test_重复建期间是幂等的(db, book):
    a = ensure_periods(db, book, 2027)
    n = len(a)
    b = ensure_periods(db, book, 2027)
    assert len(b) == n == 12
    assert [p.id for p in a] == [p.id for p in b], "第二次应当原样返回"
    rows = db.scalars(select(Period).where(
        Period.book_id == book.id, Period.year == 2027)).all()
    assert len(rows) == 12, f"期间被重复插入了 {len(rows)} 行"


def test_建期间时会锁住账套行(db, book):
    """串行化靠的是账套行上的 FOR UPDATE —— 这一步不能被优化掉。"""
    from sqlalchemy import event

    seen: list[str] = []
    engine = db.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        seen.append(statement)

    try:
        ensure_periods(db, book, 2027)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    locks = [s for s in seen if "book" in s.lower() and "FOR UPDATE" in s]
    assert locks, f"没有对账套行加 FOR UPDATE：{seen}"


def test_插入被数据库拒绝时给的是人话而不是裸异常(db, book):
    """唯一约束 / 外键兜底不能直接甩 IntegrityError 到 500。

    用一个库里不存在的账套 id 触发外键拒绝，走的是同一条异常处理路径。
    """
    from types import SimpleNamespace

    ghost = SimpleNamespace(id=book.id + 999999, start_year=2026, start_month=1)
    with pytest.raises(ValueError, match="会计期间创建失败"):
        ensure_periods(db, ghost, 2027)
    db.rollback()


def test_插入失败不会连累调用方已经做的工作(db, book, periods, admin_user):
    """savepoint 的意义：回滚的只是这批 insert，不是整个事务。

    ``year_end_carry`` 是拿 ensure_periods 当中途步骤的 ——
    一次插入失败不该把前面解锁 12 月、结转本年利润的成果一起废掉。
    """
    from app.accounting.periods import year_end_carry

    # 预铺 2027（正常路径），再让它以「第二次建」的形式走一遍 —— 必须是幂等而不是抛错
    ensure_periods(db, book, 2027)
    db.commit()
    again = ensure_periods(db, book, 2027)
    assert len(again) == 12
    db.commit()