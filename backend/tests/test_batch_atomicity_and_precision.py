"""批量删除的原子性（P2-05）与期初金额精度（P2-06）。

P2-05：``delete_voucher`` 内部自带 ``commit()``，``batch_delete`` 逐张调用它。
       预检只覆盖了「已记账」这一种失败原因；一旦中途因别的原因失败，
       前面几张已删除**且已提交**，回滚回滚不回来。
       用户收到 400，账上却少了几张，且没有「哪几张成功了」的信息。

P2-06：期初试算平衡用全精度 Decimal，但数据库列是 ``Numeric(20,2)`` ——
       「校验的数」和「存储的数」不是同一个数。审计实测：
       提交 0.005 + 0.005 - 0.010，全精度和为 0.000（试算通过），
       接口 200，而库里实际存下来的和是 0.01。
       期初差 0.01 会顺着 LedgerEngine 传导到之后每个月、每张账簿、每份报表，
       且极难回溯到是录入期初时就差了。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Period, PeriodOpening, Subject, Voucher


# ---------------------------------------------------------------- P2-05


def _mk(client, auth, book_id, day, amount="100"):
    subs = client.get(f"/api/books/{book_id}/subjects/flat", headers=auth).json()
    dr = next(s for s in subs if s["category"] == "ASSET")
    cr = next(s for s in subs if s["category"] in ("LIABILITY", "REVENUE"))
    r = client.post(f"/api/books/{book_id}/vouchers", headers=auth, json={
        "voucher_date": day,
        "entries": [
            {"subject_id": dr["id"], "direction": "DEBIT", "amount": amount, "summary": "删一半"},
            {"subject_id": cr["id"], "direction": "CREDIT", "amount": amount, "summary": "删一半"},
        ],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk(client, auth, book_id, day, amount="100"):
    subs = client.get(f"/api/books/{book_id}/subjects/flat", headers=auth).json()
    dr = next(s for s in subs if s["category"] == "ASSET")
    cr = next(s for s in subs if s["category"] in ("LIABILITY", "REVENUE"))
    r = client.post(f"/api/books/{book_id}/vouchers", headers=auth, json={
        "voucher_date": day,
        "entries": [
            {"subject_id": dr["id"], "direction": "DEBIT", "amount": amount, "summary": "批量删除"},
            {"subject_id": cr["id"], "direction": "CREDIT", "amount": amount, "summary": "批量删除"},
        ],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _count(db, book_id):
    return db.scalar(select(Voucher).where(Voucher.book_id == book_id)
                     .with_only_columns(__import__("sqlalchemy").func.count()))


def test_批量删除中途失败时一张都不该少(client, auth, db, book, monkeypatch):
    """**构造真正的中途失败**：第 2 张删除时抛错，断言 3 张一张都没少。

    这是 P2-05 的要害。原来 ``delete_voucher`` 自带 ``commit()``，
    ``batch_delete`` 逐张调用它 —— 删到第 2 张失败时，第 1 张已经落库，
    路由的 ``db.rollback()`` 回滚不回来。用户收到 400，账上却少了一张。

    预检拦不住的失败是真实存在的：并发删除、期间状态变化、数据库触发器
    拦截���所以「预检覆盖了已记账」并不等于「不会中途失败」。
    """
    from app.api import vouchers as api_vouchers

    ids = [_mk(client, auth, book.id, "2026-01-0%d" % (i + 1)) for i in range(3)]
    n_before = _count(db, book.id)

    real = api_vouchers.svc.delete_voucher
    calls = {"n": 0}

    def flaky(db_, v, user, commit=True):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("模拟第 2 张删除失败")
        return real(db_, v, user, commit=commit)

    monkeypatch.setattr(api_vouchers.svc, "delete_voucher", flaky)

    r = client.post(f"/api/books/{book.id}/vouchers/batch-delete", headers=auth,
                    json={"ids": ids})
    assert r.status_code == 400, r.text
    assert calls["n"] == 2, "用例没构造出中途失败，测不到原子性"

    db.expire_all()
    assert _count(db, book.id) == n_before, (
        f"中途失败后凭证数变了：{n_before} → {_count(db, book.id)}"
        "（说明前几张已经提交，无法回滚）")


def test_批量删除全部成功时照常删干净(client, auth, db, book):
    """收紧不能误伤正常路径。"""
    ids = [_mk(client, auth, book.id, "2026-01-0%d" % (i + 1)) for i in range(3)]
    n_before = _count(db, book.id)

    r = client.post(f"/api/books/{book.id}/vouchers/batch-delete", headers=auth,
                    json={"ids": ids})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 3
    db.expire_all()
    assert _count(db, book.id) == n_before - 3


def test_批量删除混入不存在的ID整批拒绝(client, auth, db, book):
    """传 3 个 id、其中 1 个不存在 → 整批拒绝，一张都不删。

    原来这里直接把查询结果拿去删，返回 ``{"deleted": 2}`` 且不置一词：
    用户以为自己删了 3 张，实际 2 张，那张还在列表里躺着。
    """
    ids = [_mk(client, auth, book.id, "2026-01-0%d" % (i + 1)) for i in range(2)]
    n_before = _count(db, book.id)

    r = client.post(f"/api/books/{book.id}/vouchers/batch-delete", headers=auth,
                    json={"ids": ids + [999999]})
    assert r.status_code == 400, f"期望整批拒绝，实际 {r.status_code}：{r.text}"
    assert "不存在" in r.json()["detail"]

    db.expire_all()
    assert _count(db, book.id) == n_before, "有非法 ID 时一张都不该被删掉"


def test_批量删除里混了已记账凭证则整批不动(client, auth, db, book, make_voucher):
    """已记账的删不掉 —— 拦截之后，同批的其它凭证也一张都不该少。"""
    ok1 = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    ok2 = make_voucher("2026-01-06", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    posted = make_voucher("2026-01-07", dr=[("100201", 100, "x")],
                          cr=[("600101", 100, "x")], posted=True)

    r = client.post(f"/api/books/{book.id}/vouchers/batch-delete", headers=auth,
                    json={"ids": [ok1.id, ok2.id, posted.id]})
    assert r.status_code == 400, r.text
    assert "已记账" in r.json()["detail"]

    db.expire_all()
    assert db.get(Voucher, ok1.id) is not None, "整批被拒时不该删掉任何一张"
    assert db.get(Voucher, ok2.id) is not None


# ---------------------------------------------------------------- P2-06


def _post_openings(client, auth, book, items, period_id=None):
    url = f"/api/books/{book.id}/periods"
    if period_id:
        url += f"?period_id={period_id}"
    return client.post(url, headers=auth, json=items)


def _openings(db, book, year=2026, month=1):
    p = db.scalar(select(Period).where(Period.book_id == book.id,
                                       Period.year == year, Period.month == month))
    return db.scalars(select(PeriodOpening).where(PeriodOpening.period_id == p.id)).all()


def test_三位小数落库后仍然借贷平衡(client, auth, db, book, subjects):
    """审计 P1/P2 实测的原始场景：0.005 + 0.005 - 0.010。

    三个**互不相同**的科目 —— 用同一个科目两次会先撞上
    ``uq_period_opening`` 唯一约束，报出来的是「数据已存在」，
    跟精度这条线一点关系都没有，测试就成了摆设。

    修好之后应当**明确报错**（量化后不平），而不是报 200 再落一个不平的库：
      · 不量化：0.005+0.005 == 0.010 → 试算通过 → 落库存成 0.01/0.01/-0.01，
        库里实际和 = 0.01，接口却返回 200。
      · 量化后：0.01+0.01 = 0.02 ≠ 0.01 → 拒绝。
    """
    a, b, c = subjects["100201"], subjects["100202"], subjects["4001"]
    r = _post_openings(client, auth, book, [
        {"subject_id": a.id, "amount": "0.005"},
        {"subject_id": b.id, "amount": "0.005"},
        {"subject_id": c.id, "amount": "-0.010"},
    ])
    assert r.status_code == 400, (
        f"量化后本该不平却通过了：{r.status_code} {r.text}"
        "（返回 200 意味着落库后借贷和不是 0）")
    db.expire_all()
    assert _openings(db, book) == [], "被拒的期初不该落库"


def test_量化后恰好平的期初可以落库(client, auth, db, book, subjects):
    """收紧不能误伤：量化后正好平的照常录入，且落库值就是校验用的那个值。"""
    a, b = subjects["100201"], subjects["4001"]
    r = _post_openings(client, auth, book, [
        {"subject_id": a.id, "amount": "100.004"},
        {"subject_id": b.id, "amount": "-100.004"},
    ])
    assert r.status_code == 200, f"量化后正好相等，却被拒了：{r.status_code} {r.text}"

    db.expire_all()
    rows = _openings(db, book)
    assert sum(x.amount for x in rows) == Decimal("0.00"), "落库后借贷和必须为 0"
    by_id = {x.subject_id: x.amount for x in rows}
    # 落库的必须是量化后的值，和试算平衡时用的是同一个数
    assert by_id[a.id] == Decimal("100.00"), by_id
    assert by_id[b.id] == Decimal("-100.00"), by_id


def test_非法金额给出明确拒绝(client, auth, db, book, subjects):
    a = subjects["100201"]
    r = _post_openings(client, auth, book, [{"subject_id": a.id, "amount": "金额待定"}])
    # 数字类型的字段由 Pydantic 先挡（422），非数字进不到量化那一步；
    # 两条路都得是「明确拒绝」而不是静默当成 0。
    assert r.status_code in (400, 422), f"非法金额竟然被放行了：{r.status_code} {r.text}"


def test_被拒的期初不会留下半截记录(client, auth, db, book, subjects):
    """整体重建的逻辑遇到不平就退出，库里不能剩下被删一半的期初。"""
    a, b = subjects["100201"], subjects["4001"]
    ok = _post_openings(client, auth, book, [
        {"subject_id": a.id, "amount": "50"},
        {"subject_id": b.id, "amount": "-50"},
    ])
    assert ok.status_code == 200
    db.expire_all()
    assert len(_openings(db, book)) == 2

    bad = _post_openings(client, auth, book, [
        {"subject_id": a.id, "amount": "70"},
        {"subject_id": b.id, "amount": "-60"},
    ])
    assert bad.status_code == 400
    db.expire_all()
    rows = _openings(db, book)
    assert len(rows) == 2, "失败的写入把原来的期初也冲掉了"
    assert {x.subject_id: x.amount for x in rows}[a.id] == Decimal("50.00")
