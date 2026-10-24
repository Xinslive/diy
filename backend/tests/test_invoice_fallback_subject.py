"""发票科目规则的**兜底科目**，以及「一张发票一张凭证、按项目列支」。

用户提了两件事：

1. 进项发票上什么奇怪的项目都有（「*油料*粮油米面」这种带税局分类前缀的），
   不可能把见过的项目名都配成规则 —— 需要一个**兜底科目**。
   配不出规则的项目落到它上面，而不是把整张发票（乃至整批）作废。
2. 「一张发票一张凭证」指的是**一张凭证上列支这张发票的所有项目**，
   而且**借方摘要就是发票上的项目名**。

这两条都写在这里，因为它们改的是同一段代码：分录从「按科目合并」改成
「按项目逐项列支」，而「没匹配上规则的项目」正是逐项列支之后才有的行。
"""
from __future__ import annotations

import pathlib

import pytest

from app.invoice import service
from app.models import InvoiceBatch, InvoiceRecord

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "invoices"
BUYER_TAX_ID = "91420100MA1234567X"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path / "invoices"))
    return tmp_path / "invoices"


def _config(db, book, subjects, **overrides):
    payload = {
        "taxpayer_id": BUYER_TAX_ID,
        "output_tax_subject_id": subjects["22210106"].id,
        "input_credit_subject_id": subjects["1002"].id,
        "output_debit_subject_id": subjects["1002"].id,
    }
    payload.update(overrides)
    return service.save_config(db, book.id, payload)


def _make_record(db, book, *, lines, invoice_type="INPUT", page_index=0,
                 invoice_no="24417000000099999999", total="106.00",
                 base="100.00", tax="6.00", seller="某供应商有限公司"):
    """造一张发票记录 —— 用来测「一张发票多个项目」这种夹具里没有的形状。"""
    batch = InvoiceBatch(book_id=book.id, filename="造出来的.pdf", file_path="",
                          status="PENDING", total=1, created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=page_index,
        invoice_no=invoice_no, invoice_type=invoice_type, status="PENDING",
        payload={
            "meta": {
                "invoice_no": invoice_no, "issue_date": "2026-10-01",
                "seller_name": seller, "seller_tax_id": "91310000MA0000000X",
                "buyer_name": "测试公司", "buyer_tax_id": BUYER_TAX_ID,
                "total_excl_tax": base, "total_tax": tax, "total_incl_tax": total,
            },
            "lines": lines,
        },
    )
    db.add(record)
    db.flush()
    return batch, record


def _line(name, amount, tax="0.00", display=None):
    return {"name": name, "display_name": display if display is not None else name,
            "amount": amount, "tax": tax, "tax_rate": "6%"}


# ------------------------------------------------------------- 兜底：落科目不报错


def test_没命中规则_落到兜底科目(db, book, subjects):
    """配不出规则的项目落到兜底科目，整张发票照常可生成。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    previews, ready, errors = service._build_preview(
        db, _batch, [], service.get_config(db, book.id), {})

    assert errors == 0, f"配了兜底就不该再报错：{previews[0]['errors']}"
    assert ready == 1
    entries = previews[0]["resolution"]["entries"]
    debit = next(e for e in entries if e["direction"] == "DEBIT")
    assert debit["subject_id"] == subjects["560201"].id, "未命中的项目记到兜底科目"


def test_没配兜底_仍然报错(db, book, subjects):
    """兜底是可选的。没配就沿用旧行为：这张发票有问题，整批确认不了。"""
    _config(db, book, subjects)
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    previews, ready, errors = service._build_preview(
        db, _batch, [], service.get_config(db, book.id), {})

    assert errors == 1 and ready == 0
    assert any("没有命中科目规则" in e for e in previews[0]["errors"]), previews[0]["errors"]


def test_兜底的行在预览里标出来(db, book, subjects):
    """兜底行是「先记着、等人核对」，不是「已经对了」—— 界面上要看得出来。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    previews, _, _ = service._build_preview(db, _batch, [], service.get_config(db, book.id), {})

    resolution = previews[0]["resolution"]["line_resolution"][0]
    assert resolution["fallback"] is True
    assert resolution["reason"] == "兜底科目"


def test_命中规则的行不走兜底(db, book, subjects, admin_user):
    """规则命中的照旧记规则里那个科目，兜底只兜「没匹配上」的。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    service.create_rule(db, book.id, admin_user, {
        "match_type": "ITEM", "invoice_type": "BOTH", "keyword": "粮油",
        "subject_id": subjects["560202"].id, "priority": 0, "active": True,
    })
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    from app.models import InvoiceRule

    previews, _, errors = service._build_preview(
        db, _batch, db.query(InvoiceRule).all(), service.get_config(db, book.id), {})

    assert errors == 0
    debit = next(e for e in previews[0]["resolution"]["entries"] if e["direction"] == "DEBIT")
    assert debit["subject_id"] == subjects["560202"].id, "命中规则就不该用兜底"
    assert previews[0]["resolution"]["line_resolution"][0]["fallback"] is False


def test_规则冲突不走兜底(db, book, subjects, admin_user):
    """同优先级两个规则指向不同科目是**配错了**，兜底不能把配错悄悄盖住。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    for code in ("560202", "560207"):
        service.create_rule(db, book.id, admin_user, {
            "match_type": "ITEM", "invoice_type": "BOTH", "keyword": "粮油",
            "subject_id": subjects[code].id, "priority": 0, "active": True,
        })
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    from app.models import InvoiceRule

    previews, ready, errors = service._build_preview(
        db, _batch, db.query(InvoiceRule).all(), service.get_config(db, book.id), {})

    assert errors == 1 and ready == 0
    assert any("多条同优先级规则命中不同科目" in e for e in previews[0]["errors"]), previews[0]["errors"]


def test_进项销项用各自的兜底科目(db, book, subjects):
    """进项兜底记成本费用、销项兜底记收入 —— 混用必有一边记错。"""
    _config(db, book, subjects,
            input_fallback_subject_id=subjects["560201"].id,
            output_fallback_subject_id=subjects["5001"].id)
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    previews, _, _ = service._build_preview(db, _batch, [], service.get_config(db, book.id), {})
    debit = next(e for e in previews[0]["resolution"]["entries"] if e["direction"] == "DEBIT")
    assert debit["subject_id"] == subjects["560201"].id


def test_兜底科目必须是本账套的末级科目(db, book, subjects):
    """兜底科目和别的配置科目一样要校验 —— 记到停用/非末级科目上比不记更糟。"""
    from app.models import Subject

    non_leaf = db.query(Subject).filter(
        Subject.book_id == book.id, Subject.code == "5602").first()
    assert non_leaf is not None and not non_leaf.is_leaf

    with pytest.raises(ValueError, match="末级科目"):
        service.save_config(db, book.id, {"input_fallback_subject_id": non_leaf.id})


def test_没配结算科目_报的是人话而不是500(db, book, subjects):
    """少了结算科目时不能抛 TypeError。

    以前这里直接 ``int(None)``，接口层翻成 500，用户看到的是「服务器错误」，
    而真正的原因（「未配置结算科目」）就在上一行写着，永远轮不到显示。
    """
    service.save_config(db, book.id, {"taxpayer_id": BUYER_TAX_ID,
                                       "input_fallback_subject_id": subjects["560201"].id})
    _batch, record = _make_record(db, book, lines=[_line("*油料*粮油米面", "100.00", "6.00")])

    previews, ready, errors = service._build_preview(
        db, _batch, [], service.get_config(db, book.id), {})

    assert errors == 1 and ready == 0
    assert any("未配置结算科目" in e for e in previews[0]["errors"]), previews[0]["errors"]


# ------------------------------------------------- 一张发票一个凭证：按项目列支


def test_一张发票多个项目_每个项目一条借方(db, book, subjects):
    """一张发票买了三样东西 → 一张凭证上三条借方，**不按科目合并**。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    _batch, record = _make_record(
        db, book,
        lines=[_line("*油料*粮油米面", "30.00"), _line("*美容护肤品*日用杂品", "40.00"),
               _line("*办公用品*打印纸", "30.00")],
        total="100.00", base="100.00", tax="0.00")

    previews, _, errors = service._build_preview(
        db, _batch, [], service.get_config(db, book.id), {})

    assert errors == 0
    entries = previews[0]["resolution"]["entries"]
    debits = [e for e in entries if e["direction"] == "DEBIT"]
    assert len(debits) == 3, f"三个项目三条借方，实际 {len(debits)} 条"
    assert [e["amount"] for e in debits] == ["30.00", "40.00", "30.00"]


def test_借方摘要是发票的项目名(db, book, subjects):
    """摘要写项目名 —— 用户核对的是「买的是什么」。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    _batch, record = _make_record(
        db, book,
        lines=[_line("*油料*粮油米面6930536003708", "30.00", display="*油料*粮油米面"),
               _line("*美容护肤品*日用杂品", "40.00")],
        total="70.00", base="70.00", tax="0.00")

    previews, _, errors = service._build_preview(
        db, _batch, [], service.get_config(db, book.id), {})

    assert errors == 0
    debits = [e for e in previews[0]["resolution"]["entries"] if e["direction"] == "DEBIT"]
    assert [e["summary"] for e in debits] == ["*油料*粮油米面", "*美容护肤品*日用杂品"]


def test_同科目的两个项目也不合并(db, book, subjects):
    """两个项目碰巧都归到同一个科目时，也是两条借方 —— 凭证要看得见买了几样。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    _batch, record = _make_record(
        db, book, lines=[_line("油", "30.00"), _line("米", "40.00")],
        total="70.00", base="70.00", tax="0.00")

    previews, _, errors = service._build_preview(
        db, _batch, [], service.get_config(db, book.id), {})

    assert errors == 0
    debits = [e for e in previews[0]["resolution"]["entries"] if e["direction"] == "DEBIT"]
    assert len(debits) == 2
    assert [e["summary"] for e in debits] == ["油", "米"]


def test_结算行摘要还是发票号(db, book, subjects):
    """结算行对应的是整张发票的钱，摘要仍然是「发票 <号码>」。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    _batch, record = _make_record(db, book, lines=[_line("油", "100.00", "6.00")])

    previews, _, _ = service._build_preview(db, _batch, [], service.get_config(db, book.id), {})
    credit = next(e for e in previews[0]["resolution"]["entries"] if e["direction"] == "CREDIT")
    assert credit["summary"] == "发票 24417000000099999999"


# --------------------------------------------------------------- 端到端：真 PDF


def test_真实发票PDF_走兜底不报错(db, store, book, subjects, admin_user):
    """用真的电子发票 PDF 走一遍：配了兜底，整批能生成。"""
    _config(db, book, subjects, input_fallback_subject_id=subjects["560201"].id)
    data = (FIXTURES / "inv_a.pdf").read_bytes()
    batch = service.create_batch(db, book.id, admin_user, [("发票.pdf", data)])

    preview = service.preview_batch(db, batch)

    assert preview["error_count"] == 0, preview["invoices"][0]["errors"]
    assert preview["ready_count"] == 1
    entries = preview["invoices"][0]["resolution"]["entries"]
    debit = next(e for e in entries if e["direction"] == "DEBIT")
    assert debit["subject_id"] == subjects["560201"].id
    # 摘要用发票上的项目名（进项税额并入第一条明细行，摘要不加任何系统标记）
    #
    # 「餐费」后面的「2」是**数量**，PDF 文字层把它粘在了项目名后面 ——
    # 摘要里不该有它。
    assert debit["summary"].startswith("*餐饮服务*餐费"), debit["summary"]
    assert "餐费2" not in debit["summary"], debit["summary"]
