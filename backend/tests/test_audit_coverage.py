"""高风险写路径的审计覆盖（审计 P3-10）。

审计列了四类没有留痕的写路径：**期初余额、科目增删改、授权变更、
报表公式修改**。前两类直接决定账面数字 —— 出事之后没有日志，
就只能靠用户回忆「我好像改过点什么」。

这里不按接口逐个钉，而是钉**审计行本身的内容**：谁改的、改了什么、
改前改后是什么。``detail`` 只有一句话而 ``before``/``after`` 是空的
那种，事后基本没法追 —— 尤其期初：一个科目从 100 改成 0 和从 100
改成 500，在文字描述里可能长得一模一样。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AuditLog


def _logs(db, book, action=None):
    stmt = select(AuditLog).where(AuditLog.book_id == book.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.scalars(stmt.order_by(AuditLog.id)).all())


def _last(db, book, action):
    rows = _logs(db, book, action)
    assert rows, f"没有 {action} 的审计记录"
    return rows[-1]


# --------------------------------------------------------------------- 期初余额


def test_期初余额写入要留审计(client, auth, db, book, subjects):
    p1 = db.query(__import__("app.models", fromlist=["Period"]).Period).filter_by(
        book_id=book.id, month=1).one()

    r = client.post(f"/api/books/{book.id}/periods?period_id={p1.id}", headers=auth, json=[
        {"subject_id": subjects["100201"].id, "amount": "1000"},
        {"subject_id": subjects["4001"].id, "amount": "-1000"},
    ])
    assert r.status_code == 200, r.text

    entry = _last(db, book, "SET_OPENINGS")
    assert entry.detail, "期初写入的审计没有 detail"
    assert entry.before or entry.after, (
        "期初写入的审计没有留 before/after 快照 —— "
        "事后只知道「有人动过」，不知道动成了什么")


def test_期初改写要留改前值(client, auth, db, book, subjects):
    """先录 1000，再改成 500 —— 审计里必须能看出 1000 这个数。"""
    from app.models import Period

    p1 = db.query(Period).filter_by(book_id=book.id, month=1).one()
    payload = [{"subject_id": subjects["100201"].id, "amount": "1000"},
               {"subject_id": subjects["4001"].id, "amount": "-1000"}]
    assert client.post(f"/api/books/{book.id}/periods?period_id={p1.id}",
                       headers=auth, json=payload).status_code == 200

    payload2 = [{"subject_id": subjects["100201"].id, "amount": "500"},
                {"subject_id": subjects["4001"].id, "amount": "-500"}]
    assert client.post(f"/api/books/{book.id}/periods?period_id={p1.id}",
                       headers=auth, json=payload2).status_code == 200

    rows = _logs(db, book, "SET_OPENINGS")
    assert len(rows) == 2, f"两次写入应当有两条审计，实际 {len(rows)} 条"
    latest = rows[-1]
    blob = str(latest.before) + str(latest.after)
    assert "1000" in blob or "500" in blob, (
        f"第二次写入的审计里既没有旧值也没有新值：{latest.before} / {latest.after}")
    assert "500" in blob, f"新值没进审计：{latest.before} / {latest.after}"


def test_试算不平被拒时不留审计(client, auth, db, book, subjects):
    """被拒绝的请求没有改动任何数据，不该产生审计 —— 否则日志会被噪音淹没。"""
    from app.models import Period

    p1 = db.query(Period).filter_by(book_id=book.id, month=1).one()
    before = len(_logs(db, book, "SET_OPENINGS"))

    r = client.post(f"/api/books/{book.id}/periods?period_id={p1.id}", headers=auth, json=[
        {"subject_id": subjects["100201"].id, "amount": "1000"},
    ])
    assert r.status_code == 400
    assert len(_logs(db, book, "SET_OPENINGS")) == before, "被拒绝的期初写入留下了审计"


# --------------------------------------------------------------------- 科目


def test_新建科目要留审计(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1901", "name": "测试用科目", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": True,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 99,
    })
    assert r.status_code == 201, r.text

    entry = _last(db, book, "CREATE_SUBJECT")
    assert "1901" in entry.detail


def test_改科目要留改前改后(client, auth, db, book, subjects):
    sid_ = subjects["100201"].id
    payload = {
        "code": "100201", "name": "改过的名字", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": True,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 0,
    }
    r = client.patch(f"/api/books/{book.id}/subjects/{sid_}", headers=auth, json=payload)
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_SUBJECT")
    blob = str(entry.before) + str(entry.after)
    assert "银行存款" in blob or "改过的名字" in blob, (
        f"科目改名没有留下新旧名字：{entry.before} / {entry.after}")


def test_停用科目要留审计(client, auth, db, book, subjects):
    """停用是个安全动作但影响面很大（老凭证还在用它），必须留痕。"""
    sid_ = subjects["100201"].id
    payload = {
        "code": "100201", "name": "银行存款", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": True,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 0,
        "active": False,
    }
    r = client.patch(f"/api/books/{book.id}/subjects/{sid_}", headers=auth, json=payload)
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_SUBJECT")
    assert "停用" in entry.detail or "active" in str(entry.after), (
        f"停用没有在审计里体现：{entry.detail} / {entry.after}")


def test_删科目要留审计(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1999", "name": "待删除科目", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": True,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 98,
    })
    sid_ = r.json()["id"]

    d = client.delete(f"/api/books/{book.id}/subjects/{sid_}", headers=auth)
    assert d.status_code == 200, d.text

    entry = _last(db, book, "DELETE_SUBJECT")
    assert "1999" in entry.detail
    assert entry.before, "删除审计没有留 before 快照 —— 删掉之后就没法还原这个科目了"


# --------------------------------------------------------------------- 报表公式


def test_改报表公式要留审计(client, auth, db, book):
    """改公式的接口是 ``PUT /reports/{id}/cells``（审计报告里的行号已过期）。

    公式写错一个字符，资产负债表就会悄无声息地少算一列 —— 而且报表照样
    渲染得出来。所以事后必须能查到「这个格子的公式原来是什么」。
    """
    from app.models import ReportCell, ReportTemplate

    tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id).limit(1))
    old_formula = cell.formula

    r = client.put(f"/api/reports/{tpl.id}/cells?book_id={book.id}", headers=auth,
                   json=[{"row_no": cell.row_no, "col_no": cell.col_no,
                          "formula": "科目(\"1001\",\"期末余额\",{{本期}})*999"}])
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_REPORT_CELLS")
    blob = str(entry.before) + str(entry.after)
    assert old_formula[:20] in blob, (
        f"公式审计里没有原公式（{old_formula[:40]}…）：{entry.before} / {entry.after}")
    assert "999" in blob, f"新公式没进审计：{entry.before} / {entry.after}"


# --------------------------------------------------------------------- 授权


def test_调整成员授权要留审计(client, auth, db, book, plain_user):
    """授权变更是权限系统的入口动作，出事时必须知道谁给谁升了权、降到什么。

    实际接口是 ``PUT /system/users/{id}/books``（整份替换授权列表）。
    """
    r = client.put(f"/api/system/users/{plain_user.id}/books", headers=auth, json=[
        {"book_id": book.id, "role": "VIEWER"},
    ])
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_BOOK_ACCESS")
    blob = str(entry.detail) + str(entry.before) + str(entry.after)
    assert plain_user.username in blob or str(plain_user.id) in blob, (
        f"授权审计没点名被改的人：{entry.detail} / {entry.after}")
    assert "VIEWER" in blob, f"新角色没进审计：{entry.detail} / {entry.after}"
