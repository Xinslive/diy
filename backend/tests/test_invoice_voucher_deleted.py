"""凭证删掉了，那张发票还能不能再生成一张凭证。

**这个洞是怎么来的。**

发票记录上有一条不变式：

    ``invoice_record.status == 'COMMITTED'`` ⟺ ``invoice_record.voucher_id IS NOT NULL``

外键 ``invoice_record.voucher_id → voucher.id`` 是 ``ON DELETE SET NULL``：
凭证一删，``voucher_id`` 自动变空，**但 ``status`` 还停在「已生成」**。

那条记录于是变成「孤儿」—— 状态说已生成，账上却什么都没有。它不是显示
难看的问题，是**这张发票再也变不出凭证**：

* 服务端对「已生成」的记录跳过重算，它不会变回「可生成」；
* 「确认生成」只认「可生成」的那些，所以点下去是 0 张。

用户的处境：账上那张凭证他亲手删了，界面上这张发票还标着「已生成」，
却再也没有任何按钮能让他用这张票重新生成一张凭证。

这一组盯的是**后果**，不是实现：

1. 删掉凭证之后，那张发票重新变回「可生成」，而且真的能再生成一张凭证；
2. 计数要跟着走（界面上「确认生成 N 张」的按钮不能是灰的）；
3. 重新预览不能把它变回「已生成」，也不能报「没有待生成的发票」；
4. 人工改过的科目不能被重新计算冲掉（自动重算是要反复跑的）；
5. 「来源文件」那一列，**打开批次页就该看得见** —— 不该要点一次
   「重新计算预览」才冒出来。
"""
from __future__ import annotations

from sqlalchemy import select

from app.accounting import vouchers as voucher_service
from app.invoice import service
from app.models import InvoiceBatch, InvoiceRecord, Voucher


def _config(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })


def _batch_with(db, book, count: int = 1, source_file: str = "六月.pdf"):
    batch = InvoiceBatch(book_id=book.id, filename=source_file, file_path="",
                          status="PENDING", total=count, created_by="tester")
    db.add(batch)
    db.flush()
    for index in range(count):
        number = f"2441700000003333333{index}"
        record = InvoiceRecord(
            batch_id=batch.id, book_id=book.id, page_index=index,
            invoice_no=number, invoice_type="INPUT", status="PENDING",
            payload={
                "source_file": source_file,
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


# --------------------------------------------------------------------------
# 1. 删掉凭证之后，那张发票重新变回「可生成」
# --------------------------------------------------------------------------


def test_删掉凭证后发票变回可生成(db, book, subjects, admin_user):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    record = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id)).one()
    voucher = db.get(Voucher, record.voucher_id)
    assert record.status == "COMMITTED"

    voucher_service.delete_voucher(db, voucher, admin_user)

    db.refresh(record)
    assert record.voucher_id is None, "凭证没了，指向必须是空的"
    assert record.status == "READY", (
        "凭证都删了，这张发票还标着「已生成」—— "
        "它就再也变不出凭证了")


def test_删掉凭证后能再生成一张凭证(db, book, subjects, admin_user):
    """用户真正要的那件事：删掉凭证之后，**还能再用这张发票生成凭证**。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    first = _vouchers(db, book)[0]
    voucher_service.delete_voucher(db, first, admin_user)

    # 重新预览（界面上打开批次就会自动跑一次），然后确认生成。
    preview = service.preview_batch(db, batch)
    assert preview["ready_count"] == 1, preview

    result = service.commit_batch(db, batch, admin_user)
    assert result["created"] == 1, result

    vouchers = _vouchers(db, book)
    assert len(vouchers) == 1, "删掉一张再生成一张，账上仍然只有一张"
    record = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id)).one()
    assert record.status == "COMMITTED"
    assert record.voucher_id == vouchers[0].id


def test_删掉凭证后计数要跟着走(db, book, subjects, admin_user):
    """界面上「确认生成 N 张凭证」那个按钮，靠的是 ``ready_count``。

    它不跟着走的话，用户看到的还是「0 张可生成」，按钮永远是灰的 ——
    于是「能重新生成」这件事在界面上根本做不到。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    counts = service.status_counts(db, [batch.id]).get(batch.id, {})
    assert counts.get("READY", 0) == 0

    voucher_service.delete_voucher(db, _vouchers(db, book)[0], admin_user)

    counts = service.status_counts(db, [batch.id]).get(batch.id, {})
    assert counts.get("READY", 0) == 1, counts


def test_删掉凭证后批次的已入账张数要减(db, book, subjects, admin_user):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)
    assert batch.committed_count == 2

    voucher_service.delete_voucher(db, _vouchers(db, book)[0], admin_user)
    assert batch.committed_count == 1, batch.committed_count
    assert batch.status == "COMMITTED", "还有一张在账上，批次就该是已生成"


def test_凭证全删光后批次回到待确认(db, book, subjects, admin_user):
    """一张都不剩时，「已生成」那一栏是骗人的。

    界面上那一栏写着「已生成」而实际上一张凭证都没有，看着像还有账在账上。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    for voucher in list(_vouchers(db, book)):
        voucher_service.delete_voucher(db, voucher, admin_user)

    assert batch.status == "PENDING", batch.status
    assert batch.committed_count == 0, batch.committed_count


def test_删掉凭证后重新预览不报错(db, book, subjects, admin_user):
    """以前这里会得到一句「该批次已是 COMMITTED 状态，没有待生成的发票」。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    voucher_service.delete_voucher(db, _vouchers(db, book)[0], admin_user)

    preview = service.preview_batch(db, batch)
    assert preview["ready_count"] == 1, preview
    assert preview["invoices"][0]["status"] == "READY"


def test_没删凭证的发票不会被重算成可生成(db, book, subjects, admin_user):
    """**安全底座**：删一张凭证不能把别的发票也放回「可生成」。

    放回去了，下一次确认就会给同一张发票**再生成一张凭证** ——
    账上凭空多一笔，还查不出是哪张干的。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    service.commit_batch(db, batch, admin_user)

    voucher_service.delete_voucher(db, _vouchers(db, book)[0], admin_user)

    preview = service.preview_batch(db, batch)
    statuses = sorted(row["status"] for row in preview["invoices"])
    assert statuses == ["COMMITTED", "READY"], statuses
    assert len(_vouchers(db, book)) == 1, "没删的那张凭证不能动"


# --------------------------------------------------------------------------
# 2. 人工改过的科目不能被重新计算冲掉
# --------------------------------------------------------------------------


def test_重新计算不会冲掉人工改过的科目(db, book, subjects, admin_user):
    """人工改过的科目是**结论**，不是待算的草稿。

    前端只在用户**动过**某一行时才有那一行的覆盖项 —— 刷新页面后它是空的。
    于是刷新页面再点一次「重新计算预览」，用户上次挑的科目就被悄悄换回
    规则里的那个：页面上看着像「改过」，账上记的却是另一个科目。

    （打开批次页会自动重新计算，所以这条每天都在被考验。）
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    manual = subjects["1001"].id  # 库存现金：不在配置里，改它才看得出来

    # 用户手工把结算科目改成 manual。
    service.preview_batch(db, batch, {str(batch.invoices[0].id): {
        "settlement_subject_id": manual,
    }})

    # 刷新页面后前端手上是空的 —— 再算一次。
    preview = service.preview_batch(db, batch)

    row = preview["invoices"][0]
    assert row["status"] == "READY", row
    credit = next(e for e in row["resolution"]["entries"] if e["direction"] == "CREDIT")
    assert int(credit["subject_id"]) == manual, (
        "人工指定的结算科目被重新计算冲掉了")


def test_这次传进来的覆盖项盖过落库的(db, book, subjects, admin_user):
    """用户**刚**做的决定比上次做的更近 —— 新的赢。"""
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)
    first = subjects["1001"].id
    second = subjects["560202"].id

    service.preview_batch(db, batch, {str(batch.invoices[0].id): {
        "settlement_subject_id": first,
    }})
    preview = service.preview_batch(db, batch, {str(batch.invoices[0].id): {
        "settlement_subject_id": second,
    }})

    row = preview["invoices"][0]
    credit = next(e for e in row["resolution"]["entries"] if e["direction"] == "CREDIT")
    assert int(credit["subject_id"]) == second


def test_落库的覆盖项指向已停用科目时_不拖垮整批(db, book, subjects, admin_user):
    """历史不该拖垮现在。

    用户上次挑的科目，今天被停用了。若还照用，预览会直接报错
    （「人工覆盖科目包含已停用科目」），**整个批次都打不开** ——
    一张票的历史决定拖垮了另外几十张。

    正确做法：把那一项丢掉，让它回落规则/配置科目，界面上看得到来源变了。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    stopped = subjects["1001"]

    # 先用还没停用的科目算一遍，把覆盖项落进库（模拟「用户上次挑了它」）。
    service.preview_batch(db, batch, {str(batch.invoices[0].id): {
        "settlement_subject_id": stopped.id,
    }})
    record = batch.invoices[0]
    saved = (record.payload or {}).get("resolution", {}).get("overrides", {})
    assert int(saved.get("settlement_subject_id")) == stopped.id, saved

    # 那个科目今天被停用了。
    stopped.active = False
    db.flush()

    # 刷新页面后前端手上是空的 —— 再算一次。
    preview = service.preview_batch(db, batch)
    assert preview["ready_count"] == 2, preview


# --------------------------------------------------------------------------
# 3. 「来源文件」那一列，打开批次页就该看得见
# --------------------------------------------------------------------------


def test_批次详情接口带来源文件(client, db, book, subjects, auth):
    """那一列看的是 ``sources``，而以前只有预览接口带。

    于是用户打开批次页时那一列是空的，点一次「重新计算预览」才看见 ——
    像是这列数据要算出来才有，其实它一直都在库里。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1, source_file="七月.pdf")
    service.preview_batch(db, batch)

    r = client.get(f"/api/books/{book.id}/invoices/batches/{batch.id}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("sources") == ["七月.pdf"], body
