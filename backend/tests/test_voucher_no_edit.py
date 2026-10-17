"""修改凭证时对 voucher_no / insert_after 的处理（审计 P2-2）。

这两个字段以前是**静默丢弃**的：API 层把整个 ``payload.model_dump()`` 递进
服务层，而 ``update_voucher`` 从头到尾没读过它们。于是

* 客户端 PUT 一个新凭证号 → 拿到 200、审计日志写「修改凭证」、号一个字没变；
* 客户端 PUT 一个 insert_after → 同样 200，什么都没发生。

「返回成功却什么都没做」比报错难查得多 —— 用户以为改完了，去核对时才发现
号还是旧的。

两者处置不同，因为业务语义不同：

* ``voucher_no`` **该实现**。凭证号可回收复用是 ``next_voucher_no`` 取
  max+1 的前提，所以改号本来就是业务上允许的动作。
* ``insert_after`` **该拒绝**。「插在谁后面、后面的号整体顺位」是建凭证
  独有的动作，对一张已存在的凭证说这句话没有意义 —— 照着办要动**别的**
  凭证的号，那属于另一条业务规则（整理凭证）。
"""
from __future__ import annotations

import pytest


def _put(client, auth, book_id, voucher_id, payload):
    return client.put(f"/api/books/{book_id}/vouchers/{voucher_id}",
                      headers=auth, json=payload)


def test_改凭证号真的生效(client, auth, book, book_id, make_voucher):
    """回归点：原来 PUT voucher_no 返回 200 但号没变。"""
    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    r = _put(client, auth, book_id, v.id, {
        "voucher_date": "2026-01-05",
        "entries": [
            {"subject_id": v.entries[0].subject_id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": v.entries[1].subject_id, "direction": "CREDIT",
             "amount": "10", "summary": "x"},
        ],
        "voucher_no": "记077",
        "remark": "",
    })
    assert r.status_code == 200, r.text
    assert r.json()["voucher_no"] == "记077", (
        f"接口说成功了但号没变：仍是 {r.json()['voucher_no']}")


def test_改成一个已被占用的号要报错(client, auth, book, book_id, make_voucher):
    """撞 ``uq_voucher_period_no`` 要给人话，不能等数据库报约束名。"""
    taken = make_voucher("2026-01-06", dr=[("1002", 20, "x")], cr=[("5001", 20, "x")])
    other = make_voucher("2026-01-07", dr=[("1002", 30, "x")], cr=[("5001", 30, "x")])
    r = _put(client, auth, book_id, other.id, {
        "voucher_date": "2026-01-07",
        "entries": [
            {"subject_id": other.entries[0].subject_id, "direction": "DEBIT",
             "amount": "30", "summary": "x"},
            {"subject_id": other.entries[1].subject_id, "direction": "CREDIT",
             "amount": "30", "summary": "x"},
        ],
        "voucher_no": taken.voucher_no,
        "remark": "",
    })
    assert r.status_code == 400, f"撞号变成了 {r.status_code}：{r.text}"
    assert taken.voucher_no in r.text


def test_改成自己原来的号不算撞号(client, auth, book, book_id, make_voucher):
    """原样提交不该被自己的唯一约束挡下 —— 检查必须排除自己。"""
    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    r = _put(client, auth, book_id, v.id, {
        "voucher_date": "2026-01-05",
        "entries": [
            {"subject_id": v.entries[0].subject_id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": v.entries[1].subject_id, "direction": "CREDIT",
             "amount": "10", "summary": "x"},
        ],
        "voucher_no": v.voucher_no,
        "remark": "",
    })
    assert r.status_code == 200, r.text


def test_改期后号占用按新期间判(client, auth, book, book_id, make_voucher):
    """改了日期就换了号段 —— 唯一约束是 (period_id, voucher_no)。"""
    jan = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    feb = make_voucher("2026-02-05", dr=[("1002", 10, "y")], cr=[("5001", 10, "y")])
    # 把 jan 挪到 2 月并占用 feb 的号 —— 在 2 月里这是撞号
    r = _put(client, auth, book_id, jan.id, {
        "voucher_date": "2026-02-20",
        "entries": [
            {"subject_id": jan.entries[0].subject_id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": jan.entries[1].subject_id, "direction": "CREDIT",
             "amount": "10", "summary": "x"},
        ],
        "voucher_no": feb.voucher_no,
        "remark": "",
    })
    assert r.status_code == 400, f"换期间后撞号没被拦：{r.status_code} {r.text}"


def test_修改凭证不接受insert_after(client, auth, book, book_id, make_voucher):
    """回归点：原来带 insert_after 返回 200 且什么都没做。"""
    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    before = v.voucher_no
    r = _put(client, auth, book_id, v.id, {
        "voucher_date": "2026-01-05",
        "entries": [
            {"subject_id": v.entries[0].subject_id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": v.entries[1].subject_id, "direction": "CREDIT",
             "amount": "10", "summary": "x"},
        ],
        "insert_after": 999,
        "remark": "",
    })
    assert r.status_code == 400, f"insert_after 被静默接受了：{r.status_code} {r.text}"
    assert "insert_after" in r.text
    # 明确拒绝 ≠ 改了一半：号不能被动过
    got = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    assert got["voucher_no"] == before
