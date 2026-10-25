"""审计修复 §3.15：凭证修改路径的并发控制。

**这条缺陷是什么。**

原来「修改凭证」这条写路径上**没有任何并发控制**：

* 路由层 ``db.get(Voucher, id)`` 是普通 SELECT；
* 服务层 ``voucher.status`` 读的是**会话内那份可能已经陈旧**的属性；
* 库层 ``fn_posted_voucher_guard``（voucher 的 BEFORE UPDATE 行触发器）
  **只有当 UPDATE 语句真的产生时才执行** —— 而改分录走的是
  「DELETE 旧 entry 行 + INSERT 新 entry 行」，voucher 行往往**没有任何净变化**
  （status 本来就是 UNPOSTED，又被重新赋成 UNPOSTED，SQLAlchemy 眼里
  「没变」→ 根本不生成 UPDATE 语句），于是那道守卫**一次都没跑**。

三道闸在并发下同时落空，后果是：

* **A：已记账凭证的金额被静默改写。** 借贷平、试算平、科目余额、总账、
  「整理就绪」体检**全过** —— 数字是错的，但没有任何一处会报错。
* **B：同一张凭证留下 4 条分录**（两组重复的行号），同一笔钱记两遍，
  而借贷仍然平。
* **C：审计快照的 ``before`` 取自陈旧对象**，与事实不符 ——
  审计日志是「凭证号可复用」场景下唯一的事后追溯依据。

**这里怎么复现（确定性，不用线程）。**

真实交错是「B 的页面在 A 记账**之前**就打开了」：

    create(未记账) → B 读 → A 记账 → B 保存

而「B 读」这一步**不需要真的并发**：只要 B 的会话在 A 提交**之前**
把那行读进 identity map，B 会话里的 ``status`` 就是陈旧的 UNPOSTED。
本文件用两个**独立会话**（不是两个线程）把这个交错固定下来：

* 会话 A（``db`` 夹具）先读那张凭证 —— 它的 identity map 里是 UNPOSTED；
* 会话 B（另一个连接）把它记了账并提交；
* 会话 A 再来保存 —— 修复后必须被拒（它拿到行锁时会读到最新的 POSTED）。

**为什么不用线程/barrier。** 线程版的交错靠调度，偶发失败；这里靠
「会话 A 的 identity map 里那份陈旧值」把时序**钉死** —— 失败的不是 flaky，
是确定地错。这正是审计报告 §6.2 那句「错乱的交错不算竞态」的正面写法。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.accounting.vouchers import update_voucher
from app.core.db import SessionLocal
from app.models import AuditLog, Entry, Voucher


def _entries(subjects, dr, cr):
    out = [{"subject_id": subjects[c].id, "direction": "DEBIT", "amount": str(a), "summary": s}
           for c, a, s in dr]
    out += [{"subject_id": subjects[c].id, "direction": "CREDIT", "amount": str(a), "summary": s}
            for c, a, s in cr]
    return out


def _post_in_other_session(voucher_id: int, username: str = "甲") -> None:
    """在**另一个会话**里把这张凭证记账并提交。

    走的是与 ``post_vouchers`` 同样的库层动作（UNPOSTED → POSTED 一次状态
    跳转，``fn_posted_voucher_guard`` 允许），但绕开了「上一期必须已结账」
    那条前置 —— 那条与本文件要验的东西无关。
    """
    s = SessionLocal()
    try:
        s.execute(
            update(Voucher).where(Voucher.id == voucher_id).values(
                status="POSTED", posted_at=datetime.now(timezone.utc),
                posted_by=username))
        s.commit()
    finally:
        s.close()


def _amounts(db, voucher_id: int) -> list[tuple[int, str, Decimal]]:
    return [
        (e.line_no, e.direction, e.amount)
        for e in db.scalars(
            select(Entry).where(Entry.voucher_id == voucher_id)
            .order_by(Entry.line_no)).all()
    ]


# ------------------------------------------------- 场景 A：已记账凭证被并发改写


def test_已记账凭证被并发改写时修改被拒(db, book, subjects, make_voucher, admin_user):
    """B 的会话在 A 记账**之前**就拿到了这张凭证 —— 保存时必须被拒。

    修复前：会话里的 ``status`` 是陈旧的 UNPOSTED → 放行 → 分录被
    「删旧插新」成新金额，而 voucher 行**没有净变化** → 库层守卫一次没跑 →
    **已记账凭证的金额被静默改写，没有任何报错**。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 500, "x")], cr=[("5001", 500, "x")])
    before = _amounts(db, v.id)
    assert before == [(1, "DEBIT", Decimal("500.00")), (2, "CREDIT", Decimal("500.00"))]

    # 会话 A（db）此刻手里是 UNPOSTED —— 这就是「页面早就打开了」那一步
    assert v.status == "UNPOSTED"

    # 会话 B 记账并提交
    _post_in_other_session(v.id)
    assert db.scalar(select(Voucher.status).where(Voucher.id == v.id)) == "POSTED"

    # 会话 A 拿自己那份**陈旧**的 status 去保存
    with pytest.raises(ValueError, match="已记账"):
        update_voucher(db, v, admin_user, {
            "entries": _entries(subjects, [("1001", 5000, "改")], [("5001", 5000, "改")]),
        })
    db.rollback()

    # 账上的数一个字节都没动
    db.expire_all()
    assert _amounts(db, v.id) == before, "已记账凭证的金额被改写了"
    assert db.scalar(select(Voucher.status).where(Voucher.id == v.id)) == "POSTED"


def test_并发改同一张凭证不会留下重复分录(db, book, subjects, make_voucher, admin_user):
    """场景 B：修复前两张并发编辑会在这张凭证上留下 **4 条**分录。

    两组重复的行号、同一笔钱记两遍，而**借贷仍然平** ——
    库层的平衡触发器、试算平衡、科目余额、总账、「整理就绪」体检全过。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 1000, "x")], cr=[("5001", 1000, "x")])
    _post_in_other_session(v.id)

    with pytest.raises(ValueError, match="已记账"):
        update_voucher(db, v, admin_user, {
            "entries": _entries(subjects, [("1001", 2000, "改")], [("5001", 2000, "改")]),
        })
    db.rollback()

    db.expire_all()
    rows = _amounts(db, v.id)
    assert len(rows) == 2, f"这张凭证上有 {len(rows)} 条分录（同一笔钱记了两遍）: {rows}"
    assert [r[0] for r in rows] == [1, 2], f"行号重复了: {rows}"


def test_审计快照取的是锁后的真实内容(db, book, subjects, make_voucher, admin_user):
    """场景 C：``before`` 快照必须在**锁到行之后**取，否则它记的是不存在的事。

    审计日志是「凭证号可复用」场景下唯一的事后追溯依据
    （``app/accounting/audit_helper.py`` 的立论）：快照是假的，那条立论就废了。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    stale_remark = v.remark

    # 会话 B 改了备注并提交；会话 A 手里的还是旧的
    s = SessionLocal()
    try:
        s.execute(update(Voucher).where(Voucher.id == v.id).values(remark="别人改过的备注"))
        s.commit()
    finally:
        s.close()
    assert v.remark == stale_remark, "前提不成立：会话 A 应当还拿着旧值"

    update_voucher(db, v, admin_user, {"remark": "我这边的备注"})

    row = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity == "voucher", AuditLog.entity_id == str(v.id))
        .order_by(AuditLog.id.desc())).first()
    assert row is not None, "修改凭证必须留审计"
    assert row.before["remark"] == "别人改过的备注", (
        f"审计快照的 before 取自陈旧对象：{row.before['remark']!r}")


# ------------------------------------------------- 库层两道护栏（绕过应用层也拦得住）


def test_库层拒绝同一张凭证里重复的行号(db, book, subjects, make_voucher):
    """``uq_entry_voucher_line_no``：重复行号在库层变成**看得见**的失败。

    没有这条约束时，重复行号能静默入库（场景 B 的后果），
    而借贷仍然平 —— 所有自检都查不出来。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    first = db.scalar(
        select(Entry).where(Entry.voucher_id == v.id).order_by(Entry.line_no))
    db.add(Entry(
        voucher_id=v.id, line_no=first.line_no, book_id=0, period_ord=0,
        subject_id=subjects["1001"].id, direction="DEBIT", amount=Decimal("1"),
        summary="重复的行号"))
    with pytest.raises(IntegrityError) as ei:
        db.flush()
    assert "uq_entry_voucher_line_no" in str(ei.value), str(ei.value)
    db.rollback()


def test_库层拒绝改已记账凭证的分录(db, book, subjects, make_voucher):
    """绕过应用层直接改库：已记账凭证的分录**不许动**。

    这条护栏挂在 ``entry`` 上（``fn_entry_lock_guard``）——
    因为 ``fn_posted_voucher_guard`` 只挂在 ``voucher`` 的 BEFORE UPDATE 上，
    而「改凭证」走的是删旧插新，**那张 voucher 行可能根本没有 UPDATE 语句**
    → 守卫一次都不会跑。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                     posted=True)
    e = db.scalar(select(Entry).where(Entry.voucher_id == v.id).order_by(Entry.line_no))
    with pytest.raises(IntegrityError) as ei:
        db.execute(update(Entry).where(Entry.id == e.id).values(amount=Decimal("999")))
    assert "已记账" in str(ei.value), str(ei.value)
    db.rollback()


def test_库层拒绝往已记账凭证里插分录(db, book, subjects, make_voucher):
    """插进去也一样要拒 —— 总账是按 entry 求和的（不看行号）。

    往一张已记账的凭证里**插**一对借贷分录，账面上的数就变了，
    而那张凭证仍然是「已记账」。只堵 DELETE/UPDATE 是漏的。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                     posted=True)
    with pytest.raises(IntegrityError) as ei:
        db.execute(text("""
            INSERT INTO entry (voucher_id, book_id, period_ord, line_no,
                               subject_id, direction, amount, summary)
            VALUES (:v, 0, 0, 99, :s, 'DEBIT', 1, '硬塞')
        """), {"v": v.id, "s": subjects["1001"].id})
    assert "已记账" in str(ei.value), str(ei.value)
    db.rollback()


def test_库层拒绝删已记账凭证的分录(db, book, subjects, make_voucher):
    """删也一样 —— 「已记账 = 已进账簿，数字已被总账引用」。"""
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                     posted=True)
    e = db.scalar(select(Entry).where(Entry.voucher_id == v.id).order_by(Entry.line_no))
    with pytest.raises(IntegrityError) as ei:
        db.execute(text("DELETE FROM entry WHERE id = :i"), {"i": e.id})
    assert "已记账" in str(ei.value), str(ei.value)
    db.rollback()


def test_结转凭证的分录仍然可以整张重做(db, book, subjects, make_voucher):
    """护栏不许**削弱**已有的例外：结转凭证由系统生成，
    「重新结转损益」要能把它整张删掉重做（``periods._drop_carry_forward``）。

    删的是**整张凭证的分录**（借贷两边一起删）—— 删一半会让凭证不平，
    那道延迟平衡触发器会挡下来（那是另一条护栏，与本条无关）。
    """
    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                     posted=True, source="CARRY_FORWARD")
    # 删得掉（系统重做结转凭证时走的就是这条路）
    db.execute(text("DELETE FROM entry WHERE voucher_id = :v"), {"v": v.id})
    db.commit()
    assert db.scalar(
        select(func.count()).select_from(Entry).where(Entry.voucher_id == v.id)) == 0
