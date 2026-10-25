"""账套审计流水的 ``limit`` 必须夹下限（审计 P3-11，报告 §3.11）。

``GET /api/books/{book_id}/audit-logs`` 的 ``limit`` 以前**只写了上限**
（``Query(100, le=1000)``），于是 ``?limit=-1`` 一路传到 SQL 的
``LIMIT -1``，而 PostgreSQL 对负 LIMIT 报
``InvalidRowCountInLimitClause``（sqlstate 2201W）—— 那不在
``app/main.py`` 的 4xx 白名单里，于是回一句**脱敏的 500**：
明明是参数写错，却告诉调用方「服务器坏了」。

同仓的兄弟接口都夹取（``system.py:537`` 的 ``min(max(limit,1),500)``、
``system.py:1219``、``backup/service.py:318``），这里是唯一一处漏掉的。

**为什么按 ``ge=1`` 而不是夹取值。** 两种做法都能把 500 变成 4xx：

* 夹取值（``min(max(limit,1),500)``）→ ``?limit=-1`` 回 200，静默地按 1 条返回；
* 参数校验（``ge=1``）→ ``?limit=-1`` 回 422，**明确告诉调用方参数不合法**。

审计报告 §3.11 写死的是后者（``Query(100, ge=1, le=1000)``），本仓也
倾向「说得清」而不是「悄悄改写调用方的意思」：静默把 -1 当成 1 返回，
调用方不会知道自己问错了。这里按报告的口径钉住 422。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


def test_负数limit不是500(client, auth, book):
    """**这条就是审计报告 §3.11 的复现**：``?limit=-1`` 以前回 500。"""
    r = client.get(f"/api/books/{book.id}/audit-logs?limit=-1", headers=auth)
    assert r.status_code != 500, (
        f"拿到 500：负 LIMIT 让 PostgreSQL 报 InvalidRowCountInLimitClause，"
        f"而它不在 main.py 的 4xx 白名单里，于是脱敏成了一句「服务器坏了」。"
        f"响应体 {r.text[:200]}")
    assert r.status_code == 422, (
        f"拿到 {r.status_code}：按报告的口径（ge=1）应当回 422。"
        f"响应体 {r.text[:200]}")


@pytest.mark.parametrize("limit", [-1, -100, 0])
def test_各种非正数的limit都被参数校验挡住(client, auth, book, limit):
    r = client.get(f"/api/books/{book.id}/audit-logs?limit={limit}", headers=auth)
    assert r.status_code == 422, f"limit={limit} 拿到 {r.status_code}：{r.text[:200]}"


def test_合法limit照常返回200(client, auth, book):
    """**收紧不能把功能也堵了。**"""
    r = client.get(f"/api/books/{book.id}/audit-logs?limit=10", headers=auth)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_不传limit用默认值(client, auth, book):
    r = client.get(f"/api/books/{book.id}/audit-logs", headers=auth)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_超过上限仍然是422(client, auth, book):
    """上限那道闸没被这次调整弄丢。"""
    r = client.get(f"/api/books/{book.id}/audit-logs?limit=1001", headers=auth)
    assert r.status_code == 422, f"拿到 {r.status_code}：{r.text[:200]}"


def test_负数limit没有打到数据库(client, auth, book, db):
    """负 LIMIT 之所以炸，是因为 SQL 一路走到了 ``LIMIT -1``。

    这条钉的是「请求在**进数据库之前**就被挡下」—— 万一有人把 ``ge=1``
    去掉、只留夹取值，这条会提醒你那条 SQL 又能拿到负数了。
    """
    before = db.scalar(text("SELECT count(*) FROM audit_log"))
    r = client.get(f"/api/books/{book.id}/audit-logs?limit=-1", headers=auth)
    assert r.status_code != 500
    db.expire_all()
    after = db.scalar(text("SELECT count(*) FROM audit_log"))
    assert after == before, "被拒的请求居然改了数据"
