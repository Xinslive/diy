"""已入账（已生成）的发票在界面上是**只读**的，它记到哪个科目要看得见。

这一组盯三件事：

1. **「忽略这张」对已入账的行没有意义** —— 服务端对已入账的记录直接跳过
   重算（``_build_preview`` 里那条「已入账的不重算」），点了什么也不会发生。
   所以界面上不摆那排控件（见 ``frontend/tests/invoice_committed_row.test.mjs``）。
   这一组盯的是**服务端那半边**：已入账的记录，状态必须还是已入账，
   重新预览不能把它变回「可生成」—— 变回去了下一次确认就又生成一张凭证。

2. **科目编码和名称要跟着记录一起出来**。以前批次详情接口没带科目表，
   界面上「凭证预览」那串科目名一直是空的，看着像「这笔钱没记到科目上」。

3. **逐项的科目也要带出来**。界面上「入账科目」那一列对已入账的行是
   只读文字，显示的就是这里带出去的编码和名称。
"""
from __future__ import annotations

from sqlalchemy import select

from app.invoice import service
from app.models import Entry, InvoiceBatch, InvoiceRecord, Voucher


def _config(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })


def _batch_with(db, book, count: int = 2):
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


def test_凭证预览里带着科目编码和名称(db, book, subjects):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    preview = service.preview_batch(db, batch)

    entries = preview["invoices"][0]["resolution"]["entries"]
    assert entries, "预览里应该有分录"
    for entry in entries:
        assert entry["subject_code"], entry
        assert entry["subject_name"], entry
    # 借方是那一条项目（落兜底科目），贷方是结算科目。
    codes = {entry["subject_code"] for entry in entries}
    assert codes == {subjects["560201"].code, subjects["1002"].code}, codes


def test_逐项也带着科目编码和名称(db, book, subjects):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    preview = service.preview_batch(db, batch)

    lines = preview["invoices"][0]["resolution"]["line_resolution"]
    assert lines, "预览里应该有逐项的科目"
    assert lines[0]["subject_code"] == subjects["560201"].code, lines
    assert lines[0]["subject_name"] == subjects["560201"].name, lines


def test_已入账的行重新预览还是已入账(db, book, subjects, admin_user):
    """安全底座：已入账的不能变回「可生成」。

    变回去了，下一次确认就会给同一张发票**再生成一张凭证** —— 账上凭空
    多一笔，还查不出是哪张干的。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    preview = service.preview_batch(db, batch)
    assert preview["ready_count"] == 0, "已入账的不该还能再生成"
    assert all(row["status"] == "COMMITTED" for row in preview["invoices"])
    # 凭证还是那两张。
    assert len(db.scalars(select(Voucher).where(Voucher.book_id == book.id)).all()) == 2


def test_已入账的行仍然带着科目名称(db, book, subjects, admin_user):
    """已入账的行在界面上是只读的 —— 那就要**看得见**它记到了哪个科目。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    preview = service.preview_batch(db, batch)
    row = preview["invoices"][0]
    assert row["status"] == "COMMITTED"
    entries = row["resolution"]["entries"]
    assert entries and all(e["subject_code"] and e["subject_name"] for e in entries)
    lines = row["resolution"]["line_resolution"]
    assert lines and lines[0]["subject_code"] == subjects["560201"].code


def test_批次详情接口也带着科目名称(client, db, book, subjects, auth):
    """界面加载批次详情走的是 ``GET /batches/{id}`` —— 那一路也得带科目表。

    以前只有 ``_build_preview`` 内部那条路带着，接口这头没带，
    于是界面上那串科目名一直是空的。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)

    r = client.get(f"/api/books/{book.id}/invoices/batches/{batch.id}", headers=auth)
    assert r.status_code == 200, r.text
    rows = r.json()["invoices"]
    assert rows, r.text
    for entry in rows[0]["resolution"]["entries"]:
        assert entry["subject_code"], entry
        assert entry["subject_name"], entry
    for item in rows[0]["resolution"]["line_resolution"]:
        assert item["subject_code"], item
