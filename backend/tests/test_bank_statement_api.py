"""流水对账的**接口契约**：路径、权限、返回形状。

这一组盯的是「接口这一层」而不是业务逻辑（业务逻辑在
``test_bank_statement_service.py`` 里）：

  · 路径挂在 ``/api/books/{book_id}/bank-statements`` 下；
  · 写接口挂了 ``require_book_write``（只读账号不能上传、不能制证）；
  · 列表接口带上「账面余额 / 差额」—— 页面不用再发一轮请求去拼这些数；
  * 行级接口能改对方科目、能定方向、能忽略。
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.models import BankStatement, BankStatementRow, Subject

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def cash(db, book):
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    parent.is_leaf = False
    db.flush()
    sub = Subject(book_id=book.id, year=book.start_year, code="100201", name="基本户",
                  parent_id=parent.id, level=parent.level + 1, category="ASSET",
                  direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=1,
                  active=True)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _upload(client, auth, book, cash, name="cmb_csv.xlsx"):
    data = (FIXTURES / name).read_bytes()
    return client.post(
        f"/api/books/{book.id}/bank-statements",
        headers=auth,
        files={"file": (name, data, "application/octet-stream")},
        data={"account_subject_id": str(cash.id)},
    )


# ---------------------------------------------------------------------- 上传


def test_超大小的文件被拒(client, auth, book, cash, store, monkeypatch):
    """文件大小沿用 ``core/uploads.py`` 那把尺 —— 超了直接 413，不进内存。

    把上限调到很小来测：真的造一个几十 MB 的文件既慢又占地方，
    而这道闸看的就是「声明的大小 vs 上限」。
    """
    monkeypatch.setattr(settings, "import_max_bytes", 64)
    r = _upload(client, auth, book, cash)
    assert r.status_code == 413, r.text
    assert "超过上限" in r.json()["detail"], r.text


def test_上传后能列出对账单(client, auth, db, book, cash, store):
    r = _upload(client, auth, book, cash)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["row_count"] == 5
    assert body["container_format"] == "CSV"

    r = client.get(f"/api/books/{book.id}/bank-statements", headers=auth)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["filename"] == "cmb_csv.xlsx"
    # **列表里就带着差额** —— 页面不用再发一轮请求去拼这些数字
    assert "reconcile" in items[0]
    assert "status_counts" in items[0]


def test_上传时要选现金科目(client, auth, book, cash, store):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    r = client.post(
        f"/api/books/{book.id}/bank-statements",
        headers=auth,
        files={"file": ("x.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 422, r.text


def test_重复上传被拒(client, auth, book, cash, store):
    assert _upload(client, auth, book, cash).status_code == 201
    r = _upload(client, auth, book, cash)
    assert r.status_code == 400
    assert "已经有一份对账单" in r.json()["detail"]


def test_只读账号不能上传(client, db, book, cash, store, plain_auth):
    r = _upload(client, plain_auth, book, cash)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------- 明细


def test_取流水行(client, auth, book, cash, store):
    created = _upload(client, auth, book, cash).json()
    r = client.get(f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
                   headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 5
    assert rows[0]["seq"] == 1
    assert rows[0]["voucher_state"] in ("READY", "PENDING", "UNKNOWN", "NOAMOUNT")


def test_改对方科目(client, auth, db, book, cash, store, subjects):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    assert r.status_code == 200, r.text
    assert r.json()["plan_status"] == "MANUAL"


def test_人工指定方向(client, auth, db, book, cash, store):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.post(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}/direction",
        headers=auth, json={"direction": "IN"})
    assert r.status_code == 200, r.text
    assert r.json()["direction"] == "IN"


def test_忽略一行(client, auth, db, book, cash, store):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"status": "IGNORED"})
    assert r.status_code == 200
    assert r.json()["voucher_state"] == "IGNORED"


# ---------------------------------------------------------------------- 制证


def test_计划与提交凭证(client, auth, db, book, cash, store, subjects):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    client.post(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}/direction",
        headers=auth, json={"direction": "IN"})

    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/plan-vouchers",
        headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["candidate_count"] >= 1

    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["created_count"] >= 1


def test_只读账号不能制证(client, auth, db, book, cash, store, plain_auth, subjects):
    created = _upload(client, auth, book, cash).json()
    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=plain_auth)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------- 规则与画像


def test_流水科目规则的增删(client, auth, db, book, subjects):
    r = client.post(f"/api/books/{book.id}/bank-statement-rules", headers=auth, json={
        "match_field": "SUMMARY", "keyword": "运费",
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    assert r.status_code == 201, r.text
    rule_id = r.json()["id"]
    assert r.json()["match_label"] == "摘要"

    r = client.get(f"/api/books/{book.id}/bank-statement-rules", headers=auth)
    assert len(r.json()) == 1

    r = client.delete(f"/api/books/{book.id}/bank-statement-rules/{rule_id}",
                      headers=auth)
    assert r.status_code == 200
    r = client.get(f"/api/books/{book.id}/bank-statement-rules", headers=auth)
    assert r.json() == []


def test_格式画像的存与删(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/bank-statement-profiles", headers=auth,
                    json={"fingerprint": "abc123", "column_mapping": {"DATE": 0}})
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]
    r = client.get(f"/api/books/{book.id}/bank-statement-profiles", headers=auth)
    assert len(r.json()) == 1
    r = client.delete(f"/api/books/{book.id}/bank-statement-profiles/{profile_id}",
                      headers=auth)
    assert r.status_code == 200


# ---------------------------------------------------------------------- 隔离


def test_跨账套看不到别人的对账单(client, auth, db, book, cash, store, admin_user):
    from app.models import Book, UserBook

    created = _upload(client, auth, book, cash).json()
    other = Book(company_name="别家", name="别家账套", start_year=2026,
                 start_month=1, subject_code_rule="4-2-2-2", status="ACTIVE")
    db.add(other)
    db.commit()
    db.refresh(other)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    r = client.get(f"/api/books/{other.id}/bank-statements", headers=auth)
    assert r.status_code == 200
    assert r.json() == []
    # 直接按 id 去取别的账套的那一份也不行
    r = client.get(f"/api/books/{other.id}/bank-statements/{created['id']}",
                   headers=auth)
    assert r.status_code == 404


def test_只改状态不该顺手清掉对方科目(client, auth, db, book, cash, store, subjects):
    """改一个字段不许抹掉另一个。

    少了 ``exclude_unset`` 的话，``model_dump()`` 会把没传的字段也带上
    （值是 None）—— 「只改状态」那次调用顺手把对方科目清空了。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    assert r.json()["plan_subject_id"] == subjects["560202"].id

    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"status": "IGNORED"})
    assert r.status_code == 200, r.text
    assert r.json()["plan_subject_id"] == subjects["560202"].id, (
        "只改状态就把对方科目清空了")
    assert r.json()["status"] == "IGNORED"
