"""发票批量上传：合并 PDF、多文件、整个文件夹。

用户问的三件事在这里各自钉成用例：

1. **合并 PDF** —— 一份多页 PDF 里每页一张发票，按页拆开各自成一条记录；
   页里夹着封面/装订页时能点名忽略，不让它把整批卡住。
2. **文件夹** —— 整文件夹传进来，非 PDF 被忽略，而且忽略项**有交代**
   （批次备注里点名），不无声消失。
3. **批量上传** —— 多份 PDF 合成一个批次，页码连续，每张发票记得自己来自哪个文件。

夹具是两份**真实可解析**的中文电子发票 PDF（``tests/fixtures/invoices/*.pdf``），
所以这些用例跑的是真的文本层解析与真的合并，不是打桩后的假绿。
"""
from __future__ import annotations

import io
import pathlib

import pytest
import pypdfium2 as pdfium
from sqlalchemy import select

from app.core.config import settings
from app.invoice import service, storage
from app.models import InvoiceBatch, InvoiceRecord

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "invoices"
BUYER_TAX_ID = "91420100MA1234567X"


def _pdf_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _merged_bytes(paths: list[pathlib.Path]) -> bytes:
    """把几份 PDF 合成一份（模拟用户「打印/合并成一个 PDF」的结果）。"""
    out = pdfium.PdfDocument.new()
    try:
        for path in paths:
            out.import_pages(pdfium.PdfDocument(str(path)))
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
    finally:
        out.close()


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


# --------------------------------------------------------------- 1. 合并 PDF


def test_一份合并PDF按页拆成多张发票(db, store, configured, admin_user):
    """用户把多张发票合并成一个 PDF 再传 —— 一页一张，各自成一条记录。"""
    merged = _merged_bytes([FIXTURES / "inv_a.pdf", FIXTURES / "inv_b.pdf"])
    batch = service.create_batch(db, configured.id, admin_user, [("十月发票合并.pdf", merged)])

    assert batch.total == 2, "两页应当拆成两张发票"
    records = sorted(batch.invoices, key=lambda r: r.page_index)
    assert [r.invoice_no for r in records] == ["24417000000011111111", "24417000000022222222"]
    assert [r.page_index for r in records] == [0, 1], "页码必须连续且从 0 开始"
    assert all(r.payload.get("source_file") == "十月发票合并.pdf" for r in records)
    assert {r.invoice_type for r in records} == {"INPUT"}, "税号匹配购买方 → 进项"


def _blank_pdf_bytes() -> bytes:
    """一页纯白：合并件里夹的封面/装订页就是这样 —— 没有发票号码。"""
    doc = pdfium.PdfDocument.new()
    doc.new_page(595, 842)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _merge(parts: list[bytes]) -> bytes:
    """按顺序合并任意几段 PDF 字节（封面夹在中间也能造出来）。"""
    out = pdfium.PdfDocument.new()
    try:
        for part in parts:
            out.import_pages(pdfium.PdfDocument(io.BytesIO(part)))
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
    finally:
        out.close()


def test_合并PDF里的非发票页可以忽略(db, store, configured, admin_user):
    """封面/装订页本来就没有发票号码。点名忽略后整批照常能确认。"""
    merged = _merge([_blank_pdf_bytes(), _pdf_bytes("inv_a.pdf"), _pdf_bytes("inv_b.pdf")])
    batch = service.create_batch(db, configured.id, admin_user, [("带封面的合并件.pdf", merged)])
    preview = service.preview_batch(db, batch)
    assert preview["total"] == 3, "三页三条记录，封面页不该被悄悄丢掉"
    assert preview["error_count"] == 1, "没忽略之前，封面页是红字异常"

    cover = next(row for row in preview["invoices"] if not row["invoice_no"])
    assert cover["page_index"] == 0, "封面在第 0 页"
    recomputed = service.preview_batch(db, batch, {str(cover["id"]): {"ignored": True}})
    assert recomputed["error_count"] == 0, "封面被点名忽略后不该再拦下整批"
    assert recomputed["ignored_count"] == 1
    assert {row["status"] for row in recomputed["invoices"]} == {"READY", "IGNORED"}


def test_忽略的页不生成凭证(db, store, configured, admin_user):
    """点名忽略的那张既不该拦下整批，也不该生成凭证。"""
    batch = service.create_batch(db, configured.id, admin_user, [("两份.pdf", _merged_bytes(
        [FIXTURES / "inv_a.pdf", FIXTURES / "inv_b.pdf"]))])
    first = service.preview_batch(db, batch)
    assert first["error_count"] == 0
    kept = next(row for row in first["invoices"] if row["invoice_no"] == "24417000000011111111")
    dropped = next(row for row in first["invoices"] if row["invoice_no"] == "24417000000022222222")

    recomputed = service.preview_batch(db, batch, {str(dropped["id"]): {"ignored": True}})
    assert recomputed["error_count"] == 0, "被忽略的异常不该再拦下整批"
    assert recomputed["ignored_count"] == 1

    result = service.commit_batch(db, batch, admin_user)
    assert result["created"] == 1, "两张里只确认一张，就只生成一张凭证"
    assert "1 张已忽略" in batch.message
    rows = {r.invoice_no: r for r in db.scalars(
        select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id)).all()}
    assert rows[kept["invoice_no"]].voucher_id is not None
    assert rows[dropped["invoice_no"]].voucher_id is None, "忽略的发票不该产生凭证"
    assert rows[dropped["invoice_no"]].status == "IGNORED"


# ------------------------------------------------------------- 2. 整文件夹传


def test_文件夹里的非PDF被忽略且有交代(db, store, configured, admin_user):
    """整文件夹传进来是常态：jpg/xlsx 该忽略，但必须在备注里点名。"""
    batch = service.create_batch(db, configured.id, admin_user, [
        ("十月凭证.pdf", _pdf_bytes("inv_a.pdf")),
        ("扫描件.jpg", b"not-a-pdf"),
        ("费用台账.xlsx", b"not-a-pdf"),
    ])
    assert batch.total == 1, "只有那份 PDF 进了批次"
    assert batch.invoices[0].payload.get("source_file") == "十月凭证.pdf"
    assert "已忽略 2 个非 PDF 文件" in batch.message
    assert "扫描件.jpg" in batch.message and "费用台账.xlsx" in batch.message, "忽略项要能点名"


def test_忽略项写进批次历史(db, store, configured, admin_user):
    batch = service.create_batch(db, configured.id, admin_user, [
        ("a.pdf", _pdf_bytes("inv_a.pdf")), ("b.png", b"x"),
    ])
    summary = service.batch_summary(batch)
    assert "b.png" in summary["message"], "历史列表也要看得见跳过了什么"


def test_一个PDF都没有直接拒(db, store, configured, admin_user):
    def batch_count() -> int:
        return db.scalar(
            select(InvoiceBatch.id).where(InvoiceBatch.book_id == configured.id).limit(1)
        ) is not None

    before = batch_count()
    with pytest.raises(ValueError, match="没有可解析的 PDF"):
        service.create_batch(db, configured.id, admin_user, [("照片.jpg", b"x"), ("表格.xlsx", b"y")])
    db.rollback()
    assert batch_count() == before, "被拒的上传不许留下批次行"
    assert not store.exists() or not list(store.glob("*.pdf")), "被拒的上传不许留下原件"


# ------------------------------------------------------------- 3. 批量上传


def test_多份PDF合成一个批次且记得各自来源(db, store, configured, admin_user):
    batch = service.create_batch(db, configured.id, admin_user, [
        ("10月8日早餐.pdf", _pdf_bytes("inv_a.pdf")),
        ("10月9日保洁.pdf", _pdf_bytes("inv_b.pdf")),
    ])
    assert batch.total == 2, "一次上传两份 = 一个批次两张发票"
    assert batch.filename == "10月8日早餐.pdf 等 2 个文件", "标题要短、要认得出是哪批"
    sources = {row.invoice_no: row.payload.get("source_file") for row in batch.invoices}
    assert sources == {
        "24417000000011111111": "10月8日早餐.pdf",
        "24417000000022222222": "10月9日保洁.pdf",
    }, "每张发票都要能说出自己来自哪个文件"

    stored = storage.path_for(batch.file_path)
    assert len(pdfium.PdfDocument(str(stored))) == 2, "合并后的原件应是 2 页"
    assert not list(store.glob("*_src*.pdf")), "暂存文件合并完就要清掉"
    assert [p.name for p in store.glob("*.pdf")] == [f"batch_{batch.id}.pdf"]


def test_批量上传的批次一次确认生成两张凭证(db, store, configured, admin_user):
    batch = service.create_batch(db, configured.id, admin_user, [
        ("a.pdf", _pdf_bytes("inv_a.pdf")), ("b.pdf", _pdf_bytes("inv_b.pdf")),
    ])
    preview = service.preview_batch(db, batch)
    assert preview["error_count"] == 0, preview["invoices"][0]["errors"]
    assert preview["sources"] == ["a.pdf", "b.pdf"]

    service.commit_batch(db, batch, admin_user)
    vouchers = db.scalars(select(InvoiceRecord.voucher_id).where(InvoiceRecord.batch_id == batch.id)).all()
    assert all(v is not None for v in vouchers), "两张都要落到凭证上"
    assert batch.committed_count == 2


def test_单文件上传原样落盘不被重写(db, store, configured, admin_user):
    """只有一份时不合并：落盘的就是用户传上来的那份字节。"""
    data = _pdf_bytes("inv_a.pdf")
    batch = service.create_batch(db, configured.id, admin_user, [("单张.pdf", data)])
    assert storage.path_for(batch.file_path).read_bytes() == data
    assert batch.filename == "单张.pdf"


def test_同一批里号码重复要点名来自哪些文件(db, store, configured, admin_user):
    with pytest.raises(ValueError) as err:
        service.create_batch(db, configured.id, admin_user, [
            ("扫描件.pdf", _pdf_bytes("inv_a.pdf")),
            ("扫描件-副本.pdf", _pdf_bytes("inv_a.pdf")),
        ])
    message = str(err.value)
    assert "24417000000011111111" in message
    assert "扫描件.pdf" in message and "扫描件-副本.pdf" in message, "只给号码等于让用户在几十个文件里自己找"
    db.rollback()
    assert not store.exists() or not list(store.glob("*.pdf")), "失败批次不许留下原件"


def test_已导入的号码跨文件被拒(db, store, configured, admin_user):
    first = service.create_batch(db, configured.id, admin_user, [("第一次.pdf", _pdf_bytes("inv_a.pdf"))])
    service.preview_batch(db, first)
    service.commit_batch(db, first, admin_user)
    with pytest.raises(ValueError) as err:
        service.create_batch(db, configured.id, admin_user, [
            ("第二次.pdf", _pdf_bytes("inv_a.pdf")), ("新的.pdf", _pdf_bytes("inv_b.pdf")),
        ])
    message = str(err.value)
    assert "第二次.pdf" in message, "要点名是刚传上来的哪份文件重复了 —— 用户手里只有这批文件"
    assert "第一次.pdf" not in message, "已经导入过的那批不在他眼前，点名它没有可操作性"
    db.rollback()


def test_损坏的PDF整批失败并点名(db, store, configured, admin_user):
    """该解析却解析不了的文件不忽略 —— 静默跳过等于用户以为传进去了。"""
    with pytest.raises(ValueError, match="坏的.pdf"):
        service.create_batch(db, configured.id, admin_user, [
            ("好的.pdf", _pdf_bytes("inv_a.pdf")), ("坏的.pdf", b"%PDF-1.7 broken"),
        ])
    db.rollback()
    assert not store.exists() or not list(store.glob("*.pdf"))


def test_作废掉的批次不再挡着重新上传(db, store, configured, admin_user):
    """用户点「作废批次」的意思是「这批不要」—— 转头上传同一份必须能成。

    判重口径要跟 commit_batch 一致（那边只看 COMMITTED），两边不一样就会出现
    「作废前传得进去、作废后再也传不进去」的死局。
    """
    first = service.create_batch(db, configured.id, admin_user, [("第一次.pdf", _pdf_bytes("inv_a.pdf"))])
    service.preview_batch(db, first)
    service.discard_batch(db, first, admin_user)
    again = service.create_batch(db, configured.id, admin_user, [("再来一次.pdf", _pdf_bytes("inv_a.pdf"))])
    assert again.total == 1, "作废过的发票应当能重新导入"
    assert service.preview_batch(db, again)["error_count"] == 0


def test_作废会删掉记录与原件(db, store, configured, admin_user):
    """作废 = 「这批不要」。没人要的数据留着就是占地方。

    一张几十页的合并 PDF 有好几 MB，解析出来的 payload 更是每页一整段
    原文（JSONB）。用户传错、作废、再传 —— 这些东西没人再要。

    批次行本身留着：历史里还要看得见「什么时候传过什么、被作废了」。
    """
    first = service.create_batch(db, configured.id, admin_user, [("要作废.pdf", _pdf_bytes("inv_a.pdf"))])
    service.preview_batch(db, first)
    record_id = first.invoices[0].id
    file_path = first.file_path
    assert (store / file_path).exists(), "原件应当已经落盘"

    service.discard_batch(db, first, admin_user)

    assert db.get(InvoiceRecord, record_id) is None, "发票记录应当被删掉"
    assert not (store / file_path).exists(), "原件 PDF 应当被删掉"
    kept = db.get(InvoiceBatch, first.id)
    assert kept is not None and kept.status == "DISCARDED", "批次行留着，历史才看得见"


def test_待确认的批次仍然挡住重复上传(db, store, configured, admin_user):
    """还没确认的批次要挡住 —— 否则同一份发票会被传两遍、生成两张凭证。"""
    first = service.create_batch(db, configured.id, admin_user, [("待确认.pdf", _pdf_bytes("inv_a.pdf"))])
    assert first.status == "PENDING"
    with pytest.raises(ValueError, match="发票已存在"):
        service.create_batch(db, configured.id, admin_user, [("又传一遍.pdf", _pdf_bytes("inv_a.pdf"))])
    db.rollback()


# ------------------------------------------------------------------ 接口层


def _post_files(client, book, auth, files: list[tuple[str, bytes]]):
    return client.post(f"/api/books/{book.id}/invoices/batches", headers=auth, files=[
        ("files", (name, data, "application/pdf")) for name, data in files
    ])


def test_接口一次收多份文件并夹带非PDF(client, auth, configured, store):
    response = _post_files(client, configured, auth, [
        ("10月8日.pdf", _pdf_bytes("inv_a.pdf")),
        ("10月9日.pdf", _pdf_bytes("inv_b.pdf")),
        ("IMG_0021.HEIC", b"not-a-pdf"),
    ])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["sources"] == ["10月8日.pdf", "10月9日.pdf"]
    assert "IMG_0021.HEIC" in body["message"]
    assert body["invoices"][0]["source_file"] in ("10月8日.pdf", "10月9日.pdf")


def test_接口_上传完能直接确认生成(client, auth, configured, store):
    """上传结果的批次键必须能直接拿去 commit。

    这条钉的是键名：预览接口原来返回 ``batch_id``，而前端按 ``batch.id``
    去确认/重算/作废，于是「上传完点确认生成」必然 422 —— 上传是主路径。
    """
    created = _post_files(client, configured, auth, [
        ("10月8日.pdf", _pdf_bytes("inv_a.pdf")), ("10月9日.pdf", _pdf_bytes("inv_b.pdf")),
    ])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"] == body["batch_id"] if "batch_id" in body else True
    batch_id = body["id"]

    preview = client.post(f"/api/books/{configured.id}/invoices/batches/{batch_id}/preview",
                          headers=auth, json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["error_count"] == 0, preview.json()["invoices"][0]["errors"]

    committed = client.post(f"/api/books/{configured.id}/invoices/batches/{batch_id}/commit", headers=auth)
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 2


def test_接口_全是非PDF被拒(client, auth, configured, store):
    response = _post_files(client, configured, auth, [("照片.jpg", b"x")])
    assert response.status_code == 400
    assert "没有可解析的 PDF" in response.json()["detail"]


def test_接口_文件数超限返回413(client, auth, configured, store, monkeypatch):
    monkeypatch.setattr(settings, "invoice_max_files", 1)
    response = _post_files(client, configured, auth, [
        ("a.pdf", _pdf_bytes("inv_a.pdf")), ("b.pdf", _pdf_bytes("inv_b.pdf")),
    ])
    assert response.status_code == 413
    assert "一次最多上传 1 个文件" in response.json()["detail"]


def test_接口_合计体积超限返回413(client, auth, configured, store, monkeypatch):
    monkeypatch.setattr(settings, "invoice_max_total_bytes", 1024)
    response = _post_files(client, configured, auth, [
        ("a.pdf", _pdf_bytes("inv_a.pdf")), ("b.pdf", _pdf_bytes("inv_b.pdf")),
    ])
    assert response.status_code == 413
    assert "合计" in response.json()["detail"]


def test_重开批次仍报得出几张被忽略(client, auth, configured, store):
    """刷新页面 / 从历史里重开一个批次，「已忽略」这一格不能丢。

    忽略状态记在 invoice_record.status 上，批次表里没有这一列；重开走的是
    batch_summary，不现算的话页面上就是「共 3 张发票：2 张可生成」—— 第 3 张
    既不在可生成里也不在异常里，像凭空多出来的一页。
    """
    created = _post_files(client, configured, auth, [
        ("带封面的合并件.pdf", _merge([_blank_pdf_bytes(), _pdf_bytes("inv_a.pdf")])),
    ])
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    cover = next(row for row in created.json()["invoices"] if not row["invoice_no"])

    recomputed = client.post(f"/api/books/{configured.id}/invoices/batches/{batch_id}/preview",
                             headers=auth, json={"overrides": {str(cover["id"]): {"ignored": True}}})
    assert recomputed.status_code == 200, recomputed.text
    assert recomputed.json()["ignored_count"] == 1

    reopened = client.get(f"/api/books/{configured.id}/invoices/batches/{batch_id}", headers=auth)
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["ignored_count"] == 1, "重开批次后已忽略的张数要还在"
    assert body["ready_count"] + body["error_count"] + body["ignored_count"] == body["total"], \
        "三类张数加起来要等于总数，否则页面上有一页无处安放"
    assert next(r for r in body["invoices"] if r["id"] == cover["id"])["status"] == "IGNORED"

    listed = client.get(f"/api/books/{configured.id}/invoices/batches", headers=auth)
    assert listed.status_code == 200, listed.text
    assert next(b for b in listed.json() if b["id"] == batch_id)["ignored_count"] == 1, \
        "历史列表也要算得出已忽略张数"
