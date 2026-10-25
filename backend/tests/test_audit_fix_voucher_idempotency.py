"""新建凭证的幂等：同一把键只建一张凭证（审计报告 §3.28）。

**没有幂等键时的失败场景（报告原文）**：

    新凭证 POST 发出 → 服务端已入库 → 响应在网络上丢了（30s 超时）
    → 客户端以为自己没存上 → 再敲一个字 → 自动保存又发一次 POST
    → 库里两张内容几乎相同的凭证

**为什么这件事必须在服务端做。** 客户端当然可以「先查一下有没有存上」，
但查和写之间还是有时间窗：两次自动保存几乎同时到（用户连敲两下，
或者第一个请求慢到 30s 才超时），两边都查不到、都去写 —— 还是两张。
所以胜负必须由**数据库当场分出来**：一把键对应一行占位，
``INSERT … ON CONFLICT DO NOTHING`` 抢到的人负责做完，后来的人拿既有结果回去。

**客户端那一半**（回放之后补一次 PUT 把当前内容收敛上去）在
``frontend/tests/voucher_idempotency.test.mjs``。
"""
from __future__ import annotations

import concurrent.futures

import pytest
from sqlalchemy import select

from app.core import idempotency as idem
from app.models import Voucher, VoucherIdempotency

# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _payload(subjects, amount="100.00", summary="幂等"):
    """一张借贷各一行的请求体。"""
    return {
        "voucher_date": "2026-03-02",
        "entries": [
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": amount, "summary": summary},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": amount, "summary": summary},
        ],
    }


def _count(db, book_id) -> int:
    return len(db.scalars(
        select(Voucher).where(Voucher.book_id == book_id)).all())


# --------------------------------------------------------------------------
# 抢键：核心机制
# --------------------------------------------------------------------------


def test_同一把键只有一个人抢到(db, book):
    claimed_a, row_a = idem.claim(db, book_id=book.id, key="vch-a", request_hash="h1")
    assert claimed_a is True
    assert row_a is not None
    db.commit()

    claimed_b, row_b = idem.claim(db, book_id=book.id, key="vch-a", request_hash="h1")
    assert claimed_b is False, "同一把键不该被抢第二次"
    assert row_b.id == row_a.id
    db.commit()


def test_不同的键各占各的(db, book):
    claimed_a, _ = idem.claim(db, book_id=book.id, key="vch-a", request_hash="h1")
    claimed_b, _ = idem.claim(db, book_id=book.id, key="vch-b", request_hash="h1")
    assert claimed_a is True and claimed_b is True
    db.commit()


def test_不同的账套用同一把键不算冲突(db, book):
    """键的作用域是「账套 + 键」—— 两个账套各用各的。"""
    from app.models import Book
    other = Book(company_name="另一个公司", name="另一个账套",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    claimed_a, _ = idem.claim(db, book_id=book.id, key="vch-x", request_hash="h1")
    claimed_b, _ = idem.claim(db, book_id=other.id, key="vch-x", request_hash="h1")
    assert claimed_a is True and claimed_b is True
    db.commit()


def test_交结果之后占位行记着那张凭证(db, book, make_voucher):
    v = make_voucher("2026-03-02", [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    claimed, row = idem.claim(db, book_id=book.id, key="vch-c", request_hash="h1")
    assert claimed is True
    idem.complete(db, row, voucher_id=v.id)
    db.commit()
    db.expire_all()
    again = db.get(VoucherIdempotency, row.id)
    assert again.voucher_id == v.id


def test_键的格式不合法就拒绝():
    with pytest.raises(idem.InvalidIdempotencyKey):
        idem.validate_key("")
    with pytest.raises(idem.InvalidIdempotencyKey):
        idem.validate_key("有空格")
    with pytest.raises(idem.InvalidIdempotencyKey):
        idem.validate_key("x" * 129)
    assert idem.validate_key("  vch-abc-123  ") == "vch-abc-123"


def test_请求体的散列与内容有关():
    a = idem.request_hash({"x": 1, "y": [1, 2]})
    b = idem.request_hash({"y": [1, 2], "x": 1})
    c = idem.request_hash({"x": 2, "y": [1, 2]})
    assert a == b, "字段顺序不该影响散列"
    assert a != c


# --------------------------------------------------------------------------
# 接口层
# --------------------------------------------------------------------------


def test_不带键的请求照旧建两张(client, auth, book, subjects, db):
    """**没带键就不能拦** —— 老的客户端一个键都不带。"""
    for _ in range(2):
        r = client.post(f"/api/books/{book.id}/vouchers", headers=auth,
                        json=_payload(subjects))
        assert r.status_code == 201, r.text
    assert _count(db, book.id) == 2


def test_同一把键只建一张凭证(client, auth, book, subjects, db):
    key = "vch-fixed-1"
    body = _payload(subjects)
    first = client.post(f"/api/books/{book.id}/vouchers",
                        headers={**auth, "Idempotency-Key": key}, json=body)
    assert first.status_code == 201, first.text
    second = client.post(f"/api/books/{book.id}/vouchers",
                         headers={**auth, "Idempotency-Key": key}, json=body)
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["id"] == first.json()["id"]
    assert _count(db, book.id) == 1, "同一把键建出了不止一张凭证"


def test_回放之后内容变了也只回放同一张(client, auth, book, subjects, db):
    """客户端回放之后会补一次 PUT —— 所以「内容不同」不能变成 409。"""
    key = "vch-fixed-2"
    first = client.post(f"/api/books/{book.id}/vouchers",
                        headers={**auth, "Idempotency-Key": key},
                        json=_payload(subjects, amount="100.00"))
    assert first.status_code == 201, first.text
    second = client.post(f"/api/books/{book.id}/vouchers",
                         headers={**auth, "Idempotency-Key": key},
                         json=_payload(subjects, amount="200.00"))
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["id"] == first.json()["id"]
    assert _count(db, book.id) == 1


def test_不同的键各建各的(client, auth, book, subjects, db):
    for key in ("vch-a", "vch-b"):
        r = client.post(f"/api/books/{book.id}/vouchers",
                        headers={**auth, "Idempotency-Key": key},
                        json=_payload(subjects))
        assert r.status_code == 201, r.text
    assert _count(db, book.id) == 2


def test_键的格式不合法就400(client, auth, book, subjects):
    # HTTP 头只能是 ASCII —— 所以用「带空格」与「超长」这两种不合法形态，
    # 而不是中文（中文连发都发不出去，那不是服务端该管的事）。
    for bad in ("bad key", "x" * 200):
        r = client.post(f"/api/books/{book.id}/vouchers",
                        headers={**auth, "Idempotency-Key": bad},
                        json=_payload(subjects))
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert "幂等键" in r.text


def test_回放不新建凭证但内容能收敛(client, auth, book, subjects, db):
    """回放之后客户端补的那次 PUT，必须真的改到那张凭证上。"""
    key = "vch-fixed-3"
    body = _payload(subjects, amount="100.00")
    first = client.post(f"/api/books/{book.id}/vouchers",
                        headers={**auth, "Idempotency-Key": key}, json=body)
    assert first.status_code == 201, first.text
    vid = first.json()["id"]
    # 客户端把内容改了，再补一次 PUT
    put = client.put(f"/api/books/{book.id}/vouchers/{vid}", headers=auth,
                     json=_payload(subjects, amount="300.00"))
    assert put.status_code == 200, put.text
    assert _count(db, book.id) == 1
    db.expire_all()
    v = db.get(Voucher, vid)
    assert v is not None
    amounts = sorted(float(e.amount) for e in v.entries)
    assert amounts == [300.0, 300.0], f"补的那次 PUT 没落上：{amounts}"


def test_两个并发请求同一把键只建一张(client, auth, book, subjects, db):
    """**并发**才是最难的那一档：两个请求几乎同时到。

    它们抢的是同一把键 —— 数据库当场分出胜负，
    库里最后只有一张凭证，两个响应指向同一张。
    """
    key = "vch-race-1"
    body = _payload(subjects)
    url = f"/api/books/{book.id}/vouchers"

    def post_once():
        # 每个线程一个自己的 client：TestClient 不是线程安全的。
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app
        with TestClient(fastapi_app) as c:
            return c.post(url, headers={**auth, "Idempotency-Key": key}, json=body)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post_once(), range(2)))

    statuses = sorted(r.status_code for r in results)
    assert statuses in ([200, 201], [201, 201]), f"状态码不对：{statuses}"
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1, f"两个请求建出了两张凭证：{ids}"
    assert _count(db, book.id) == 1
