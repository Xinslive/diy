"""审计修复 §3.18：新建凭证**不许写死「记」前缀**。

**这条缺陷是什么。**

``create_voucher`` 原来发号时不传前缀，``next_voucher_no`` 的默认前缀就是「记」。
而这个期间的凭证字**未必是「记」** —— Excel 导入的凭证直接采用表里带的号
（``importer`` 里就是 ``v.get("voucher_no")``），而 ``require_no`` 只校验
「凭证字 + 序号」这个**形状**、**不限制前缀** —— 所以「用友导出的 银0001」进得来。

后果：那个期间的 dominant prefix 变成「银」，而界面上正常新建一张凭证 →
服务端发出「记0001」→ 该期立刻变成「记、银 混用」→
``assert_tidy`` 判为 blocking，而它同时是**记账**与**结账**的闸门 →
**这个月从此既不能记账也不能结账**。

用户看到的是一句看不懂的报错：既不知道是哪张凭证引起的，
也不知道「记0001」是系统自己刚发出去的号（凭证号在界面上是只读的，用户改不了）。

修法与「把一张凭证挪进另一个期间」那条路**同一口径**：
``organize.period_prefix`` 给出该期出现次数最多的凭证字，期间一张都没有时回落「记」。

**顺带钉住日期那道闸的适用范围。** 新凭证拿的是本期**最大**的号（排在最后），
而「整理就绪」体检要求**号序与日期同向** —— 号大的那张日期反而更早，
就判「时序不对」，这个月既不能记账也不能结账。

这条闸**只管界面上那张单张新建的表单**（``create_voucher`` 的
``enforce_date_order=True``）：导入的日期来自 Excel、发票的日期来自发票本身、
结转的日期是系统定的 —— 那三条路的日期不是我们能替用户改的。
而倒序录入本身是合法业务，本仓的既定解法是月底「整理凭证」按日期重排
（``SORT_DATE``），不是当场拒绝录入。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.accounting import organize as svc_organize
from app.accounting.vouchers import create_voucher
from app.models import Period


def _payload(subjects, dr, cr, when: str):
    entries = [{"subject_id": subjects[c].id, "direction": "DEBIT",
                "amount": str(a), "summary": s} for c, a, s in dr]
    entries += [{"subject_id": subjects[c].id, "direction": "CREDIT",
                 "amount": str(a), "summary": s} for c, a, s in cr]
    return {"voucher_date": when, "entries": entries, "remark": "测试"}


def _seed_yin_period(db, book, make_voucher):
    """造一个「凭证字是银」的期间（模拟 Excel 导入进来的用友凭证）。"""
    made = []
    for i, day in enumerate(("2026-01-05", "2026-01-10", "2026-01-15"), start=1):
        made.append(make_voucher(
            day, dr=[("1001", 100 * i, "x")], cr=[("5001", 100 * i, "x")],
            voucher_no=f"银{i:04d}"))
    db.commit()
    return made


def test_银期间里新建的凭证拿银号(db, book, subjects, make_voucher, admin_user):
    """服务端发出的号必须与该期在用的凭证字**一致**。"""
    _seed_yin_period(db, book, make_voucher)

    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 20),
        entries=[
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "500", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "500", "summary": "x"},
        ], commit=False)
    db.commit()
    assert v.voucher_no == "银0004", f"新凭证拿到了 {v.voucher_no}（该期在用的是「银」）"


def test_银期间新建凭证之后这个月照样能记账(db, book, subjects, periods, make_voucher,
                                       admin_user):
    """修好之后：这个月**既不是混用凭证字，也不是断号** → 体检放行。

    修复前：新建一张就发「记0001」→ 该期变成「记、银 混用」→
    ``assert_tidy`` blocking → 这个月从此既不能记账也不能结账。
    """
    _seed_yin_period(db, book, make_voucher)
    create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 20),
        entries=[
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "500", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "500", "summary": "x"},
        ], commit=False)
    db.commit()

    period = db.get(Period, periods[1].id)
    svc_organize.assert_tidy(db, [period])       # 不抛 = 放行
    rep = svc_organize.check_period(db, period)
    assert rep["prefixes"] == ["银"], rep
    assert rep["gaps"] == [], rep


def test_界面上新建拿到的也是银号(client, auth, book, subjects, make_voucher):
    """HTTP 那条路（页面上新建）同样拿银号。"""
    for i, day in enumerate(("2026-01-05", "2026-01-10", "2026-01-15"), start=1):
        make_voucher(day, dr=[("1001", 100 * i, "x")], cr=[("5001", 100 * i, "x")],
                     voucher_no=f"银{i:04d}")

    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json=_payload(
        subjects, [("1001", 500, "x")], [("5001", 500, "x")], "2026-01-20"))
    assert r.status_code == 201, r.text
    assert r.json()["voucher_no"] == "银0004", r.json()


def test_页面上预填的号与实际发出来的号一致(client, auth, db, book, subjects, make_voucher):
    """``/vouchers/next`` 预填的号必须与落库时发的号**同一个口径**。

    预填「记0001」、存下去却是「银0004」—— 用户看到的东西与存进去的东西
    不是一回事，而凭证号在界面上是只读的（客户端的号又不采信）。
    """
    for i, day in enumerate(("2026-01-05", "2026-01-10", "2026-01-15"), start=1):
        make_voucher(day, dr=[("1001", 100 * i, "x")], cr=[("5001", 100 * i, "x")],
                     voucher_no=f"银{i:04d}")
    period = db.scalar(select(Period.id).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 1))

    r = client.get(f"/api/books/{book.id}/vouchers/next?period_id={period}",
                   headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["voucher_no"] == "银0004", r.json()


def test_界面上新建的日期不能早于本期最后一张(client, auth, book, subjects, make_voucher):
    """日期倒着填 → 当场说清楚，而不是等月底被体检拦下。

    提示必须说清**下一步**：改日期，或者用「插入凭证」把它插到该在的位置。
    """
    for i, day in enumerate(("2026-01-05", "2026-01-10", "2026-01-15"), start=1):
        make_voucher(day, dr=[("1001", 100 * i, "x")], cr=[("5001", 100 * i, "x")])

    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json=_payload(
        subjects, [("1001", 500, "x")], [("5001", 500, "x")], "2026-01-01"))
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "早于" in detail, detail
    assert "插入凭证" in detail, detail


def test_导入路径不受日期那道闸(db, book, subjects, make_voucher, admin_user):
    """服务层的默认口径是**不查日期** —— 导入的日期来自 Excel，不是我们能改的。

    这条用例把适用范围钉死：闸门只管界面上那张单张新建的表单。
    """
    v = make_voucher("2026-01-15", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    created = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 1),
        entries=[
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "500", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "500", "summary": "x"},
        ], source="IMPORT", commit=False)
    db.commit()
    assert created.id != v.id
    assert created.voucher_date == date(2026, 1, 1)
