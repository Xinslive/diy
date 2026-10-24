"""发票「确认生成凭证」的并发闸门与异常脱敏（审查报告 P0-1 / P1-1 / P1-2 / P1-3）。

三件事钉在这里：

1. **并发「确认生成」只能生成一张凭证。** 原来三道检查全是「先读后写」的
   无锁读，两个并发的确认请求各自读到「还没人提交过」，于是双双建了一套
   凭证 —— 同一张发票的费用/收入被重复入账，而批次摘要还少报一张。
   现在批次行被 ``SELECT … FOR UPDATE`` 锁住，第二个请求**确定性地**失败
   并拿到「该批次已是 COMMITTED 状态」。
2. **数据库异常不回显。** ``str(SQLAlchemyError)`` 里有整条 SQL、绑定参数
   （凭证金额、摘要、发票号码）、表名、列名与约束名 —— 一律只进服务端日志，
   响应里只剩一句脱敏提示 + 请求号。
3. **并发上传同一份发票给的是人话。** 唯一约束违约要翻成「发票已存在」，
   而不是把约束名与 INSERT 语句回给客户端。
"""
from __future__ import annotations

import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.invoice import service
from app.models import InvoiceBatch, InvoiceRecord, Voucher

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "invoices"
BUYER_TAX_ID = "91420100MA1234567X"

#: 出现在假异常里的业务数据：泄露探针要盯的就是这几样。
FAKE_INVOICE_NO = "24417000000011111111"
FAKE_AMOUNT = Decimal("106.00")
FAKE_SUMMARY = f"发票 {FAKE_INVOICE_NO}（含税）"
FAKE_SQL = ("INSERT INTO entry (voucher_id, book_id, period_ord, line_no, subject_id, "
            "direction, amount, summary) VALUES (%(voucher_id)s, %(book_id)s, "
            "%(amount)s, %(summary)s)")


def _pdf_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """发票原件落到临时目录 —— 绝不写进仓库的 data/invoices。"""
    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path / "invoices"))
    return tmp_path / "invoices"


@pytest.fixture
def configured(db, book, admin_user, subjects):
    """配好税号与科目规则：预览里不该因为「没配科目」而全红。"""
    service.save_config(db, book.id, {
        "taxpayer_id": BUYER_TAX_ID,
        "output_tax_subject_id": subjects["22210106"].id,
        "input_credit_subject_id": subjects["1002"].id,
        "output_debit_subject_id": subjects["1002"].id,
    })
    for keyword, code in (("餐饮", "560202"), ("保洁", "560207")):
        service.create_rule(db, book.id, admin_user, {
            "match_type": "PROJECT", "invoice_type": "BOTH", "keyword": keyword,
            "subject_id": subjects[code].id, "priority": 0, "active": True,
        })
    return book


def _post_files(client, book, auth, files: list[tuple[str, bytes]]):
    return client.post(f"/api/books/{book.id}/invoices/batches", headers=auth, files=[
        ("files", (name, data, "application/pdf")) for name, data in files
    ])


def _upload_one(client, book, auth, name: str = "10月8日.pdf") -> int:
    created = _post_files(client, book, auth, [(name, _pdf_bytes("inv_a.pdf"))])
    assert created.status_code == 201, created.text
    return created.json()["id"]


# --------------------------------------------------------------------- P0-1


def test_并发确认生成只生成一张凭证(client, auth, configured, store, monkeypatch):
    """两个并发的「确认生成」：只能有**一张**凭证、一个 200、另一个 409。

    原来这条路的每一道检查都是「先读后写」的**无锁读**，两个事务各自读到
    「还没人提交过」，于是各建了一套凭证 —— 费用/收入被重复入账，而批次
    摘要还报「成功生成 1 张凭证」，界面上一眼看不出异常。
    """
    batch_id = _upload_one(client, configured, auth)

    # 让第一个请求在生成凭证时慢一点，两个请求**真正重叠**。
    # （不打这个桩也可能重叠，但那样这条用例就退化成「顺序点两下」，
    #    顺序点两下在修复前也是过的 —— 那样的用例抓不住缺陷。）
    original = service.voucher_service.create_voucher
    slow = threading.Event()

    def slow_create(db, **kwargs):
        slow.set()
        time.sleep(0.4)
        return original(db, **kwargs)

    monkeypatch.setattr(service.voucher_service, "create_voucher", slow_create)

    url = f"/api/books/{configured.id}/invoices/batches/{batch_id}/commit"
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        return client.post(url, headers=auth)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(fire), pool.submit(fire)]]

    assert slow.is_set(), "两个请求没有真正进到生成凭证那一步"

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409], [(r.status_code, r.text) for r in responses]

    # 账上只有一张凭证，而且发票记录能追溯到它。
    listed = client.get(f"/api/books/{configured.id}/vouchers", headers=auth).json()
    mine = [v for v in listed["items"] if v.get("remark") == f"发票 {FAKE_INVOICE_NO}"]
    assert len(mine) == 1, f"账上多出了凭证：{[v.get('remark') for v in listed['items']]}"

    batch = client.get(f"/api/books/{configured.id}/invoices/batches/{batch_id}",
                       headers=auth).json()
    assert batch["status"] == "COMMITTED"
    assert batch["committed_count"] == 1, "批次摘要必须与实际张数一致"

    record = next(r for r in batch["invoices"] if r["invoice_no"] == FAKE_INVOICE_NO)
    assert record["voucher_id"] == mine[0]["id"], "发票记录必须能追溯到那张凭证"
    assert record["status"] == "COMMITTED"


def test_并发落败的那一路拿到的是人话(client, auth, configured, store, monkeypatch):
    """落败的那一路**不能**拿到一坨 SQL，要拿到「该批次已是 COMMITTED 状态」。"""
    batch_id = _upload_one(client, configured, auth)
    original = service.voucher_service.create_voucher

    def slow_create(db, **kwargs):
        time.sleep(0.4)
        return original(db, **kwargs)

    monkeypatch.setattr(service.voucher_service, "create_voucher", slow_create)

    url = f"/api/books/{configured.id}/invoices/batches/{batch_id}/commit"
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        return client.post(url, headers=auth)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(fire), pool.submit(fire)]]

    loser = next(r for r in responses if r.status_code == 409)
    detail = loser.json()["detail"]
    assert "COMMITTED" in detail, detail
    for fragment in ("[SQL:", "INSERT INTO", "psycopg", "constraint", FAKE_INVOICE_NO):
        assert fragment not in loser.text, loser.text


def test_顺序再点一次确认也是409(client, auth, configured, store):
    """同一个批次确认第二次 —— 确定性失败，不生成第二张凭证。"""
    batch_id = _upload_one(client, configured, auth)
    url = f"/api/books/{configured.id}/invoices/batches/{batch_id}/commit"

    first = client.post(url, headers=auth)
    assert first.status_code == 200, first.text
    second = client.post(url, headers=auth)
    assert second.status_code == 409, second.text
    assert "COMMITTED" in second.json()["detail"]


# ----------------------------------------------------------------- P1-1 / P1-2


def _fake_integrity_error() -> IntegrityError:
    return IntegrityError(
        FAKE_SQL,
        {"voucher_id": 1, "book_id": 2, "amount": FAKE_AMOUNT, "summary": FAKE_SUMMARY},
        Exception("duplicate key value violates unique constraint \"uq_voucher_period_no\""),
    )


def test_数据库异常只回一句脱敏提示加请求号(client, auth, configured, store, monkeypatch):
    """数据库异常**不得**把 SQL、绑定参数、业务数据、约束名回给客户端。

    绑定参数里装的是**真实账务数据**（凭证金额、摘要、发票号码）——
    泄露它们等于把账抄送给任意一个有账套写权限的普通成员。
    """
    batch_id = _upload_one(client, configured, auth)

    def boom(*args, **kwargs):
        raise _fake_integrity_error()

    monkeypatch.setattr(service, "commit_batch", boom)

    response = client.post(
        f"/api/books/{configured.id}/invoices/batches/{batch_id}/commit", headers=auth)

    assert response.status_code == 500, response.text
    body = response.text
    for fragment in ("[SQL:", "INSERT INTO", "SELECT", "[parameters:", "constraint",
                     "uq_voucher_period_no", "entry", FAKE_INVOICE_NO, str(FAKE_AMOUNT),
                     FAKE_SUMMARY, "psycopg"):
        assert fragment not in body, f"响应里漏出了 {fragment}：{body}"
    assert "数据库内部错误" in body, body
    assert "请求号" in body, body
    # 请求号要与响应头一致：用户截图里的那串字符要能在服务端日志里定位到。
    assert response.headers.get("X-Request-ID", "") in body


def test_上传接口的数据库异常同样脱敏(client, auth, configured, store, monkeypatch):
    """上传接口以前把完整 INSERT 与参数回给客户端（文件名超长就能单请求命中）。"""

    def boom(*args, **kwargs):
        raise _fake_integrity_error()

    # 上传接口走的是「已落盘的源文件」那个入口（2026-10-24 性能审计：
    # 字节不再在内存里攒，改成边读边写）。脱敏的断言与入口无关 ——
    # 它验的是「异常不能把 SQL 原文回给客户端」，所以打在真正的那个入口上。
    monkeypatch.setattr(service, "create_batch_from_staged", boom)

    response = _post_files(client, configured, auth, [("10月8日.pdf", _pdf_bytes("inv_a.pdf"))])

    assert response.status_code == 500, response.text
    body = response.text
    for fragment in ("[SQL:", "INSERT INTO", "[parameters:", "constraint", "psycopg"):
        assert fragment not in body, body
    assert "数据库内部错误" in body, body


def test_规则接口的数据库异常同样脱敏(client, auth, configured, store, monkeypatch):
    """规则接口共用同一套 except —— 一并收口。"""

    def boom(*args, **kwargs):
        raise _fake_integrity_error()

    monkeypatch.setattr(service, "create_rule", boom)

    response = client.post(f"/api/books/{configured.id}/invoices/rules", headers=auth, json={
        "match_type": "ITEM", "invoice_type": "BOTH", "keyword": "餐饮",
        "subject_id": 1, "priority": 0, "active": True,
    })

    assert response.status_code == 500, response.text
    assert "[SQL:" not in response.text
    assert "数据库内部错误" in response.text


# --------------------------------------------------------------------- P1-3


def test_并发上传同一份发票给的是人话不是SQL(client, auth, configured, store, monkeypatch):
    """判重被并发绕过时，唯一约束违约要翻成「发票已存在」。

    原来这条路径上那句专门写好的中文提示（service.py 的「发票已存在，不能
    重复导入：…（来自哪个文件）」）走不到 —— 走的是数据库异常那条路，于是
    用户看到的是一坨 SQL，而且**约束名**也一起回给了客户端。
    """
    assert _upload_one(client, configured, auth, "第一次.pdf") > 0

    # 模拟「预检被并发绕过」：**第一次**判重查询返回空，于是写入会撞唯一约束。
    # 之后（回滚之后那次复查）必须走真的判重 —— 否则连「是哪一张重复」都
    # 说不出来，用户只看到一句干巴巴的「重复上传」。
    original_check = service._existing_invoice_numbers
    calls = {"n": 0}

    def flaky_check(db, book_id, numbers):
        calls["n"] += 1
        if calls["n"] == 1:
            return set()
        return original_check(db, book_id, numbers)

    monkeypatch.setattr(service, "_existing_invoice_numbers", flaky_check)

    second = _post_files(client, configured, auth, [("第二次.pdf", _pdf_bytes("inv_a.pdf"))])

    assert second.status_code == 400, second.text
    detail = second.json()["detail"]
    assert "发票已存在" in detail, detail
    assert FAKE_INVOICE_NO in detail, "要点名是哪一张重复了"
    for fragment in ("[SQL:", "INSERT INTO", "[parameters:", "uq_invoice_record_book_no",
                     "psycopg", "constraint"):
        assert fragment not in second.text, second.text


def test_落败的并发上传不会留下批次与原件(client, auth, configured, store, monkeypatch):
    """被拒的上传不许留下批次行，也不许留下原件（和别的失败路径同一口径）。"""
    assert _upload_one(client, configured, auth, "第一次.pdf") > 0
    original_check = service._existing_invoice_numbers
    calls = {"n": 0}

    def flaky_check(db, book_id, numbers):
        calls["n"] += 1
        if calls["n"] == 1:
            return set()
        return original_check(db, book_id, numbers)

    monkeypatch.setattr(service, "_existing_invoice_numbers", flaky_check)

    second = _post_files(client, configured, auth, [("第二次.pdf", _pdf_bytes("inv_a.pdf"))])
    assert second.status_code == 400, second.text

    batches = client.get(f"/api/books/{configured.id}/invoices/batches", headers=auth).json()
    assert len(batches) == 1, f"被拒的上传留下了批次行：{batches}"
    stored = list(store.glob("*.pdf")) if store.exists() else []
    assert len(stored) == 1, f"被拒的上传留下了原件：{stored}"


# ------------------------------------------------------------------ 账上核对


def test_并发落败之后账上与批次摘要一致(db, client, auth, configured, store):
    """批次摘要、发票记录、凭证三处必须对得上（原来少报一张，界面看不出异常）。"""
    batch_id = _upload_one(client, configured, auth)
    url = f"/api/books/{configured.id}/invoices/batches/{batch_id}/commit"
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        return client.post(url, headers=auth)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(fire), pool.submit(fire)]]

    assert sorted(r.status_code for r in responses) == [200, 409]

    db.expire_all()
    batch = db.get(InvoiceBatch, batch_id)
    records = db.scalars(
        select(InvoiceRecord).where(InvoiceRecord.batch_id == batch_id)).all()
    vouchers = db.scalars(select(Voucher).where(Voucher.book_id == configured.id)).all()

    assert batch.committed_count == 1
    assert len([v for v in vouchers if v.remark == f"发票 {FAKE_INVOICE_NO}"]) == 1
    committed = [r for r in records if r.status == "COMMITTED"]
    assert len(committed) == 1
    assert committed[0].voucher_id is not None
