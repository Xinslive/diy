"""区间倒置必须报错，而不是给出一份「看起来自洽」的错账（审计 P0）。

``queries.balance_sheet_of_accounts`` 是科目余额表与总账**共用**的区间实现。
倒过来传一段区间（起始期间晚于止期）时它不会失败，只会算出一份内部完全自洽的表：

* ``engine.range_movements`` 逐期按 ``from_ord <= ord <= to_ord`` 过滤，
  倒置区间一条都留不下 → **区间发生额全是 0**；
* 期初取的是「起始期的上一期期末」，期末 = 期初 + 0 − 0
  → **每一行的期初都等于期末**；
* 试算平衡 ``Σ借 == Σ贷`` 于是照样通过。

三条全中，所以没有任何自检会报警。用户看到的是一张有标题、有期初、试算也平的
账 —— 而科目余额表正是要交给客户和审计的纸面数。

以前这道闸只写在 ``/ledger/balance`` 那一个路由里，``/ledger/general`` 没查
（只校验了起始期间属不属于本账套），两者却调的是同一个函数：同一段倒置区间，
余额表回 400、总账悄悄给出一份错账。现在闸门下沉到共享实现，所有调用方一致。

**断言口径是「接口真的返回 400」，不是「某个分支被走到了」**，并且带一道
**非空自检**：先证明这段区间本来就有发生额 —— 否则「400」也可能只是因为区间里
本来就没数据，测试就成了永远绿的空断言（这正是本仓库记过的「假绿」毛病）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.ledger import queries
from app.models import Subject, Voucher

# 倒置区间用的两个月：1 月与 3 月（中间隔着 2 月）。
FROM_MONTH = 3
TO_MONTH = 1


def _subjects(db, book) -> dict:
    return {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book.id)).all()}


def _seed(make_voucher) -> None:
    """两个月各一张**已记账**凭证，保证区间里真的有发生额。

    必须已记账：账簿默认只统计 POSTED（``include_unposted=False``，
    也是这几个接口的默认口径）。未记账的凭证在余额表上表现为零，
    拿它做前置自检就会得出「这段区间本来就没数」的相反结论。
    """
    make_voucher("2026-01-05", dr=[("1002", 300, "收款")],
                 cr=[("5001", 300, "收入")], posted=True)
    make_voucher("2026-03-05", dr=[("1002", 700, "收款")],
                 cr=[("5001", 700, "收入")], posted=True)


def _params(periods, from_month=FROM_MONTH, to_month=TO_MONTH) -> dict:
    return {"period_id": periods[to_month].id, "from_period_id": periods[from_month].id}


def _前置_区间本该有发生额(db, book, periods, subs) -> list[dict]:
    """非空自检：正序框住同样这两个月，发生额必须非零。

    没有这一句，「倒置区间返回 400」可能只是因为区间里压根没数据 ——
    那种 400 是对的，但它证明不了闸门真的在挡什么。
    """
    rows = queries.general_ledger(
        db, book.id, list(subs.values()), periods[1], periods[FROM_MONTH], False)
    moving = [r for r in rows if r["period_debit"] or r["period_credit"]]
    assert moving, "前置：这段区间必须有发生额，否则倒置区间返回 400 说明不了任何问题"
    return rows


# --------------------------------------------------------------------- 总账（缺陷所在）


def test_总账的倒置区间返回400而不是一份全零的错账(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """这条就是缺陷本身：``/ledger/general`` 以前根本不查 ord。"""
    _seed(make_voucher)
    forward = _前置_区间本该有发生额(db, book, periods, _subjects(db, book))

    r = client.get(f"/api/books/{book.id}/ledger/general", headers=auth,
                   params=_params(periods))
    assert r.status_code == 400, f"倒置区间应当被拒绝，实际返回 {r.status_code}：{r.text[:400]}"
    assert "起始期间不能晚于结束期间" in r.text, r.text[:400]
    # 记下旧实现会给出的那份错账长什么样：期初 = 期末、发生额全 0、试算还平。
    # 它不是「恰好没数」，而是把有数的区间算成了静止的 —— 这正是必须挡掉的。
    assert all(r["opening"] == r["closing"] or r["period_debit"] or r["period_credit"]
               for r in forward), "前置：正序区间的行不该全是静止行"


def test_总账的正序区间仍然是200且有发生额(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """闸门不能顺手把正常区间也拦了 —— 这条是上一条的对照。"""
    _seed(make_voucher)

    r = client.get(f"/api/books/{book.id}/ledger/general", headers=auth,
                   params=_params(periods, from_month=TO_MONTH, to_month=FROM_MONTH))
    assert r.status_code == 200, r.text[:400]
    rows = r.json()["rows"]
    assert rows, "正序区间应当有行"
    assert any(row["period_debit"] > 0 for row in rows), \
        f"正序区间却没有发生额，闸门可能拦过头了：{rows[:2]}"


def test_倒置区间在共享实现里就被挡住(db, book, periods, subjects, make_voucher):
    """服务层也要挡：打印、导出、将来的脚本都不经过路由。

    断言抛的是 ``HTTPException`` 而不是 ``ValueError`` —— 这是有讲究的：
    ``api/printing.py`` 的 ``_render_scope``（五个 .html 接口）只接
    ``ExportTooLarge``，裸 ``ValueError`` 会以 **500** 落给用户。参数写错却回
    「服务器错误」，比返回一份错账还让人无从下手。
    """
    _seed(make_voucher)
    subs = _subjects(db, book)
    _前置_区间本该有发生额(db, book, periods, subs)

    with pytest.raises(HTTPException) as exc:
        queries.balance_sheet_of_accounts(
            db, book.id, list(subs.values()), periods[TO_MONTH], False,
            from_period=periods[FROM_MONTH])
    assert exc.value.status_code == 400
    assert "起始期间不能晚于结束期间" in str(exc.value.detail)

    # 总账是同一份实现的另一个门面，行为必须一致
    with pytest.raises(HTTPException):
        queries.general_ledger(
            db, book.id, list(subs.values()), periods[FROM_MONTH], periods[TO_MONTH], False)


# --------------------------------------------------------------------- 余额表 / 明细账


def test_余额表的倒置区间返回400(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """回归护栏：路由层那道闸（更早触发、报错更直接）不能被下沉改动弄掉。"""
    _seed(make_voucher)

    r = client.get(f"/api/books/{book.id}/ledger/balance", headers=auth,
                   params=_params(periods))
    assert r.status_code == 400, r.text[:400]
    assert "起始期间不能晚于结束期间" in r.text


def test_明细账的倒置区间返回400(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """明细账走的是另一个函数（``detail_ledger``），它自己的那道 ord 校验也必须在。"""
    _seed(make_voucher)

    r = client.get(f"/api/books/{book.id}/ledger/detail", headers=auth,
                   params={**_params(periods), "subject_id": _subjects(db, book)["1002"].id})
    assert r.status_code == 400, r.text[:400]
    assert "起始期间不能晚于结束期间" in r.text


# --------------------------------------------------------------------- 打印与导出


def test_总账打印的倒置区间返回400而不是500(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """``/print/general-ledger.html`` 是**没有**自己那道 ord 校验的调用方。

    它经 ``_render_scope`` → ``queries.general_ledger`` 进来。以前这条路径会
    悄悄渲染出一份全零的错账（纸面数！），闸门下沉之后必须变成 400。
    """
    _seed(make_voucher)
    _前置_区间本该有发生额(db, book, periods, _subjects(db, book))

    r = client.get(f"/api/books/{book.id}/print/general-ledger.html", headers=auth,
                   params=_params(periods))
    assert r.status_code == 400, \
        f"总账打印的倒置区间应当回 400（而不是 500 或一份错账），实际 {r.status_code}"
    assert "起始期间不能晚于结束期间" in r.text


def test_余额表导出的倒置区间返回400(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """导出侧同样要挡住：xlsx 是直接交给客户的纸面数。"""
    _seed(make_voucher)

    r = client.get(f"/api/books/{book.id}/print/balance.xlsx", headers=auth,
                   params=_params(periods))
    assert r.status_code == 400, r.text[:400]


# --------------------------------------------------------------------- 只读性


def test_倒置区间不会偷偷改动账上的凭证(
    db, book, periods, subjects, admin_user, make_voucher, client, auth
):
    """闸门是只读的：查一份错账也不该有任何副作用。"""
    _seed(make_voucher)
    before = sorted(db.scalars(select(Voucher.id, Voucher.voucher_no)
                               .where(Voucher.book_id == book.id)).all())

    assert client.get(f"/api/books/{book.id}/ledger/general", headers=auth,
                      params=_params(periods)).status_code == 400
    assert client.get(f"/api/books/{book.id}/ledger/balance", headers=auth,
                      params=_params(periods)).status_code == 400

    db.expire_all()
    after = sorted(db.scalars(select(Voucher.id, Voucher.voucher_no)
                              .where(Voucher.book_id == book.id)).all())
    assert after == before, "被拒绝的查询不该动账"
