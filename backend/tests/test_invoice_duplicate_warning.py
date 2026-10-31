"""发票导入的「可能重复记账」提示。

**这件事要解决什么。**

有些发票是**手工先记了账**，后来才把票传上来导入 —— 再生成一张凭证，同一
笔钱就在账上记了两遍。系统拦不住这种重复：发票号码是这张票的**唯一身份**，
而手工记的那笔账根本没有号码可比（号码级拦截见
``service._existing_invoice_numbers``）。

所以导入时要**提示**：这张发票的金额，在它开票**那个月**的账上，已经有对
应的分录了。

**口径**（``app/invoice/duplicates.py``）：同月 + 同额 + 同向 + 同结算科目。

这一组用例盯住：

1. 同月同额同向同科目 → **有提示**，并说清是哪张凭证、哪条分录；
2. 期间 / 金额 / 方向 / 科目**差一样** → **没有提示**（宁可漏报，也不要
   天天报警 —— 管理费每月同额，报警多了就没人看了）；
3. **只提示、不拦**：那张发票照样「可生成」，照样能生成凭证；
4. **已入账的不提示**（它已经在账上了）；
5. **本批次自己生成的凭证不算重复**（同一批两张同额发票是两张票，不是
   重复记账）。
"""
from __future__ import annotations

from decimal import Decimal

from app.invoice import service
from app.models import InvoiceBatch, InvoiceRecord


def _config(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
        "output_debit_subject_id": subjects["1002"].id,
        "output_tax_subject_id": subjects["22210106"].id,
        "output_fallback_subject_id": subjects["5001"].id,
    })


def _batch_with(db, book, count: int = 1, *, amount: str = "100.00",
                issue_date: str = "2026-10-01", invoice_type: str = "INPUT",
                tax: str = "0.00", base: str | None = None):
    """一个批次，count 张发票。默认一张 100.00 元的进项票。"""
    batch = InvoiceBatch(book_id=book.id, filename="十月.pdf", file_path="",
                          status="PENDING", total=count, created_by="tester")
    db.add(batch)
    db.flush()
    total = Decimal(amount)
    base_amount = Decimal(base) if base is not None else total - Decimal(tax)
    for index in range(count):
        number = f"2441700000004444444{index}"
        record = InvoiceRecord(
            batch_id=batch.id, book_id=book.id, page_index=index,
            invoice_no=number, issue_date=__import__("datetime").date.fromisoformat(issue_date),
            invoice_type=invoice_type, status="PENDING",
            payload={
                "meta": {"invoice_no": number, "issue_date": issue_date,
                         "seller_name": "某供应商", "seller_tax_id": "91310000MA2AYXC79Y",
                         "buyer_name": "测试公司", "buyer_tax_id": "91420100MA1234567X",
                         "total_excl_tax": f"{base_amount}", "total_tax": f"{tax}",
                         "total_incl_tax": f"{total}"},
                "lines": [{"name": "油", "display_name": "油",
                           "amount": f"{base_amount}", "tax": f"{tax}"}],
            },
        )
        db.add(record)
        db.flush()
    return batch


def _warning_of(rows, index: int = 0):
    """从一批发票行里取「可能重复记账」的提示。"""
    return rows[index].get("duplicate_warning")


def _warnings(payload, index: int = 0):
    return _warning_of(payload["invoices"], index)


# --------------------------------------------------------------- 有提示的那一种


def test_同月同额同向有提示(db, book, subjects, make_voucher, admin_user):
    """手工先记了 100 元，导入一张 100 元的进项票 —— 必须提示。"""
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1)

    payload = service.preview_batch(db, batch)

    warning = _warnings(payload)
    assert warning, "同月账上已经有同额分录，却一点提示都没有"
    assert warning["period"] == "2026-10", warning
    assert warning["amount"] == "100.00", warning
    assert warning["direction"] == "CREDIT", warning
    assert len(warning["matches"]) == 1, warning
    hit = warning["matches"][0]
    assert hit["subject_code"] == "1002", hit
    assert hit["subject_name"] == "银行存款", hit
    assert hit["direction"] == "CREDIT", hit
    assert hit["amount"] == "100.00", hit
    assert hit["voucher_no"], hit
    assert hit["voucher_date"] == "2026-10-05", hit


def test_提示只说不拦这张发票照样能生成(db, book, subjects, make_voucher, admin_user):
    """**只提示，不拦。** 号码才是唯一身份，金额相同不代表是同一笔。

    拦下来的话，用户就传不进这张票了 —— 而它可能真的是另一笔。
    """
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1)

    payload = service.preview_batch(db, batch)

    assert payload["ready_count"] == 1, "有提示就把票判成不能生成了？"
    assert payload["error_count"] == 0, payload
    assert payload["invoices"][0]["status"] == "READY", payload["invoices"][0]


def test_销项票按借方找(db, book, subjects, make_voucher, admin_user):
    """销项是「收钱」，结算那条分录是**借方** —— 方向必须跟着变。"""
    _config(db, book, subjects)
    # 手工记的一笔收款：借 银行存款 100 / 贷 主营业务收入 93.46 / 贷 销项税额 6.54
    make_voucher("2026-10-05", dr=[("1002", 100, "收款")],
                 cr=[("5001", "93.46", "货款"), ("22210106", "6.54", "销项税")])
    batch = _batch_with(db, book, 1, invoice_type="OUTPUT", amount="100.00",
                         base="93.46", tax="6.54")

    payload = service.preview_batch(db, batch)

    warning = _warnings(payload)
    assert warning, "销项票在同月有同额收款分录，却没有提示"
    assert warning["direction"] == "DEBIT", warning
    assert len(warning["matches"]) == 1, warning


# --------------------------------------------------------- 不该有提示的那几种


def test_别的期间不提示(db, book, subjects, make_voucher, admin_user):
    """10 月记的那笔，拦不住 11 月的票 —— 期间是判据之一。"""
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1, issue_date="2026-11-01")

    payload = service.preview_batch(db, batch)

    assert _warnings(payload) is None, "跨期也报警 —— 管理费每月同额，那样就没人看了"


def test_金额不一样不提示(db, book, subjects, make_voucher, admin_user):
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1, amount="88.00")

    payload = service.preview_batch(db, batch)

    assert _warnings(payload) is None, "金额都不一样也提示"


def test_方向不一样不提示(db, book, subjects, make_voucher, admin_user):
    """进项找贷方。账上只有一条**借方** 100 元（收到钱）—— 那不是同一笔。"""
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("1002", 100, "收款")], cr=[("5001", 100, "货款")])
    batch = _batch_with(db, book, 1)

    payload = service.preview_batch(db, batch)

    assert _warnings(payload) is None, "方向不对也提示"


def test_科目不一样不提示(db, book, subjects, make_voucher, admin_user):
    """同额同向，但记在**库存现金**上 —— 与这张票要结的银行账户不是一回事。"""
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1001", 100, "付油款")])
    batch = _batch_with(db, book, 1)

    payload = service.preview_batch(db, batch)

    assert _warnings(payload) is None, "科目不同也提示"


def test_账上什么都没有不提示(db, book, subjects, admin_user):
    _config(db, book, subjects)
    batch = _batch_with(db, book, 1)

    payload = service.preview_batch(db, batch)

    assert _warnings(payload) is None, "账上空的就凭空提示"


# ------------------------------------------------------------- 边界与不误报


def test_已经入账的不再提示(db, book, subjects, make_voucher, admin_user):
    """已经生成过凭证的票已经在账上了，再说「可能重复」是废话。"""
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)

    result = service.commit_batch(db, batch, admin_user)

    assert result["created"] == 1, result
    payload = service.preview_batch(db, batch)
    assert _warnings(payload) is None, "已入账的票不该再提示"


def test_本批次自己生成的凭证不算重复(db, book, subjects, make_voucher, admin_user):
    """同一批里两张金额相同的发票是**两张票**，不是重复记账。

    自己刚生成的凭证不该拿来提示自己。
    """
    _config(db, book, subjects)
    batch = _batch_with(db, book, 2)
    service.preview_batch(db, batch)
    records = sorted(batch.invoices, key=lambda r: r.page_index)
    voucher = make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    # 假装第一张已经生成了凭证（凭证号用不上，只看它有没有被排除）。
    records[0].voucher_id = voucher.id
    db.flush()

    rows = service.serialize_records(db, batch, records)

    assert _warning_of(rows, 0) is None, "已入账的那张不该提示"
    assert _warning_of(rows, 1) is None, "拿本批次自己的凭证去提示自己，是噪声"


def test_另一个批次的发票入账后同额新票要提示(db, book, subjects, make_voucher, admin_user):
    """别的批次入过账的同额分录**要**提示 —— 那正是重复记账的高发区。"""
    _config(db, book, subjects)
    voucher = make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)
    records = sorted(batch.invoices, key=lambda r: r.page_index)
    # 凭证属于**另一个批次**（这里只挂个 id 表明它不是本批次的）。
    assert voucher.id not in {r.voucher_id for r in records}

    rows = service.serialize_records(db, batch, records)

    warning = _warning_of(rows)
    assert warning, "别的批次入过账的同额分录，没有提示"
    assert warning["matches"][0]["voucher_no"] == voucher.voucher_no, warning


def test_批次详情接口也带着提示(db, book, subjects, make_voucher, client, auth, admin_user):
    """**批次页与预览页是同一张表**，两处口径必须一致。

    漏一处，用户从「处理历史」点开一个批次就看不到提示了 —— 而那正是
    他想回头核对的时候。
    """
    _config(db, book, subjects)
    make_voucher("2026-10-05", dr=[("560201", 100, "油")], cr=[("1002", 100, "付油款")])
    batch = _batch_with(db, book, 1)
    service.preview_batch(db, batch)

    r = client.get(f"/api/books/{book.id}/invoices/batches/{batch.id}", headers=auth)

    assert r.status_code == 200, r.text
    warning = r.json()["invoices"][0].get("duplicate_warning")
    assert warning, "批次详情里没有「可能重复记账」的提示"
    assert warning["matches"][0]["subject_code"] == "1002", warning
