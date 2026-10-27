"""发票「忽略」这件事：点了就生效，而且**不会**被下一次重算抹掉。

两个具体毛病：

1. 点了「忽略这张」，状态那一列还是「可生成」/「有问题」—— 得再点一次
   「重新计算预览」才看得出改没改成。忽略是**用户已经做好的决定**，
   不该逼着人再点一次才能看到结果。

2. 从历史里重开一个批次，点「重新计算预览」，早就点名忽略的发票被重新
   算成「可生成」—— 用户上一次的决定，被一次重算悄悄抹掉。

这一组用例盯住三件事：

- **立刻落库**：点一下忽略，记录的状态当场就是「已忽略」，落库了。
- **重算不抹**：重新计算预览之后，之前标记的「已忽略」还在。
- **只动这一张**：忽略一张封面页，不该把别的记录上还没确认的改动一起存了。
"""
from __future__ import annotations

import pytest

from app.invoice import service
from app.models import InvoiceBatch, InvoiceRecord


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


def _statuses(payload) -> dict:
    return {row["id"]: row["status"] for row in payload["invoices"]}


# --------------------------------------------------------------- 立刻落库


def test_忽略一张立刻生效并且落库(db, book, subjects, admin_user):
    """点一下就是「已忽略」，不用再点「重新计算预览」。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    records = sorted(batch.invoices, key=lambda r: r.page_index)

    payload = service.set_record_ignored(db, batch, records[1], True)

    statuses = _statuses(payload)
    assert statuses[records[1].id] == "IGNORED", "点了忽略就该是已忽略"
    assert statuses[records[0].id] == "READY", "别的记录不受影响"
    assert payload["ignored_count"] == 1
    assert payload["ready_count"] == 1

    # **落库了**：换个 session 读，还是「已忽略」。
    db.expire_all()
    again = db.get(InvoiceRecord, records[1].id)
    assert again.status == "IGNORED", "忽略必须落库，不能只在内存里"
    assert again.errors == [], "被忽略的记录不该再挂着异常"


def test_取消忽略后状态由校验决定(db, book, subjects, admin_user):
    """一张本来就「有问题」的票，取消忽略后要重新变回「有问题」。

    取消忽略不是「翻个标志」—— 它要回到正常的科目解析与金额校验。
    当年缺发票号、缺结算科目这些，得重新报出来，而不是无声地变成「可生成」。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    records = sorted(batch.invoices, key=lambda r: r.page_index)
    record = records[0]
    # 把发票号码抹掉 —— 这张票因此「缺少发票号码」。
    record.payload = {**record.payload, "meta": {**record.payload["meta"], "invoice_no": ""}}
    record.invoice_no = None
    db.flush()
    service.preview_batch(db, batch)
    assert record.status == "REJECTED"

    service.set_record_ignored(db, batch, record, True)
    assert record.status == "IGNORED"

    service.set_record_ignored(db, batch, record, False)
    assert record.status == "REJECTED", "取消忽略后要重新报出问题，不能无声变成可生成"


def test_已入账的发票不能忽略(db, book, subjects, admin_user):
    """已生成凭证的发票改了也不会重算 —— 那就别假装能忽略。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    record = sorted(batch.invoices, key=lambda r: r.page_index)[0]
    service.commit_batch(db, batch, admin_user)
    assert record.status == "COMMITTED"

    with pytest.raises(ValueError) as excinfo:
        service.set_record_ignored(db, batch, record, True)
    assert "已生成凭证" in str(excinfo.value), excinfo.value


# --------------------------------------------------------------- 重算不抹


def test_重新计算预览不会抹掉已忽略(db, book, subjects, admin_user):
    """这是第二个毛病的正身：重算之后，之前标记的「已忽略」还在。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    records = sorted(batch.invoices, key=lambda r: r.page_index)

    service.set_record_ignored(db, batch, records[1], True)
    assert records[1].status == "IGNORED"

    # **不带任何覆盖项**地重算 —— 这就是「重新进入批次后点重新计算预览」。
    payload = service.preview_batch(db, batch)

    statuses = _statuses(payload)
    assert statuses[records[1].id] == "IGNORED", "已忽略的状态被重算抹掉了"
    assert statuses[records[0].id] == "READY"
    assert payload["ignored_count"] == 1
    assert payload["ready_count"] == 1
    assert payload["ready_count"] + payload["error_count"] + payload["ignored_count"] == payload["total"]


def test_重算多少次都还认已忽略(db, book, subjects, admin_user):
    """重算是幂等的：算一次、算十次，结果一样。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    record = sorted(batch.invoices, key=lambda r: r.page_index)[0]

    service.set_record_ignored(db, batch, record, True)
    for _ in range(3):
        payload = service.preview_batch(db, batch)
        assert _statuses(payload)[record.id] == "IGNORED"


def test_覆盖项明确写了取消忽略就能取消(db, book, subjects, admin_user):
    """落库状态是兜底，不是铁律：明确说了「取消忽略」就照办。

    前端点「取消忽略」时发的是 ``ignored=False``，这条得能盖过库里记着
    的「已忽略」—— 否则那个按钮按了没反应。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    record = sorted(batch.invoices, key=lambda r: r.page_index)[0]

    service.set_record_ignored(db, batch, record, True)
    payload = service.preview_batch(db, batch, {str(record.id): {"ignored": False}})
    assert _statuses(payload)[record.id] != "IGNORED", "明确的取消忽略要生效"


# --------------------------------------------------------------- 只动这一张


def test_忽略一张不影响别的记录上还没落库的改动(db, book, subjects, admin_user):
    """点一下忽略封面页，不该把用户还没确认的科目改动一起存了。

    别的记录上的改动按原来的节奏走：点「重新计算预览」才落库。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    records = sorted(batch.invoices, key=lambda r: r.page_index)

    # 第二张有个还没落库的人工改动：把结算科目换成另一个。
    service.set_record_ignored(db, batch, records[0], True, {
        str(records[1].id): {"settlement_subject_id": subjects["1002"].id},
    })

    db.expire_all()
    assert db.get(InvoiceRecord, records[0].id).status == "IGNORED"
    # 第二张的覆盖项没被顺带落库 —— 库里的还是重算前那一次的结果。
    assert db.get(InvoiceRecord, records[1].id).status == "READY"


# --------------------------------------------------------------- HTTP 层


def _upload(client, book_id, auth):
    """走真实上传接口落一个批次。"""
    import io
    from pathlib import Path

    import pypdfium2 as pdfium

    fixtures = Path(__file__).parent / "fixtures" / "invoices"
    merged = pdfium.PdfDocument.new()
    try:
        for name in ("inv_a.pdf", "inv_b.pdf"):
            merged.import_pages(pdfium.PdfDocument(str(fixtures / name)))
        buf = io.BytesIO()
        merged.save(buf)
    finally:
        merged.close()
    r = client.post(f"/api/books/{book_id}/invoices/batches", headers=auth,
                    files=[("files", ("两份.pdf", buf.getvalue(), "application/pdf"))])
    assert r.status_code == 201, r.text
    return r.json()


def _http_config(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })


def test_走接口忽略立刻生效(db, book, subjects, client, auth):
    """接口层也要一次调用就落库 —— 用户点的是按钮。"""
    _http_config(db, book, subjects)
    preview = _upload(client, book.id, auth)
    batch_id = preview["id"]
    target = preview["invoices"][0]["id"]

    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/records/{target}/ignore",
                    headers=auth, json={"ignored": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ignored_count"] == 1, body
    assert next(row for row in body["invoices"] if row["id"] == target)["status"] == "IGNORED"

    # 刷新页面（重开批次）之后，状态还在。
    r = client.get(f"/api/books/{book.id}/invoices/batches/{batch_id}", headers=auth)
    assert r.status_code == 200, r.text
    assert next(row for row in r.json()["invoices"] if row["id"] == target)["status"] == "IGNORED"


def test_走接口重新计算预览不会抹掉已忽略(db, book, subjects, client, auth):
    """第二个毛病的接口版：重开批次 → 重新计算 → 已忽略的还在。"""
    _http_config(db, book, subjects)
    preview = _upload(client, book.id, auth)
    batch_id = preview["id"]
    target = preview["invoices"][0]["id"]

    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/records/{target}/ignore",
                    headers=auth, json={"ignored": True})
    assert r.status_code == 200, r.text

    # 重开批次 + 重新计算预览（不带任何覆盖项）。
    r = client.get(f"/api/books/{book.id}/invoices/batches/{batch_id}", headers=auth)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/preview",
                    headers=auth, json={"overrides": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ignored_count"] == 1, body
    assert next(row for row in body["invoices"] if row["id"] == target)["status"] == "IGNORED"


def test_走接口忽略不在这批次里的发票会被拒(db, book, subjects, client, auth):
    _http_config(db, book, subjects)
    preview = _upload(client, book.id, auth)
    batch_id = preview["id"]

    r = client.post(f"/api/books/{book.id}/invoices/batches/{batch_id}/records/999999/ignore",
                    headers=auth, json={"ignored": True})
    assert r.status_code == 404, r.text
