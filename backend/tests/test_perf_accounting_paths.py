"""账簿写入路径的性能守卫（审计 2026-10-14：严重 4 / 中等 2 / 中等 3，及严重 4
附带的那条快照懒加载）。

这一轮修的是四件事，全都在**写入**这条最热的路径上：

* **严重 4** 凭证取号原来把该期间**全部**凭证号拉回 Python 求 max。批量导入
  在一个事务里逐张 ``create_voucher``（``import_max_vouchers`` 允许 5000 张），
  于是 N 张累计读回约 N(N−1)/2 行。改成 SQL 聚合：**按整数比**这个正确性
  决策原样保留（PG 对 text 的 max 是字典序，退回去就是「记100 → 记001」），
  退回应用层的只有一行。
* **中等 3** 批量记账逐张懒加载 ``v.entries``，一次批量 = N 次往返。
* **中等 2** 结账沿期间链逐行 ``db.get``，外加 ``check_period`` 把整本账的
  凭证建成完整 ORM 对象。
* 顺带一条（报告里记在严重 4 的描述里：「单张 create_voucher 固定 4 次
  SELECT + 1 次 voucher_snapshot 懒加载」）：新建凭证的审计快照因为分录只设了
  外键、回填不了 backref，``voucher.entries`` 每建一张就懒加载一次。

断言的是**查询次数**（``before_cursor_execute``，写法照 ``test_ledger_query_count.py``）
而不是「结果对不对」—— 结果对而慢，用户一样会骂；但凡是碰了正确性的地方，
另配一条行为不变性的断言，两者都要。
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date

import pytest
import sqlalchemy
from sqlalchemy import select, update

from app.accounting import organize as svc_organize
from app.accounting import periods as svc_periods
from app.accounting import vouchers as svc
from app.accounting.audit_helper import voucher_snapshot
from app.core.db import SessionLocal
from app.core.db import engine as app_engine
from app.models import AuditLog, Period, Voucher


@contextmanager
def _statements():
    """收集本次调用期间真正发出去的 SQL。"""
    stmts: list[str] = []
    handler = lambda conn, cursor, statement, params, ctx, many: stmts.append(statement)
    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        yield stmts
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)


@pytest.fixture
def period(db, book, periods):
    return db.get(Period, periods[2].id)


@pytest.fixture
def seed_nos(db, book, period, admin_user):
    """把该期间清空，塞进指定的凭证号（只占号，不建分录）。"""
    def _seed(nos):
        for v in db.scalars(select(Voucher).where(Voucher.period_id == period.id)).all():
            db.delete(v)
        db.flush()
        for n in nos:
            db.add(Voucher(book_id=book.id, period_id=period.id, voucher_no=n,
                           voucher_date=date(period.year, period.month, 1),
                           status="UNPOSTED",
                           source="MANUAL", created_by=admin_user.username))
        db.flush()
        return _seed
    return _seed


def _day(period: Period, day: int) -> date:
    """期间内的一天。凭证日期必须落在所属期间里 —— DB 上有触发器盯着。"""
    return date(period.year, period.month, day)


def _pairs(subjects, amount="10"):
    return [
        {"subject_id": subjects["1002"].id, "direction": "DEBIT",
         "amount": amount, "summary": "x"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": amount, "summary": "x"},
    ]


def _lock_prior_periods(db, book, up_to_ord):
    """把目标期间之前的期间标成已结账 —— 与 conftest.make_voucher 同一套前置。"""
    db.execute(
        update(Period).where(
            Period.book_id == book.id, Period.ord < up_to_ord, Period.locked.is_(False)
        ).values(locked=True)
    )
    db.commit()
    # 批量 UPDATE 与 ORM 会话之间的同步是实现细节，别让本用例的结论依赖它：
    # 主动过期，往后读到的就是库里那份。
    db.expire_all()


# ----------------------------------------------------------------- 严重 4：取号


def _no_aggregates(stmts) -> list[str]:
    """取号该发的那条：按前缀取最大序号的聚合。"""
    return [s for s in stmts if "FROM voucher" in s and "max(" in s.lower()]


def _whole_period_readback(stmts) -> list[str]:
    """把整期凭证号一条条选回来的语句 —— 原来的实现就是这条。"""
    return [s for s in stmts
            if re.search(r"SELECT\s+voucher\.voucher_no\b", s, re.I)
            and "FROM voucher" in s]


def test_发号只发一条聚合查询(db, period, seed_nos):
    """原来是一条 ``SELECT voucher_no FROM voucher WHERE period_id=…``，
    整期的号原样回传、在 Python 里求 max；现在是一次聚合、一行。"""
    seed_nos(["记001", "记002", "记010"])
    with _statements() as stmts:
        no = svc.next_voucher_no(db, period)
    assert no == "记011"
    assert len(stmts) == 1, f"发号应当只有一条语句，实际 {len(stmts)} 条：{stmts}"
    assert len(_no_aggregates(stmts)) == 1
    assert not _whole_period_readback(stmts), "仍然在把整期凭证号拉回 Python"


@pytest.mark.parametrize("size", [1, 30])
def test_发号查询数不随本期凭证数增长(db, period, seed_nos, size):
    """N 张累计读回 N(N−1)/2 行的问题。只钉语句数不够 —— 整表拉回本来
    就是**一条**语句，真正变掉的是「这条语句回传多少行」，所以同时要求它是聚合。"""
    seed_nos([f"记{i:03d}" for i in range(1, size + 1)])
    with _statements() as stmts:
        no = svc.next_voucher_no(db, period)
    assert no == f"记{size + 1:03d}"
    assert len(stmts) == 1, f"size={size} 时发号发了 {len(stmts)} 条：{stmts}"
    assert "max(" in stmts[0].lower() and "substring(" in stmts[0].lower()


def test_建凭证时的取号不再整表拉回(db, book, period, subjects, admin_user):
    """``create_voucher`` 里的取号语句也要是聚合，否则批量导入还是 O(N²)。"""
    with _statements() as stmts:
        svc.create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=_day(period, 5),
            entries=_pairs(subjects), commit=False)
    assert not _whole_period_readback(stmts), "create_voucher 仍在整表拉取凭证号"
    assert len(_no_aggregates(stmts)) == 1


def test_记9记10与手工号混存时下一个号仍是记011(db, period, seed_nos):
    """字典序在这里必翻车：``记-年结`` > ``记9`` > ``记10``。

    手工号「记-年结」「2026年终」解析不出序号，本来就不该参与递增。
    """
    seed_nos(["记9", "记10", "记-年结", "2026年终"])
    assert svc.next_voucher_no(db, period) == "记011"


def _reference_next_no(nos, prefix="记") -> str:
    """修复前的应用层实现，逐字照抄 —— 用来证明两条路径结果一致。"""
    from app.accounting.organize import split_no

    seq = max((n for no in nos
               if (n := split_no(no)[1]) > 0 and split_no(no)[0] == prefix),
              default=0) + 1
    return f"{prefix}{seq:03d}"


@pytest.mark.parametrize("nos,prefix", [
    ([], "记"),
    (["记9", "记10", "记-年结", "2026年终"], "记"),
    (["记100", "记99"], "记"),                      # 字典序在这里会输
    (["记999"], "记"),
    (["记0"], "记"),                                # 序号 0 不该顶号
    (["记0", "记005"], "记"),
    (["记账凭证009"], "记账凭证"),
    (["记账凭证009"], "记"),                        # 前缀不同就是另一个号段
    (["银009", "记001"], "银"),
    (["记-2", "记-年结"], "记"),
    (["记 005"], "记"),                             # 空格会被吃进前缀组，落选
    ([" 记006 "], "记"),                            # 两头的空白要被 strip 掉
    (["记99999999999999"], "记"),                   # 超 int4：Numeric 不溢出
])
def test_与修复前的应用层取号逐例一致(db, period, seed_nos, nos, prefix):
    """下推 SQL 是**性能**改动，正确性必须逐字节等价于原来的实现。"""
    seed_nos(nos)
    assert svc.next_voucher_no(db, period, prefix) == _reference_next_no(nos, prefix)


@pytest.mark.parametrize("prefix", ["", "9记", "记账凭证长"])
def test_前缀拆不出来时直接发001(db, period, seed_nos, prefix):
    """NO_PATTERN 的前缀组是 ``[^\\d]{1,4}``：含数字、超 4 字、空前缀的
    ``prefix`` 永远匹配不到任何凭证号，答案与库里有什么无关。"""
    seed_nos([f"{prefix}001" if prefix else "记001"])
    assert svc.next_voucher_no(db, period, prefix) == f"{prefix}001"


def test_带符号的前缀仍按前缀发号(db, period, seed_nos):
    """「记-」是 ``[^\\d]{1,4}`` 能拆出来的前缀，就不是「拆不出来」那一类，
    它有自己独立的号段（别与「记」混算）。"""
    seed_nos(["记001", "记-001"])
    assert svc.next_voucher_no(db, period, "记-") == "记-002"
    assert svc.next_voucher_no(db, period, "记") == "记002"


def test_各前缀发号互不干扰(db, period, seed_nos):
    seed_nos(["记001", "银005", "银存001"])
    assert svc.next_voucher_no(db, period, "记") == "记002"
    assert svc.next_voucher_no(db, period, "银") == "银006"
    assert svc.next_voucher_no(db, period, "银存") == "银存002"


# ----------------------------------------------------------------- 中等 3：分录预载


def _entry_reads(stmts) -> list[str]:
    return [s for s in stmts if "FROM entry" in s]


def test_批量记账只读一次分录(db, book, period, subjects, admin_user):
    """``_resolve_vouchers`` 原来用裸 ``select(Voucher)``，随后逐张遍历
    ``v.entries`` —— 一次批量记账 = N 次往返（列表查询早就补过 selectinload）。"""
    vs = [svc.create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=_day(period, day),
        entries=_pairs(subjects), commit=False) for day in range(1, 9)]
    db.commit()
    _lock_prior_periods(db, book, period.ord)

    ids = [v.id for v in vs]
    with _statements() as stmts:
        count = svc.post_vouchers(db, book.id, ids, admin_user, commit=False)

    assert count == len(ids)
    assert len(_entry_reads(stmts)) == 1, (
        f"一次批量记账只该预载一次分录，实际 {len(_entry_reads(stmts))} 次："
        f"{_entry_reads(stmts)}")
    for v in vs:
        assert v.status == "POSTED"


def test_批量反记账行为不变(db, book, period, subjects, admin_user):
    """预载对不读分录的调用方只是多传一份数据，状态机必须一模一样。"""
    vs = [svc.create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=_day(period, day),
        entries=_pairs(subjects), commit=False) for day in range(1, 4)]
    db.commit()
    ids = [v.id for v in vs]
    db.execute(update(Voucher).where(Voucher.id.in_(ids))
               .values(status="POSTED", posted_by=admin_user.username))
    db.commit()

    assert svc.unpost_vouchers(db, book.id, ids, admin_user, commit=False) == 3
    for v in vs:
        assert v.status == "UNPOSTED"


# ------------------------------------------------------- 审计快照的懒加载（严重 4 附带）


def test_新建凭证不再回查分录(db, book, period, subjects, admin_user):
    """分录是 ``Entry(voucher_id=…, line_no=i, **e)`` 建的，backref 不回填，
    ``voucher.entries`` 于是每建一张就懒加载一次 —— 拿回来的正是刚写进去的东西。"""
    with _statements() as stmts:
        svc.create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=_day(period, 8),
            entries=_pairs(subjects, amount="12.5"), commit=False)
    assert not _entry_reads(stmts), f"新建凭证仍然回查了分录：{_entry_reads(stmts)}"


def test_新建凭证的审计快照内容不变(db, book, period, subjects, admin_user):
    """快照改从 payload 构造，必须与「走 ORM 关系读出来」的旧结果逐字段相同。"""
    v = svc.create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=_day(period, 8),
        entries=_pairs(subjects, amount="12.5"), remark="快照守卫",
        attachments=2, commit=False)
    db.flush()

    logged = db.scalars(
        select(AuditLog).where(
            AuditLog.entity == "voucher", AuditLog.action == "CREATE",
            AuditLog.entity_id == str(v.id))
    ).one()

    # 期望值故意走 ORM：voucher.entries 这时才真的从库里读出来。
    db.expire(v, ["entries"])
    assert logged.after == voucher_snapshot(v)
    assert logged.after["debit"] == "12.50"
    assert [e["line_no"] for e in logged.after["entries"]] == [1, 2]


def test_审计快照金额是文本且科目齐全(db, book, period, subjects, admin_user):
    """金额存十进制文本（``_jsonable`` 的口径），快照不能因为换了构造方式而变形态。"""
    v = svc.create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=_day(period, 8),
        entries=_pairs(subjects, amount="3"), commit=False)
    db.flush()
    logged = db.scalars(
        select(AuditLog).where(
            AuditLog.entity == "voucher", AuditLog.action == "CREATE",
            AuditLog.entity_id == str(v.id))
    ).one()
    assert [e["amount"] for e in logged.after["entries"]] == ["3.00", "3.00"]
    assert [e["summary"] for e in logged.after["entries"]] == ["x", "x"]
    assert logged.after["entries"][0]["subject_id"] == subjects["1002"].id
    assert logged.after["entries"][1]["direction"] == "CREDIT"


# ----------------------------------------------------------------- 中等 2：期间链


def test_结账不再逐行取期间(db, book, periods, admin_user):
    """原来 ``db.get`` 每个期间一条 SELECT，十年账套结一次 12 月就是 24 次。
    现在是一条 ``id IN (…)``。"""
    with _statements() as stmts:
        svc_periods.close_period(db, db.get(Period, periods[3].id), admin_user,
                                 auto_post=False, commit=False)

    # 整列的 Period 实体查询 + 单 id 谓词 = 原来那条 ``db.get``。
    # FROM 后面必须正好是 period：``period_opening JOIN period`` 那条里也
    # 会出现 period.id = 与 period.ord，但它不是按主键取期间。
    per_row = [s for s in stmts
               if re.search(r"FROM\s+period\s", s, re.I)
               and "period.id = " in s and "period.ord" in s]
    assert not per_row, f"仍然在逐行取期间：{per_row}"
    assert any(re.search(r"FROM\s+period\s", s, re.I) and "period.id IN" in s
               for s in stmts), "没有看到那条按 id 集合批量取期间的查询"


def test_结账的顺带结账与版本号不变(db, book, periods, admin_user):
    """链上每一期都锁、version 各加 1、结果里的「顺带结账」列表照旧。"""
    before = {m: periods[m].version for m in (1, 2, 3)}

    res = svc_periods.close_period(db, db.get(Period, periods[3].id), admin_user,
                                   auto_post=False, commit=False)

    assert res["period"] == periods[3].code
    assert res["auto_closed_periods"] == [periods[1].code, periods[2].code]
    db.commit()
    for m, ver in before.items():
        p = db.get(Period, periods[m].id)
        assert p.locked is True, f"{m} 月应当被顺带结账"
        assert p.version == ver + 1, f"{m} 月的 version 应当 +1"
        assert p.closed_by is not None


def test_结账后_identity_map_里的期间对象是库里那份(db, book, periods, admin_user):
    """``populate_existing`` 的意义。

    另一个连接把 2 月结掉了，本会话 identity map 里的 2 月对象还是「未结账」。
    ``chain`` 里那个对象正好是它 —— 不刷新的话，结完账之后调用方手上仍然是
    旧值（``_fresh_period`` 注释里绕开的正是同一个坑）。
    """
    stale = db.get(Period, periods[2].id)
    assert stale.locked is False

    other = SessionLocal()
    try:
        other.execute(
            update(Period).where(Period.id == periods[2].id)
            .values(locked=True, closed_by="别人", version=periods[2].version + 1)
        )
        other.commit()
    finally:
        other.close()

    svc_periods.close_period(db, db.get(Period, periods[3].id), admin_user,
                             auto_post=False, commit=False)

    assert stale.locked is True, "链上的期间对象必须是库里刷新过的那一份"
    assert stale.closed_by == "别人"


def test_批量反结账结果不变(db, book, periods, admin_user, make_voucher):
    # 走资产科目而不是损益科目：结账要求链上每一期损益都结平，
    # 而这条用例要验的是反结账，不是结转损益。
    vs = [make_voucher(f"2026-0{m}-10", dr=[("1122", 100, "挂应收")],
                       cr=[("1002", 100, "收款")])
          for m in (1, 2)]
    for m in (1, 2, 3):
        svc_periods.close_period(db, db.get(Period, periods[m].id), admin_user,
                                 commit=False)
    db.commit()
    assert all(v.status == "POSTED" for v in vs)

    res = svc_periods.batch_unclose(db, book.id, db.get(Period, periods[1].id),
                                    admin_user, commit=False)

    assert res["periods"] == [periods[1].code, periods[2].code, periods[3].code]
    assert res["unposted_vouchers"] == 2
    for m in (1, 2, 3):
        p = db.get(Period, periods[m].id)
        assert p.locked is False
        assert p.closed_by is None
    for v in vs:
        assert v.status == "UNPOSTED"


# ----------------------------------------------------------------- 中等 2：体检


def test_体检只查三列不再实例化凭证对象(db, period, seed_nos):
    seed_nos(["记001", "记002"])
    with _statements() as stmts:
        svc_organize.check_period(db, period)
    loads = [s for s in stmts if "FROM voucher" in s]
    assert loads, "体检总得读一次凭证"
    for s in loads:
        assert "voucher.remark" not in s and "voucher.attachments" not in s, (
            f"体检又在实例化完整的 Voucher：{s}")


def test_体检仍然拦下断号与手工号(db, period, seed_nos):
    seed_nos(["记001", "记003", "记-年结"])
    rep = svc_organize.check_period(db, period)
    assert rep["gaps"] == ["记002"]
    assert rep["unparsed"] == ["记-年结"]
    assert rep["clean"] is False
    assert any("断号" in b for b in rep["blocking"])
    assert any("凭证号不是「凭证字+数字」" in b for b in rep["blocking"])


def test_体检仍然拦下时序不对(db, period, seed_nos):
    """号大的那张日期反而更早 —— 改用元组之后这条判据不能失效。"""
    seed_nos(["记001", "记002"])
    for no, d in (("记001", _day(period, 9)), ("记002", _day(period, 1))):
        db.execute(update(Voucher).where(
            Voucher.period_id == period.id, Voucher.voucher_no == no
        ).values(voucher_date=d))
    db.flush()

    rep = svc_organize.check_period(db, period)
    assert rep["clean"] is False
    assert any("时序不对" in b for b in rep["blocking"])
    assert rep["time_out_of_order"] == [{
        "voucher_no": "记002", "voucher_date": _day(period, 1).isoformat(),
        "prev_no": "记001", "prev_date": _day(period, 9).isoformat(),
    }]


def test_体检记录已记账凭证(db, period, seed_nos):
    seed_nos(["记001", "记002"])
    db.execute(
        update(Voucher).where(
            Voucher.period_id == period.id, Voucher.voucher_no == "记002")
        .values(status="POSTED"))
    db.flush()
    rep = svc_organize.check_period(db, period)
    assert rep["posted"] == ["记002"]
    assert rep["total"] == 2
    assert rep["clean"] is True


def test_体检对干净的号段给出干净结论(db, period, seed_nos):
    seed_nos(["记001", "记002"])
    rep = svc_organize.check_period(db, period)
    assert rep["duplicates"] == []
    assert rep["prefixes"] == ["记"]
    assert rep["clean"] is True


def test_体检挂在空期间上不炸(db, period):
    rep = svc_organize.check_period(db, period)
    assert rep["total"] == 0
    assert rep["clean"] is True
    assert rep["period"] == f"{period.year}-{period.month:02d}"