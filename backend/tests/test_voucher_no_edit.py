"""修改凭证时对 voucher_no / insert_after 的处理。

历史：这两个字段以前是**静默丢弃**的 —— API 层把整个 ``payload.model_dump()``
递进服务层，而 ``update_voucher`` 从头到尾没读过它们。客户端 PUT 一个新凭证号
拿到 200、审计日志写「修改凭证」、号一个字没变。「返回成功却什么都没做」比报错
难查得多，于是当年人把 ``voucher_no`` **实现**掉了。

现状（用户 2026-10-19 定的规则：新增 / 插入 / 修改三处凭证号一律只读，
只能用系统自动生成的号）：实现出来的那把刀被收起来了。生产上就是这么出的事——
优境 9 月一张 18 条分录的工资凭证，凭证号在这个页面上被改成 "wochakan"，
凭证列表四种排序全部 500（排序键要对凭证号做 CAST AS INTEGER），整月打不开。

所以现在的契约是**明确拒绝**而不是静默忽略：

* ``voucher_no`` **拒绝改号**。要调号段走「整理凭证」，它整体重排并留审计。
  原样提交自己那个号不算改号（自动保存每次都带这个字段，拦下来就等于把
  「改个日期」也一起挡了）。
* ``insert_after`` 仍然拒绝。「插在谁后面、后面的号整体顺位」是建凭证独有的
  动作，对一张已存在的凭证说这句话没有意义 —— 照着办要动**别的**凭证的号。
"""
from __future__ import annotations

import pytest


def _put(client, auth, book_id, voucher_id, payload):
    return client.put(f"/api/books/{book_id}/vouchers/{voucher_id}",
                      headers=auth, json=payload)


def test_改凭证号被拒绝并指向整理凭证(client, auth, book, book_id, make_voucher):
    """手工改号要报人话，且说清正确的出路是「整理凭证」。"""
    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    r = _put(client, auth, book_id, v.id, {
        "voucher_date": "2026-01-05",
        "entries": [
            {"subject_id": v.entries[0].subject_id, "direction": "DEBIT",
             "amount": "10", "summary": "x"},
            {"subject_id": v.entries[1].subject_id, "direction": "CREDIT",
             "amount": "10", "summary": "x"},
        ],
        "voucher_no": "记0077",
        "remark": "",
    })
    assert r.status_code == 400, f"改号被放行了：{r.status_code} {r.text}"
    assert "整理凭证" in r.json()["detail"], r.json()["detail"]
    # 拒绝之后号必须**真的没变** —— 返回 400 却在事务里改了数据，比报错更糟
    db_check = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth)
    assert db_check.json()["voucher_no"] == v.voucher_no


def test_改成一个已被占用的号同样被拒(client, auth, book, book_id, make_voucher):
    """撞号的号当然也拒 —— 但现在拒绝的理由是"不许改号"，不是撞号。

    保留这条是因为它守的是另一件事：**任何**目标号都别想从 PUT 进去。
    """
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
    assert "整理凭证" in r.text


def test_原样提交自己那个号不算改号(client, auth, book, book_id, make_voucher):
    """自动保存每次都带 voucher_no 原值，拦下来就等于把「改个日期」也挡了。"""
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
    assert r.json()["voucher_no"] == v.voucher_no


def test_只改日期不改号是可以的(client, auth, book, book_id, make_voucher):
    """凭证号锁死不等于这张凭证不能改 —— 挪到别的期间是正常业务动作。

    号跟着走期间（号段是按期间分的），所以挪月份不需要、也不允许改号。
    """
    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    r = _put(client, auth, book_id, v.id, {
        "voucher_date": "2026-02-20",
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
    assert r.json()["voucher_date"] == "2026-02-20"
    assert r.json()["voucher_no"] == v.voucher_no


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
