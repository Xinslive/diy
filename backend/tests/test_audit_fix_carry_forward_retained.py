"""审计修复 §3.16：年度结转在缺「未分配利润」科目时**不许静默跳过**。

**这条缺陷是什么。**

``app/accounting/periods.py`` 的年度结转里有一段：

    profit, _ = resolve_profit_subject(...)
    retained = _subject_by_any_code(..., RETAINED_SUBJECT_CODES)
    if profit and retained:          # ← retained 是 None 时，整段被跳过
        ...「结转本年利润 → 未分配利润」...

``retained`` 找不到时，那一整段**静默跳过**：没有报错、没有日志、没有审计，
接口照样返回 200、界面照样显示「结转成功」。后果是：

* 本年利润的余额被 ``_refresh_opening`` **原样搬进下一年**；
* 资产负债表的「未分配利润」**恒为 0**；
* 历年累计额在「本年利润」里越堆越多，下一年再结转时同样被跳过，余额继续滚。

**为什么这一侧原来没有兜底、那一侧有。** ``profit``（本年利润）有多编码
兜底（``PROFIT_SUBJECT_CODES = ("3103", "4103")``），``retained``
（未分配利润）当时**只有一个条件、没有兜底** —— 这个不对称就是根因。

**为什么默认科目表不触发。** 内置用友科目表含 310415 未分配利润；
触发条件是用户**导入自有科目表** —— 而科目导入是系统明确支持的功能。
本文件就把那个科目拆掉，复现审计报告里的那条实测输出。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.accounting.periods import (RETAINED_SUBJECT_CODES, carry_forward_profit,
                                    year_end_carry)
from app.models import Period, Subject, Voucher
from tests.accounting_helpers import close_any


def _close_year(db, book, periods, admin_user):
    """把 2026 全年结完（与 ``tests/test_year_end_direction.py`` 同一套口径）。"""
    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        if p.locked:
            continue
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass          # 该月没损益，跳过
        close_any(db, book, p, admin_user)


def _drop_subjects_by_codes(db, book, year: int, codes) -> list[str]:
    """把 ``codes`` 里那一族科目**连同子树**拆掉。

    为什么连子树一起拆：``RETAINED_SUBJECT_CODES`` 有三个候选
    （310415 / 3104 / 4104），而 3104 是 310415 的**上级** ——
    只拆 310415 的话，查找会落到 3104 上（它不是末级，往它身上记账
    会被另一道闸挡下，那不是本条要测的东西）。

    **先置空 parent_id 再删**（与 ``seed/chart_of_accounts.install_chart``
    同一套做法）：``subject.parent_id`` 是指向自己的外键，而且**没有
    ON DELETE CASCADE** —— 父科目先被删、子科目还指着它，就撞外键。
    置空之后整棵树就是「一堆 parent_id IS NULL 的行」，删的先后就无所谓了。

    **拆之前必须没有分录挂在这些科目上** —— 分录的 ``subject_id`` 是
    外键，而且已记账凭证的分录还有一道库层触发器挡着不许删。
    所以「本年利润也拆掉」那条用例要在**建凭证之前**拆。
    """
    rows = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.year == year)).all()
    doomed: set[int] = set()
    dropped: list[str] = []

    def _mark(s: Subject) -> None:
        for child in rows:
            if child.parent_id == s.id and child.id not in doomed:
                doomed.add(child.id)
                dropped.append(child.code)
                _mark(child)
        if s.id not in doomed:
            doomed.add(s.id)
            dropped.append(s.code)

    for s in rows:
        if s.code in codes:
            _mark(s)
    if not doomed:
        return []
    db.execute(update(Subject).where(Subject.id.in_(doomed)).values(parent_id=None))
    for s in rows:
        if s.id in doomed:
            db.delete(s)
    db.flush()
    return dropped


def _drop_retained_subjects(db, book, year: int) -> list[str]:
    """把「未分配利润」那一族科目**连同子树**拆掉（见上面的通用版）。"""
    return _drop_subjects_by_codes(db, book, year, RETAINED_SUBJECT_CODES)


def _carry_forward_vouchers(db, book, year: int) -> list[Voucher]:
    return db.scalars(
        select(Voucher).where(Voucher.book_id == book.id,
                              Voucher.source == "CARRY_FORWARD")).all()


def test_缺未分配利润科目时年度结转报错(db, book, periods, subjects, admin_user,
                                  make_voucher):
    """缺「未分配利润」→ **报错**，而不是静默跳过整段结转。"""
    # 一笔收入：本年利润上要有余额，否则那段结转本来就不会发生
    make_voucher("2026-01-10", dr=[("1002", 1200, "x")], cr=[("5001", 1200, "x")],
                 posted=True)
    _close_year(db, book, periods, admin_user)

    dropped = _drop_retained_subjects(db, book, 2026)
    db.commit()
    assert "310415" in dropped or "3104" in dropped, f"前置不成立：拆掉的科目是 {dropped}"

    with pytest.raises(ValueError) as ei:
        year_end_carry(db, book, admin_user, 2026, 2027)
    msg = str(ei.value)
    assert "未分配利润" in msg, msg
    # 报错必须说清**后果**与**下一步**，不能只说现象
    assert "0" in msg, msg
    db.rollback()


def test_缺未分配利润时不会偷偷把利润滚到下一年(db, book, periods, subjects,
                                          admin_user, make_voucher):
    """报错之外，还要确认**没有发生**那件事：一张结转凭证都没有生成。"""
    make_voucher("2026-01-10", dr=[("1002", 1200, "x")], cr=[("5001", 1200, "x")],
                 posted=True)
    _close_year(db, book, periods, admin_user)
    before = len(_carry_forward_vouchers(db, book, 2026))

    _drop_retained_subjects(db, book, 2026)
    db.commit()
    with pytest.raises(ValueError):
        year_end_carry(db, book, admin_user, 2026, 2027)
    db.rollback()

    after = len(_carry_forward_vouchers(db, book, 2026))
    assert after == before, "报错了却仍然生成了结转凭证"


def test_科目齐全时年度结转照旧把本年利润冲平(db, book, periods, subjects,
                                         admin_user, make_voucher):
    """对照组：fail-closed 不能把**正常**的年度结转也堵住。"""
    from app.ledger.engine import LedgerEngine

    make_voucher("2026-01-10", dr=[("1002", 1200, "x")], cr=[("5001", 1200, "x")],
                 posted=True)
    _close_year(db, book, periods, admin_user)

    dec_ord = db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))
    profit = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "3103"))
    before = LedgerEngine(db, book.id).figures(profit, dec_ord).closing
    assert before != Decimal("0"), "前置不成立：本年利润上应当有余额"

    year_end_carry(db, book, admin_user, 2026, 2027)

    after = LedgerEngine(db, book.id).figures(profit, dec_ord).closing
    assert after == Decimal("0"), "年度结转没有把本年利润冲平"


# ===================================================================== §3.16 的文案另一半
#
# 审计报告 §3.16 的修复建议里有两半：① 「找不到未分配利润就报错、
# 绝不静默跳过」（**已做**，见本文件前几条用例）；② **报错文案不许
# 陈述不成立的事实**。
#
# 原来那半没做：科目表**同时**没有「本年利润」时，文案仍然写
# 「本年利润无法结转到留存收益」「本年利润的余额原样滚到下一年」——
# 而本年利润科目根本不存在，这两句描述的是**另一个状态**。
#
# 用户读到的是一句对不上的话：他既没有「本年利润」，也就没有
# 「本年利润的余额」可滚；真正缺的是「未分配利润」这个科目。


def test_本年利润也没有时文案不说本年利润的余额会滚到下一年(db, book, periods,
                                                 admin_user):
    """两种科目表（缺未分配利润但有本年利润 / 两者都缺）各跑一次。

    **本年利润也缺**时，那两句关于「本年利润」的陈述都不成立 ——
    不许说。

    **报错本身必须保留**：缺「未分配利润」一律停下（fail-closed 是
    §3.16 的正确修法，别把它放宽回去）。
    """
    from app.accounting.periods import PROFIT_SUBJECT_CODES

    # **在建任何凭证之前**把两个科目族都拆掉 ——
    # 分录的 subject_id 是外键，而已记账凭证的分录还有库层触发器挡着不许删。
    dropped = _drop_subjects_by_codes(
        db, book, 2026, tuple(RETAINED_SUBJECT_CODES) + tuple(PROFIT_SUBJECT_CODES))
    db.commit()
    assert any(c in dropped for c in RETAINED_SUBJECT_CODES), (
        f"前置不成立：拆掉的科目是 {dropped}")
    assert any(c in dropped for c in PROFIT_SUBJECT_CODES), (
        f"前置不成立：拆掉的科目是 {dropped}")

    # 这一年没有任何损益业务 → 结账不需要结转损益（``close_any`` 会补中性凭证）
    _close_year(db, book, periods, admin_user)

    with pytest.raises(ValueError) as ei:
        year_end_carry(db, book, admin_user, 2026, 2027)
    msg = str(ei.value)
    db.rollback()

    # 缺的是「未分配利润」—— 这个事实要说
    assert "未分配利润" in msg, msg
    # **不成立的两句不许出现**
    assert "本年利润无法结转到留存收益" not in msg, msg
    assert "原样滚到下一年" not in msg, msg
    # 下一步仍然要说清
    assert "请先补上该科目再结转" in msg, msg


def test_有本年利润时文案照旧说本年利润会滚到下一年(db, book, periods, subjects,
                                            admin_user, make_voucher):
    """对称事实的另一半：**本年利润在**时，那两句**成立**，不许被删掉。

    （把文案改成「什么都不说」也是一种失败：用户就不知道继续结转会
    造成什么后果了。）
    """
    make_voucher("2026-01-10", dr=[("1002", 1200, "x")], cr=[("5001", 1200, "x")],
                 posted=True)
    _close_year(db, book, periods, admin_user)

    _drop_retained_subjects(db, book, 2026)
    db.commit()

    with pytest.raises(ValueError) as ei:
        year_end_carry(db, book, admin_user, 2026, 2027)
    msg = str(ei.value)
    db.rollback()
    assert "未分配利润" in msg, msg
    assert "本年利润无法结转到留存收益" in msg, msg
    assert "原样滚到下一年" in msg, msg
    assert "0" in msg, msg
