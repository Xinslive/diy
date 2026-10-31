"""发票跟着凭证一起打印（用户 2026-10-31 批注）。

**要解决的事**：发票导入生成的凭证，原件就是它的附件（生成时
``attachments=1``），可纸上只有一张记账凭证 —— 装订时还要一张一张翻原始
PDF 找。打印时把原件接在凭证后面，一次印完，剪下来就能订在一起。

盯的是四件事：

1. **顺序**：发票页接在**它那张纸**后面，不是全部堆到最后 ——
   剪下来时发票跟它的凭证挨着；
2. **只在开关打开时才有**：关掉时打印结果与从前一样，一张发票页都不多；
3. **拆成多格的凭证只印一次发票**：那张凭证在纸上占了几格，发票只有一张；
4. **原件读取失败要报错**：避免用户以为打印成功，实际漏掉发票。

``tests/fixtures/invoices`` 是纯文字合成夹具，只验证打印逻辑，不作为真实原票的版式验收。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil

import pytest

from app.core.config import settings
from app.models import InvoiceBatch, InvoiceRecord

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "invoices"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """发票原件落到临时目录 —— 绝不写进仓库的 data/invoices。"""
    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path / "invoices"))
    return tmp_path / "invoices"


def _template(client, auth, book, slots: int = 2) -> int:
    """建一份凭证模板，``slots`` = 一页放几张。"""
    r = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": "两张一张",
              "content": {"voucher": {"slots_per_page": slots}}},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _invoice_for(db, book, voucher, store, name: str = "inv_a.pdf", number: str = "1"):
    """关联源 PDF，每个批次单独保存，防止不同凭证的原件相互覆盖。"""
    store.mkdir(parents=True, exist_ok=True)
    source = FIXTURES / name
    relative = f"batch_{number}.pdf"
    shutil.copy(source, store / relative)
    batch = InvoiceBatch(book_id=book.id, filename=source.name, file_path=relative,
                          status="COMMITTED", total=1, committed_count=1,
                          created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(batch_id=batch.id, book_id=book.id, page_index=0,
                            invoice_no=f"244170000000333333{number}", invoice_type="INPUT",
                            status="COMMITTED", voucher_id=voucher.id, payload={})
    db.add(record)
    db.commit()
    return record


def _print(client, auth, book, period_id, **params):
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": period_id, **params}, headers=auth)
    assert r.status_code == 200, r.text
    return r.text


def _voucher(make_voucher, day: str = "2026-01-05", rows: int = 1):
    dr = [("1001", 100 + i, f"第{i}笔") for i in range(rows)]
    cr = [("5001", 100 + i, f"第{i}笔") for i in range(rows)]
    return make_voucher(day, dr=dr, cr=cr, posted=True)


def test_发票页接在它那张纸后面(client, auth, book, periods, make_voucher, db, store):
    """凭证那张纸印完，紧接着印它自己的发票。"""
    voucher = _voucher(make_voucher)
    _invoice_for(db, book, voucher, store)
    html = _print(client, auth, book, periods[1].id, print_invoices="true")
    pages = html.split('<div class="page">')[1:]
    assert len(pages) == 2, "凭证那张纸后面应该只有一张发票页"
    assert '<div class="invoice-page">' not in pages[0], "凭证那页里混进了发票"
    assert '<div class="invoice-page">' in pages[1]
    assert "data:image/png;base64," in pages[1], "发票页里没有发票图片"


def test_没开开关时不添发票页(client, auth, book, periods, make_voucher, db, store):
    """开关关着，打印结果与从前一样。"""
    voucher = _voucher(make_voucher)
    _invoice_for(db, book, voucher, store)
    html = _print(client, auth, book, periods[1].id)
    assert "data:image/png;base64," not in html
    assert '<div class="invoice-page">' not in html


def test_开了发票打印才放开图片(client, auth, book, periods, make_voucher, db, store):
    """CSP 的 ``img-src``：平时是 ``'none'``，印发票时才放开 ``data:``。

    只放开 ``data:`` —— 那张图是我们自己渲染的发票原件，不是用户填的地址。
    """
    voucher = _voucher(make_voucher)
    _invoice_for(db, book, voucher, store)
    assert "img-src 'none'" in _print(client, auth, book, periods[1].id)
    assert "img-src data:" in _print(client, auth, book, periods[1].id,
                                     print_invoices="true")


def test_没有发票的凭证不添页(client, auth, book, periods, make_voucher):
    """手工记的凭证没有发票，一张页都不该多。"""
    _voucher(make_voucher)
    html = _print(client, auth, book, periods[1].id, print_invoices="true")
    assert "data:image/png;base64," not in html


def test_拆成多格的凭证只印一次发票(client, auth, book, periods, make_voucher,
                                    db, store):
    """六行分录的凭证在纸上占两格 —— 发票只有一张，不能印两遍。"""
    voucher = _voucher(make_voucher, rows=6)
    _invoice_for(db, book, voucher, store)
    html = _print(client, auth, book, periods[1].id, print_invoices="true")
    assert html.count('<div class="invoice-page">') == 1


@pytest.mark.parametrize("failure", ["missing", "corrupt", "page_index", "file_path"])
def test_原件读取失败不能静默漏印(client, auth, book, periods, make_voucher, db, store, failure):
    voucher = _voucher(make_voucher)
    record = _invoice_for(db, book, voucher, store)
    if failure == "missing":
        (store / "batch_1.pdf").unlink()
    elif failure == "corrupt":
        (store / "batch_1.pdf").write_bytes(b"not a PDF")
    elif failure == "page_index":
        record.page_index = 99
        db.commit()
    else:
        db.get(InvoiceBatch, record.batch_id).file_path = ""
        db.commit()
    response = client.get(f"/api/books/{book.id}/print/vouchers.html",
                          params={"period_id": periods[1].id, "print_invoices": "true"},
                          headers=auth)
    assert response.status_code == 409
    assert "发票原件无法打印" in response.json()["detail"]
    assert "关闭“发票打印”" in response.json()["detail"]
    assert "记 账 凭 证" in _print(client, auth, book, periods[1].id)


def test_发票太多时明说而不是硬打(client, auth, book, periods, make_voucher, db,
                                store, monkeypatch):
    """一次几百张发票就是几十 MB 的字符串 —— 超了要说清楚，不静默截断。"""
    import app.invoice.print_pages as print_pages

    monkeypatch.setattr(print_pages, "MAX_INVOICE_PAGES", 1)
    first = _voucher(make_voucher, day="2026-01-05")
    second = _voucher(make_voucher, day="2026-01-06")
    _invoice_for(db, book, first, store, "inv_a.pdf", "1")
    _invoice_for(db, book, second, store, "inv_b.pdf", "2")
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id, "print_invoices": "true"},
                   headers=auth)
    assert r.status_code == 413
    assert "发票" in r.json()["detail"]


def test_两张凭证一张纸时两张发票接在那张纸后面(client, auth, book, periods,
                                              make_voucher, db, store):
    """一页两张时，两张凭证在同一张纸上，后面接它们各自的两张发票页。"""
    first = _voucher(make_voucher, day="2026-01-05")
    second = _voucher(make_voucher, day="2026-01-06")
    _invoice_for(db, book, first, store, "inv_a.pdf", "1")
    _invoice_for(db, book, second, store, "inv_b.pdf", "2")
    tpl_id = _template(client, auth, book)
    html = _print(client, auth, book, periods[1].id,
                  template_id=tpl_id, print_invoices="true")
    pages = html.split('<div class="page">')[1:]
    assert len(pages) == 3, "一张纸 + 两张发票页"
    assert '<div class="invoice-page">' not in pages[0]
    assert '<div class="invoice-page">' in pages[1]
    assert '<div class="invoice-page">' in pages[2]
    from app.invoice.print_pages import page_data_uri

    actual = re.findall(r'<img src="(data:image/png;base64,[^"]+)"', html)
    expected = [page_data_uri(FIXTURES / name, 0)[0] for name in ("inv_a.pdf", "inv_b.pdf")]
    assert expected[0] != expected[1]
    assert actual == expected


@pytest.mark.skipif(not os.environ.get("CWQ_INVOICE_PRINT_SAMPLE"), reason="需提供真实原票路径")
def test_真实原票使用对应原始PDF页(client, auth, book, periods, make_voucher, db, store, tmp_path):
    from app.invoice.print_pages import page_data_uri
    import pypdfium2 as pdfium

    source = pathlib.Path(os.environ["CWQ_INVOICE_PRINT_SAMPLE"])
    with pdfium.PdfDocument(source) as document:
        count = len(document)
    voucher = _voucher(make_voucher)
    record = _invoice_for(db, book, voucher, store, str(source))
    record.payload = {"page_span": count}
    db.commit()
    html = _print(client, auth, book, periods[1].id,
                  template_id=_template(client, auth, book), print_invoices="true")
    actual = re.findall(r'<img src="(data:image/png;base64,[^"]+)"', html)
    assert actual == [page_data_uri(source, index)[0] for index in range(count)]
    preview = tmp_path / "real-invoice-print.html"
    preview.write_text(html, encoding="utf-8")
    print(f"真实原票打印预览：{preview}")
