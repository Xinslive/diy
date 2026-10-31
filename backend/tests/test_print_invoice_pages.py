"""发票跟着凭证一起打印（用户 2026-10-31 批注）。

**要解决的事**：发票导入生成的凭证，原件就是它的附件（生成时
``attachments=1``），可纸上只有一张记账凭证 —— 装订时还要一张一张翻原始
PDF 找。打印时把原件接在凭证后面，一次印完，剪下来就能订在一起。

盯的是四件事：

1. **顺序**：标准票接在凭证末片后，一起拼成两格；长票整张前置；
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
from collections import OrderedDict

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


def _test_pdf(path, sizes, label="invoice"):
    """尺寸、文字可控的合成夹具，仅用于逻辑测试。"""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for i, (width, height) in enumerate(sizes):
        w, h = width * 72 / 25.4, height * 72 / 25.4
        n = len(objects) + 1
        kids.append(f"{n} 0 R")
        text = f"BT /F1 12 Tf 20 {h - 30} Td ({label}-{i}) Tj ET".encode()
        objects.extend([
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {n + 1} 0 R >>").encode(),
            f"<< /Length {len(text)} >>\nstream\n".encode() + text + b"\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    path.write_bytes(output)


def _invoice_for(db, book, voucher, store, name: str = "inv_a.pdf", number: str = "1",
                 sizes=((210, 140),)):
    """关联源 PDF，每个批次单独保存，防止不同凭证的原件相互覆盖。"""
    store.mkdir(parents=True, exist_ok=True)
    source = FIXTURES / name
    relative = f"batch_{number}.pdf"
    if pathlib.Path(name).is_absolute():
        shutil.copy(source, store / relative)
    else:
        _test_pdf(store / relative, sizes, f"invoice-{number}")
    batch = InvoiceBatch(book_id=book.id, filename=source.name, file_path=relative,
                          status="COMMITTED", total=1, committed_count=1,
                          created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(batch_id=batch.id, book_id=book.id, page_index=0,
                            invoice_no=f"244170000000333333{number}", invoice_type="INPUT",
                            status="COMMITTED", voucher_id=voucher.id, payload={"page_span": len(sizes)})
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


def test_凭证和标准票连续填满一张纸(client, auth, book, periods, make_voucher, db, store):
    voucher = _voucher(make_voucher)
    _invoice_for(db, book, voucher, store)
    html = _print(client, auth, book, periods[1].id, print_invoices="true",
                  template_id=_template(client, auth, book))
    pages = html.split('<div class="page">')[1:]
    assert len(pages) == 1
    assert pages[0].count('class="voucher-slot') == 2
    assert '<div class="voucher-slot invoice-slot">' in pages[0]
    assert pages[0].index('记0001') < pages[0].index('<img src=')


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
                                     template_id=_template(client, auth, book), print_invoices="true")


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
    html = _print(client, auth, book, periods[1].id, print_invoices="true",
                  template_id=_template(client, auth, book))
    assert html.count('<img src=') == 1
    assert html.index('003/003') < html.index('<img src=')


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
                          params={"period_id": periods[1].id, "print_invoices": "true",
                                  "template_id": _template(client, auth, book)},
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
                   params={"period_id": periods[1].id, "print_invoices": "true",
                           "template_id": _template(client, auth, book)},
                   headers=auth)
    assert r.status_code == 413
    assert "发票" in r.json()["detail"]


def test_凭证与发票统一配对而不预留空格(client, auth, book, periods,
                                              make_voucher, db, store):
    """连续配对：凭证1+发票1 / 凭证2+发票2。"""
    first = _voucher(make_voucher, day="2026-01-05")
    second = _voucher(make_voucher, day="2026-01-06")
    _invoice_for(db, book, first, store, "inv_a.pdf", "1")
    _invoice_for(db, book, second, store, "inv_b.pdf", "2")
    tpl_id = _template(client, auth, book)
    html = _print(client, auth, book, periods[1].id,
                  template_id=tpl_id, print_invoices="true")
    pages = html.split('<div class="page">')[1:]
    assert len(pages) == 2
    assert all(page.count('class="voucher-slot') == 2 for page in pages)
    from app.invoice.print_pages import page_data_uri

    actual = re.findall(r'<img src="(data:image/png;base64,[^"]+)"', html)
    expected = [page_data_uri(store / f"batch_{n}.pdf", 0)[0] for n in (1, 2)]
    assert expected[0] != expected[1]
    assert actual == expected


@pytest.mark.parametrize("order,reverse,expected", [
    ("normal", False, [["V1", "I1"], ["V2", "V3"], ["I3"]]),
    ("optimized", False, [["V1", "V3"], ["I1", "I3"], ["V2"]]),
    ("optimized", True, [["V2"], ["I1", "I3"], ["V1", "V3"]]),
])
def test_缺少附件的凭证也连续排版且顺序开关作用于整条队列(
        client, auth, book, periods, make_voucher, db, store, order, reverse, expected):
    from app.invoice.print_pages import page_data_uri

    vouchers = [_voucher(make_voucher, f"2026-01-0{i}") for i in (1, 2, 3)]
    for n in (1, 3):
        _invoice_for(db, book, vouchers[n - 1], store, number=str(n))
    images = {page_data_uri(store / f"batch_{n}.pdf", 0)[0]: f"I{n}" for n in (1, 3)}
    html = _print(client, auth, book, periods[1].id, template_id=_template(client, auth, book),
                  print_invoices="true", print_order=order, reverse=reverse)
    actual = []
    for page in html.split('<div class="page">')[1:]:
        cells = re.split(r'<div class="voucher-slot(?: invoice-slot)?">', page)[1:]
        tokens = []
        for cell in cells:
            image = re.search(r'<img src="([^"]+)"', cell)
            tokens.append(images[image[1]] if image else "V" + str(int(re.search(r'记(\d{4})', cell)[1])))
        actual.append(tokens)
    assert actual == expected
    assert sum(map(len, actual)) == 5


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_长票及其续页集中前置并标明凭证(client, auth, book, periods, make_voucher, db, store, orientation):
    first = _voucher(make_voucher, "2026-01-05")
    second = _voucher(make_voucher, "2026-01-06")
    # 一张多页原票含标准续页时也整体前置。
    _invoice_for(db, book, first, store, sizes=((210, 297), (210, 140)))
    _invoice_for(db, book, second, store, number="2")
    html = _print(client, auth, book, periods[1].id, template_id=_template(client, auth, book),
                  print_invoices="true", orientation=orientation, print_order="optimized", reverse=True)
    pages = html.split('<div class="page">')[1:]
    assert len(pages) == 4  # 两张长票页 + 三个半页格拼成两张纸
    assert all('<div class="invoice-page">' in page for page in pages[:2])
    assert all('2026-01-05 记0001' in page and '发票附件' in page for page in pages[:2])
    assert all('<div class="invoice-page">' not in page for page in pages[2:])
    assert html.count('<img src=') == 3
    assert sum(page.count('class="voucher-slot') for page in pages[2:]) == 3
    flow = 'flow-columns' if orientation == 'landscape' else 'flow-rows'
    assert all(f'slots-2 {flow}' in page for page in pages[2:])


def test_每页一张模板不启用发票打印(client, auth, book, periods, make_voucher, db, store):
    voucher = _voucher(make_voucher)
    _invoice_for(db, book, voucher, store)
    html = _print(client, auth, book, periods[1].id, template_id=_template(client, auth, book, 1),
                  print_invoices="true")
    assert '<img src=' not in html


def test_仅标准尺寸票加入半页队列(tmp_path, monkeypatch):
    from app.invoice import print_pages as p

    sizes = ((210, 140), (211, 141), (210, 143), (210, 160), (210, 297), (297, 210), (180, 140))
    path = tmp_path / "dimensions.pdf"
    _test_pdf(path, sizes)
    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path))
    refs = {n + 1: [p.InvoicePage(n + 1, 1, path.name, n, 1)] for n in range(len(sizes))}
    pages = p.invoice_pages_html(refs)
    assert [pages[n + 1][0].full_page for n in range(len(sizes))] == [False, False, True, True, True, True, True]


def test_同批次只打开一次且复用缓存并随原件更新失效(tmp_path, monkeypatch):
    from app.invoice import print_pages as p

    path = tmp_path / "batch.pdf"
    _test_pdf(path, ((210, 140), (210, 160)))
    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path))
    monkeypatch.setattr(p, "_cache", OrderedDict())
    monkeypatch.setattr(p, "_cache_bytes", 0)
    opens, renders = [], []
    real_open, real_render = p.pdfium.PdfDocument, p._render_page_png

    def opened(*args, **kwargs):
        opens.append(args[0])
        return real_open(*args, **kwargs)

    def rendered(*args):
        renders.append(args[1])
        return real_render(*args)

    monkeypatch.setattr(p.pdfium, "PdfDocument", opened)
    monkeypatch.setattr(p, "_render_page_png", rendered)
    refs = {n + 1: [p.InvoicePage(n + 1, 1, path.name, n, 1)] for n in range(2)}
    first = p.invoice_pages_html(refs)
    assert len(opens) == 1
    assert renders == [0, 1]
    assert not first[1][0].full_page and first[2][0].full_page
    assert p.invoice_pages_html(refs) == first
    assert len(opens) == 2 and renders == [0, 1]
    _test_pdf(path, ((210, 140), (210, 160)), "changed-original")
    assert p.invoice_pages_html(refs) != first
    assert renders == [0, 1, 0, 1]
    assert p._cache_bytes <= p._CACHE_LIMIT
    monkeypatch.setattr(p, "_CACHE_LIMIT", 1)
    monkeypatch.setattr(p, "_cache", OrderedDict())
    monkeypatch.setattr(p, "_cache_bytes", 0)
    p.invoice_pages_html(refs)
    assert p._cache_bytes == 0 and not p._cache


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
