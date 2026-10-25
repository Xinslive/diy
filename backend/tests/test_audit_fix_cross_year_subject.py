"""审计修复 §3.19：跨年度改凭证日期时，科目校验要按**目标年度**。

**这条缺陷是什么。**

``update_voucher`` 里改日期换期时，凭证会挪到另一个期间，而分录的科目校验
用的是 ``period.year`` —— 而那个 ``period`` 是**改期前**那个期间
（``voucher.period_id = new_period.id`` 在更后面才执行）。

科目表**每个年度各有一份**（迁移 0032）：「1001 库存现金」在 2026 与 2027
是两行不同的记录、两个 id。于是「把 2026 年的凭证改到 2027 年 1 月、
分录换成 2027 年科目表里的科目」这条**完全合法**的操作被判成
「科目不存在或已停用」—— 服务端拿着 **2026 年**的科目表去找 **2027 年**的
科目 id，自然找不到。

用户看到的是「科目不存在或已停用」，而那本 2027 科目表里这个科目明明存在、
也明明是启用的：用户会去检查科目、怀疑自己选错了，实际是服务端拿错了年份。

修法：校验时传**目标期间**的年度（``new_period.year if new_period else period.year``）。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.accounting.periods import ensure_periods
from app.accounting.vouchers import update_voucher
from app.accounting.years import copy_chart, ensure_book_year
from app.models import Entry, Period, Subject


def _year_2027(db, book):
    """给账套装上 2027 年度：年度登记 + 12 个期间 + 一份自己的科目表。

    科目表**按编码复制**（``copy_chart``）—— 与年度结转铺新年度科目表
    同一套做法：编码是唯一稳定的对应关系，新行是新 id。
    """
    ensure_book_year(db, book, 2027)
    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()


def _subjects_of(db, book, year: int) -> dict[str, Subject]:
    return {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.year == year)
        .order_by(Subject.code)).all()}


def test_跨年改期按目标年度校验科目(db, book, subjects, make_voucher, admin_user):
    """把 2026 的凭证挪到 2027 年 1 月，分录用 2027 年的科目 —— 必须成功。"""
    _year_2027(db, book)
    y27 = _subjects_of(db, book, 2027)
    assert y27["1001"].id != subjects["1001"].id, "前提不成立：两个年度的科目表应当是两行"

    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    update_voucher(db, v, admin_user, {
        "voucher_date": date(2027, 1, 15),
        "entries": [
            {"subject_id": y27["1001"].id, "direction": "DEBIT",
             "amount": "100", "summary": "x"},
            {"subject_id": y27["5001"].id, "direction": "CREDIT",
             "amount": "100", "summary": "x"},
        ],
    })

    db.refresh(v)
    assert v.voucher_date == date(2027, 1, 15)
    assert v.period_id == db.scalar(
        select(Period.id).where(Period.book_id == book.id,
                                Period.year == 2027, Period.month == 1))
    # 分录挂的是 **2027 年**那一份科目
    used = db.scalars(select(Entry).where(Entry.voucher_id == v.id)).all()
    assert {e.subject_id for e in used} == {y27["1001"].id, y27["5001"].id}


def test_跨年改期后凭证号按目标期间重发(db, book, subjects, make_voucher, admin_user):
    """换期 = 换号段：新号发在目标期间，且不撞目标期间已有的号。"""
    _year_2027(db, book)
    y27 = _subjects_of(db, book, 2027)

    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    update_voucher(db, v, admin_user, {
        "voucher_date": date(2027, 1, 15),
        "entries": [
            {"subject_id": y27["1001"].id, "direction": "DEBIT",
             "amount": "100", "summary": "x"},
            {"subject_id": y27["5001"].id, "direction": "CREDIT",
             "amount": "100", "summary": "x"},
        ],
    })
    db.refresh(v)
    assert v.voucher_no == "记0001", v.voucher_no


def test_跨年改期用旧年度的科目仍然被拒(db, book, subjects, make_voucher, admin_user):
    """对照组：目标年度的科目表里没有的科目，**仍然**要拒。

    （拿 2026 年的科目 id 挂到 2027 年的凭证上，库层
    ``fn_entry_subject_year_guard`` 也会挡 —— 但服务层就要先说清楚。）

    **这里断言的是「年度不匹配」，不是「科目不存在或已停用」。**
    审计报告 §3.19 的修复建议原文有两句：

      ① 改用目标期间的年份 —— **已做**（本文件前两条用例）；
      ② 「错误文案应区分『年度不匹配』与『科目真的不存在』」——
         **原来没做**，所以这条用例原来的断言
         ``assert "科目不存在或已停用" in str(ei.value)`` **把一条
         误导文案固化成了回归基线**。

    改的是**基线错了**，不是「测试太严」：那个科目**确实存在**
    （在 2026 那张表里）、也**确实启用着**，2027 的科目表里没有它只是
    因为**科目表每个年度各有一份**。说它「不存在或已停用」会把用户
    引去检查科目、怀疑自己选错了 —— 正是 §3.19 抱怨的那个症状，
    只是换了触发条件。
    """
    _year_2027(db, book)

    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    with pytest.raises(ValueError) as ei:
        update_voucher(db, v, admin_user, {
            "voucher_date": date(2027, 1, 15),
            "entries": [
                {"subject_id": subjects["1001"].id, "direction": "DEBIT",
                 "amount": "100", "summary": "x"},
                {"subject_id": subjects["5001"].id, "direction": "CREDIT",
                 "amount": "100", "summary": "x"},
            ],
        })
    db.rollback()
    msg = str(ei.value)
    # 文案要说清是**年度**的问题，并指出该换成哪一年的科目
    assert "年度" in msg, msg
    assert "2026" in msg and "2027" in msg, msg
    # **不许**说「不存在或已停用」—— 那个科目明明存在、也明明启用着
    assert "不存在或已停用" not in msg, msg


def test_科目在目标年度也不存在时仍然说不存在或已停用(db, book, subjects,
                                              make_voucher, admin_user):
    """对称事实的另一半：**真的**不存在时，仍然说「不存在或已停用」。

    区分两种情况的判据是**事实**：这个 id 在这个账套的哪个年度存在。
    一个哪一年都不存在的科目 id，没有「年度不匹配」可言 —— 那就老老实实
    说它不存在。
    """
    _year_2027(db, book)
    y27 = _subjects_of(db, book, 2027)

    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    ghost = 999_999_999        # 哪个账套、哪个年度都没有这个 id
    with pytest.raises(ValueError) as ei:
        update_voucher(db, v, admin_user, {
            "voucher_date": date(2027, 1, 15),
            "entries": [
                {"subject_id": ghost, "direction": "DEBIT",
                 "amount": "100", "summary": "x"},
                {"subject_id": y27["5001"].id, "direction": "CREDIT",
                 "amount": "100", "summary": "x"},
            ],
        })
    db.rollback()
    msg = str(ei.value)
    assert "科目不存在或已停用" in msg, msg
    assert str(ghost) in msg, msg
    # 它不是年度不匹配 —— 不许把「年度」那套说辞套上来
    assert "别的年度" not in msg, msg


def test_两种科目混在一起时两类都要说(db, book, subjects, make_voucher, admin_user):
    """一半是年度不匹配、一半是真不存在 → **两类都要说清**。

    只说其中一类同样是误导：一半的分录用户以为换年度就能过，
    另一半他根本查无此科目。
    """
    _year_2027(db, book)

    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    ghost = 999_999_999
    with pytest.raises(ValueError) as ei:
        update_voucher(db, v, admin_user, {
            "voucher_date": date(2027, 1, 15),
            "entries": [
                {"subject_id": subjects["1001"].id, "direction": "DEBIT",
                 "amount": "100", "summary": "x"},
                {"subject_id": ghost, "direction": "CREDIT",
                 "amount": "100", "summary": "x"},
            ],
        })
    db.rollback()
    msg = str(ei.value)
    assert "别的年度" in msg, msg
    assert "科目不存在或已停用" in msg, msg
    assert str(ghost) in msg, msg
