"""乐观锁契约：客户端带着「我读到的那一版」回来，不一致就 409（审计 §3.15）。

**为什么行锁之外还要这一层。**

行锁（``update_voucher`` 里的 ``with_for_update``）挡住的是
「**已记账**的凭证被并发改写」—— 那是 §3.15 里最严重的那一条
（数字错了，但借贷平、试算平、科目余额、总账、「整理就绪」体检**全过**）。

但**未记账**的凭证没有那道闸：两个人同时改同一张凭证，
A 存完 B 再存 —— B 的那份覆盖 A 的，**A 的改动人间蒸发**，而账上看着还是平的。

行锁在那一刻只会串行化两次保存，**不会告诉 B「你盖掉的是别人的东西」**。
这一层就是那句话：客户端把它读到的那一版带回来，服务端拿库里此刻的那一版比一下，
不一致就 409。

**不带 = 不比。** 老客户端一个字段都不带，服务端照旧按行锁判断 ——
所以这个契约是**加法**，不是破坏。

**令牌格式只有一份**（``accounting.vouchers.version_token``）：
它是客户端与服务端之间的一枚硬币，两边格式必须一模一样，
差一个时区写法就会「带了也永远判过期」。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.accounting.vouchers import version_token
from app.models import Voucher


def _payload(subjects, amount="100.00"):
    return {
        "voucher_date": "2026-03-02",
        "entries": [
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": amount, "summary": "版本"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": amount, "summary": "版本"},
        ],
    }


def test_单张响应带着令牌(client, auth, book_id, subjects, make_voucher):
    """改一张凭证之前，客户端得先拿到令牌 —— 不然它没东西可带。"""
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    r = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth)
    assert r.status_code == 200, r.text
    token = r.json().get("version")
    assert token, "单张响应必须带 version 令牌"
    assert token == version_token(v.updated_at), "令牌格式必须与权威实现一致"


def test_列表响应不带令牌(client, auth, book_id, subjects, make_voucher):
    """列表**不**带令牌。

    一次 5000 张的列表，每个格子多 40 字节就是 200KB ——
    而列表页不做行内编辑（编辑走单张 GET），根本用不上这个令牌。
    """
    make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    r = client.get(f"/api/books/{book_id}/vouchers", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "列表不该是空的"
    assert "version" not in items[0], "列表不该带令牌（白白多几百 KB）"


def test_带当前令牌能存进去(client, auth, book_id, subjects, make_voucher):
    """令牌对得上 → 正常保存，并且**换一枚新令牌**回来。"""
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    detail = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    token = detail["version"]
    body = _payload(subjects, "20.00")
    body["remark"] = detail["remark"]        # 原样带回，不算改动
    body["version"] = token
    r = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth, json=body)
    assert r.status_code == 200, r.text
    new_token = r.json().get("version")
    assert new_token and new_token != token, (
        "保存之后必须换一枚新令牌 —— 令牌是一次性的，旧的下次会被自己判成过期")


def test_只改分录时令牌也要变(client, auth, book_id, subjects, make_voucher):
    """**只改分录**（备注、日期、状态都不动）时，令牌**必须**变。

    这一条曾经是错的，而且**看起来是绿的**：只改分录时 voucher 行**没有任何净变化**
    （status 本来就是 UNPOSTED、备注没动、附单据数没动）→ SQLAlchemy 眼里
    「什么都没改」→ **根本不生成 UPDATE 语句** → 库层 ``fn_touch_updated_at``
    触发器不跑 → ``updated_at`` 不动 → **令牌不动**。

    后果有两处：
      · 令牌**抓不到并发改分录** —— 而那恰恰是最常见的那一类改动；
      · 客户端下一次保存拿的还是同一个令牌，比对下来「相等」——
        看起来有保护，实际没有。

    所以「改这张凭证」必须**真的**让 voucher 行被 UPDATE 一遍
    （见 ``accounting.vouchers.update_voucher`` 里那段说明）。
    """
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    detail = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    token = detail["version"]
    body = {
        "voucher_date": detail["voucher_date"],
        "remark": detail["remark"],           # 原样带回 —— 这一版只改分录
        "attachments": detail["attachments"],
        "entries": [
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "55.00", "summary": "借"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "55.00", "summary": "贷"},
        ],
        "version": token,
    }
    r = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["version"] != token, (
        "只改分录时令牌没动 —— 令牌抓不到并发改分录，等于没有这道闸")


def test_并发改分录会被409挡住(client, auth, book_id, subjects, make_voucher):
    """**两个人同时改同一张未记账凭证** —— 令牌要挡下后保存的那个。

    行锁管不到这一段：它只串行化两次保存，**不会告诉后保存的人
    「你盖掉的是别人的东西」**。这句提示就是令牌给的。
    """
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    detail = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    my_token = detail["version"]

    # 别人先改了一次（他手上是同一枚令牌）
    theirs = {
        "voucher_date": detail["voucher_date"], "remark": detail["remark"],
        "attachments": detail["attachments"],
        "entries": [
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "30.00", "summary": "借"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "30.00", "summary": "贷"},
        ],
        "version": my_token,
    }
    ok = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth, json=theirs)
    assert ok.status_code == 200, ok.text

    # 我这份令牌已经过期了
    mine = {
        "voucher_date": detail["voucher_date"], "remark": detail["remark"],
        "attachments": detail["attachments"],
        "entries": [
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "99.00", "summary": "借"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "99.00", "summary": "贷"},
        ],
        "version": my_token,
    }
    r = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth, json=mine)
    assert r.status_code == 409, r.text
    after = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    amounts = sorted(float(e["amount"]) for e in after["entries"])
    assert amounts == [30.0, 30.0], f"后保存的那版盖上去了：{amounts}"


def test_带着过期的令牌回409(client, auth, book_id, subjects, make_voucher):
    """**别人改过了** → 409，且这一版**没有**被存进去。

    这就是行锁管不到的那一段：两个人同时改同一张**未记账**的凭证。
    """
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    token = client.get(f"/api/books/{book_id}/vouchers/{v.id}",
                        headers=auth).json()["version"]

    # 别人改了一次（同一个接口，另一次保存）
    other = _payload(subjects, "30.00")
    other["version"] = token
    ok = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth, json=other)
    assert ok.status_code == 200, ok.text

    # 我这份是**过期的**令牌
    mine = _payload(subjects, "99.00")
    mine["version"] = token
    r = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth, json=mine)
    assert r.status_code == 409, r.text
    assert "改动" in r.text, r.text

    # 账上还是**别人**那一版 —— 我的没有覆盖上去
    detail = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    amounts = sorted(float(e["amount"]) for e in detail["entries"])
    assert amounts == [30.0, 30.0], f"过期的那一版居然盖上去了：{amounts}"


def test_不带令牌照旧能存(client, auth, book_id, subjects, make_voucher):
    """**不带 = 不比**：老客户端一个字段都不带，行为一个字没变。"""
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    r = client.put(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth,
                   json=_payload(subjects, "40.00"))
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/books/{book_id}/vouchers/{v.id}", headers=auth).json()
    amounts = sorted(float(e["amount"]) for e in detail["entries"])
    assert amounts == [40.0, 40.0]


def test_令牌只由一份实现生成(db, book, make_voucher):
    """令牌格式只有 ``accounting.vouchers.version_token`` 一份。

    两份写法相同的代码就是审计 §4.5 说的「同一个量算两遍」——
    哪天有人把时区写法改了一处，客户端带回来的令牌就会**永远**判成过期。
    """
    v = make_voucher(date(2026, 3, 2), [("1001", "10.00", "借")], [("5001", "10.00", "贷")])
    assert version_token(v.updated_at) == v.updated_at.isoformat()
    assert version_token(None) == ""
