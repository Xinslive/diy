"""已确认过的批次可以**补生成**剩下的发票。

现实里很常见：20 张发票，先确认了 10 张，剩下 10 张当时点的是「忽略」。
第二天发现那 10 张其实要入账 —— 得能把它们取消忽略、重新预览、再生成
10 张凭证出来。

以前这里是一堵墙：批次状态已经是 COMMITTED，重新预览和确认都被拒
（「该批次已是 COMMITTED 状态，不能确认」）。用户唯一的出路是把整批重新
传一遍 —— 而重传会被查重拦下（号码已经入过账）。死胡同。

这一组用例盯三件事：

1. **能补**：取消忽略 → 重新预览 → 再确认一次，多出来的凭证生成了。
2. **不重复**：重新预览**不能**把已入账的记录变回「可生成」，否则第二次
   确认会把同一张发票再入一次账 —— 账上凭空多一笔，还查不出是哪张干的。
3. **计数不重**：批次的「已生成凭证」是**累加**的，不是被后一轮覆盖。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.invoice import service
from app.models import Entry, InvoiceBatch, InvoiceRecord, Voucher


def _config(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })


def _batch_with(db, book, count: int):
    """一个批次，count 张发票，每张 10.00 元。"""
    batch = InvoiceBatch(book_id=book.id, filename="六月.pdf", file_path="",
                          status="PENDING", total=count, created_by="tester")
    db.add(batch)
    db.flush()
    for index in range(count):
        number = f"2441700000003333333{index}"
        record = InvoiceRecord(
            batch_id=batch.id, book_id=book.id, page_index=index,
            invoice_no=number, invoice_type="INPUT", status="PENDING",
            payload={
                "meta": {"invoice_no": number, "issue_date": "2026-10-01",
                         "seller_name": "某供应商", "seller_tax_id": "91310000MA2AYXC79Y",
                         "buyer_name": "测试公司", "buyer_tax_id": "91420100MA1234567X",
                         "total_excl_tax": "10.00", "total_tax": "0.00",
                         "total_incl_tax": "10.00"},
                "lines": [{"name": "油", "display_name": "油",
                           "amount": "10.00", "tax": "0.00"}],
            },
        )
        db.add(record)
        db.flush()
    return batch


def _vouchers(db, book):
    return db.scalars(select(Voucher).where(Voucher.book_id == book.id)).all()


def test_取消忽略后可以补生成(db, book, subjects, admin_user):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    preview = service.preview_batch(db, batch)
    assert preview["error_count"] == 0
    records = sorted(batch.invoices, key=lambda r: r.page_index)
    # 第二张当时被点成「忽略」。
    preview = service.preview_batch(db, batch, {str(records[1].id): {"ignored": True}})
    assert preview["ignored_count"] == 1
    assert preview["ready_count"] == 1

    first = service.commit_batch(db, batch, admin_user)
    assert first["created"] == 1, first
    assert batch.status == "COMMITTED"
    assert batch.committed_count == 1
    assert len(_vouchers(db, book)) == 1

    # 第二天：取消忽略，重新预览，再确认一次。
    preview = service.preview_batch(db, batch, {str(records[1].id): {"ignored": False}})
    assert preview["ready_count"] == 1, "取消忽略的那张该重新变成可生成"
    assert preview["committed_count"] == 1, "已入账的张数要带出来"
    statuses = {row["id"]: row["status"] for row in preview["invoices"]}
    assert statuses[records[0].id] == "COMMITTED", "已入账的那张不能变回可生成"
    assert statuses[records[1].id] == "READY"

    second = service.commit_batch(db, batch, admin_user)
    assert second["created"] == 1, second
    assert batch.committed_count == 2, "已生成张数要累加，不能被后一轮覆盖"
    assert len(_vouchers(db, book)) == 2, "两张发票两张凭证，一张都不能多"


def test_重新预览不会把已入账的记录变回可生成(db, book, subjects, admin_user):
    """这是上面那条的安全底座：变回「可生成」就会被再入一次账。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)
    assert batch.committed_count == 2

    preview = service.preview_batch(db, batch)
    assert preview["ready_count"] == 0, "已入账的两张都不该再可生成"
    assert preview["committed_count"] == 2
    assert all(row["status"] == "COMMITTED" for row in preview["invoices"])


def test_没有可生成的发票时再确认一次会被挡回去(db, book, subjects, admin_user):
    """重复提交（双击、并发、重试）要挡回去，不是回一句「生成了 0 张」。

    挡回去还有个好处：界面不会弹「凭证已生成」—— 那句话会让用户以为
    真的生成了，而其实什么也没发生。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)
    assert len(_vouchers(db, book)) == 1

    with pytest.raises(service.BatchConflict) as excinfo:
        service.commit_batch(db, batch, admin_user)
    assert "COMMITTED" in str(excinfo.value), excinfo.value
    assert len(_vouchers(db, book)) == 1, "同一张发票只能入一次账"
    assert batch.committed_count == 1


def test_补生成的凭证金额与发票一致(db, book, subjects, admin_user):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    records = sorted(batch.invoices, key=lambda r: r.page_index)
    service.preview_batch(db, batch, {str(records[1].id): {"ignored": True}})
    service.commit_batch(db, batch, admin_user)
    service.preview_batch(db, batch, {str(records[1].id): {"ignored": False}})
    service.commit_batch(db, batch, admin_user)

    for voucher in _vouchers(db, book):
        rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
        debit = sum((Decimal(row.amount) for row in rows if row.direction == "DEBIT"), Decimal("0"))
        credit = sum((Decimal(row.amount) for row in rows if row.direction == "CREDIT"), Decimal("0"))
        assert debit == credit == Decimal("10.00")
        assert all(Decimal(row.amount) != 0 for row in rows)


# --------------------------------------------------------------- HTTP 层


def test_走接口也能补生成(client, db, book, subjects, auth):
    """接口层也放行 —— 用户点的是按钮，不是 service 函数。

    这一条走的是真实链路：上传 → 预览（忽略一张）→ 确认 → 预览（取消忽略）
    → 再确认。
    """
    import io
    from pathlib import Path

    import pypdfium2 as pdfium

    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })
    fixtures = Path(__file__).parent / "fixtures" / "invoices"
    merged = pdfium.PdfDocument.new()
    try:
        for name in ("inv_a.pdf", "inv_b.pdf"):
            merged.import_pages(pdfium.PdfDocument(str(fixtures / name)))
        buf = io.BytesIO()
        merged.save(buf)
    finally:
        merged.close()

    r = client.post(f"/api/books/{book.id}/invoices/batches", headers=auth,
                    files=[("files", ("两份.pdf", buf.getvalue(), "application/pdf"))])
    assert r.status_code == 201, r.text
    preview = r.json()
    batch_id = preview["id"]
    assert preview["total"] == 2, preview
    invoices = sorted(preview["invoices"], key=lambda row: row["page_index"])

    ignored_id = invoices[1]["id"]
    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/preview", headers=auth,
                    json={"overrides": {str(ignored_id): {"ignored": True}}})
    assert r.status_code == 200, r.text
    assert r.json()["ignored_count"] == 1

    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/commit", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.text

    # 取消忽略 → 重新预览：接口层不能再拿「批次已 COMMITTED」拒掉。
    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/preview", headers=auth,
                    json={"overrides": {str(ignored_id): {"ignored": False}}})
    assert r.status_code == 200, f"重新预览被拒了：{r.text}"
    assert r.json()["ready_count"] == 1, r.text

    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/commit", headers=auth)
    assert r.status_code == 200, f"补生成被拒了：{r.text}"
    assert r.json()["created"] == 1, r.text

    r = client.get(f"/api/books/{book.id}/invoices/batches/{batch_id}", headers=auth)
    assert r.status_code == 200, r.text
    final = r.json()
    assert final["status"] == "COMMITTED"
    assert final["committed_count"] == 2, final
    assert all(row["voucher_id"] for row in final["invoices"]), final
