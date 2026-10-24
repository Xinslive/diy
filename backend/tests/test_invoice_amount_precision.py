"""发票金额的精度：不能有浮点尾数，各项目之和要等于发票金额。

这两件事是**同一个根**：金额用 float 表示。

* **尾数** —— 折扣行要合并（净额），一加就冒出「1.4100000000000001」：
  ``1.61 + (-0.20)`` 在二进制浮点里就是这个数。它会原样出现在界面的
  「税额」列里 —— 用户看得见，也不敢信。
* **对账** —— 「各项目之和 = 发票金额」这条恒等式差 1e-16 就不成立；
  差一分钱，凭证就借贷不平，而用户在预览页看不出来，只在点「确认生成」
  时才炸（那时整批回滚）。

所以金额一律走 **Decimal**（从文本直接解析，一位不多一位不少），
并且在服务层把「各项目之和 = 发票金额」**做成硬保证**而不是碰运气。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.invoice import service
from app.invoice.text_parser import parse_page

# 一张带折扣行的进项发票。折扣行是浮点尾数最容易冒出来的地方：
# 1.61 + (-0.20) 在 float 里是 1.4100000000000001。
PAGE_TEXT = """电子发票（普通发票）
发票号码：24417000000033333333
开票日期：2026年10月01日
某某商贸有限公司
91420100MA1234567X
某某供应链有限公司
91310000MA2AYXC79Y
项目名称 规格型号 单位 数量 单价 金额 税率/征收率 税额
*调味品*调味品 6972343076094 件 1.00 6.02 6.02 13% 0.78
*调味品*调味品 -0.75 13% -0.10
*洗涤剂*洗护清洁用品 8885010238023 件 1.00 13.19 13.19 13% 1.71
*洗涤剂*洗护清洁用品 -1.61 13% -0.21
*塑料制品*餐饮具 6917751430076 件 1.00 12.39 12.39 13% 1.61
*塑料制品*餐饮具 -1.51 13% -0.20
合 计 ¥27.73 ¥3.59
价税合计（大写）叁拾壹圆叁角贰分 （小写）¥31.32
开票人：王五
"""


def _parsed():
    return parse_page(0, PAGE_TEXT)


# ------------------------------------------------------------------ 解析层精度


def test_折扣行合并后没有浮点尾数():
    """1.61 + (-0.20) 必须是 1.41，不是 1.4100000000000001。"""
    invoice = _parsed()
    tax = invoice.lines[2].tax
    assert tax == Decimal("1.41"), f"税额是 {tax!r}"
    assert str(tax) == "1.41", f"落库的是 {tax!r} —— 界面上就会显示这一串"


def test_金额本身是精确的Decimal():
    invoice = _parsed()
    assert invoice.lines[0].amount == Decimal("5.27")
    assert invoice.lines[1].amount == Decimal("11.58")
    assert invoice.lines[2].amount == Decimal("10.88")


def test_各项目之和正好等于发票的金额():
    """不是「差一点点」，是**正好**。"""
    invoice = _parsed()
    lines_sum = sum((line.amount for line in invoice.lines), Decimal("0"))
    tax_sum = sum((line.tax for line in invoice.lines), Decimal("0"))
    assert lines_sum == invoice.meta.total_excl_tax == Decimal("27.73")
    assert tax_sum == invoice.meta.total_tax == Decimal("3.59")
    assert lines_sum + tax_sum == invoice.meta.total_incl_tax == Decimal("31.32")


def test_落库的是字符串不是浮点数():
    """payload 存 JSONB。存 float 就等于把二进制近似值存进账里。"""
    payload = _parsed().to_dict()
    for line in payload["lines"]:
        assert isinstance(line["amount"], str), line
        assert isinstance(line["tax"], str), line
    assert payload["lines"][2]["tax"] == "1.41"
    assert payload["meta"]["total_incl_tax"] == "31.32"


# ------------------------------------------------------------------ 服务层对账


def _record(db, book, *, lines, base, tax, total):
    from app.models import InvoiceBatch, InvoiceRecord

    batch = InvoiceBatch(book_id=book.id, filename="对账.pdf", file_path="",
                          status="PENDING", total=1, created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=0,
        invoice_no="24417000000033333333", invoice_type="INPUT", status="PENDING",
        payload={
            "meta": {"invoice_no": "24417000000033333333", "issue_date": "2026-10-01",
                     "seller_name": "某供应商", "seller_tax_id": "91310000MA2AYXC79Y",
                     "buyer_name": "测试公司", "buyer_tax_id": "91420100MA1234567X",
                     "total_excl_tax": base, "total_tax": tax, "total_incl_tax": total},
            "lines": lines,
        },
    )
    db.add(record)
    db.flush()
    return batch


def _line(name, amount, tax="0.00"):
    return {"name": name, "display_name": name, "amount": amount, "tax": tax}


def _entries(preview):
    return preview["resolution"]["entries"]


def _dr(entries):
    return sum((Decimal(e["amount"]) for e in entries if e["direction"] == "DEBIT"), Decimal("0"))


def _cr(entries):
    return sum((Decimal(e["amount"]) for e in entries if e["direction"] == "CREDIT"), Decimal("0"))


def test_凭证借贷相等且等于发票金额(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })
    batch = _record(db, book, lines=[_line("油", "5.27", "0.68"), _line("米", "11.58", "1.50")],
                       base="16.85", tax="2.18", total="19.03")
    preview, ready, errors = service._build_preview(db, batch, [], service.get_config(db, book.id), {})

    assert errors == 0, preview[0]["errors"]
    entries = _entries(preview[0])
    assert _dr(entries) == _cr(entries) == Decimal("19.03")


def test_末项吸收几分钱_让各项目之和等于发票金额(db, book, subjects):
    """发票的合计与明细差一分（发票上就常这么印）—— 末项补上，凭证照样平。"""
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })
    batch = _record(db, book, lines=[_line("油", "10.00"), _line("米", "19.99")],
                       base="30.00", tax="0.00", total="30.00")
    preview, ready, errors = service._build_preview(db, batch, [], service.get_config(db, book.id), {})

    assert errors == 0, preview[0]["errors"]
    entries = _entries(preview[0])
    assert _dr(entries) == _cr(entries) == Decimal("30.00"), "各项目之和必须等于发票金额"
    amounts = preview[0]["resolution"]["line_amounts"]
    assert amounts["1"] == "20.00", f"末项应当补上那一分，实际 {amounts}"
    assert preview[0]["resolution"]["rounding"] == "0.01"


def test_差得多不是舍入_当异常报出去(db, book, subjects):
    """差 90 块不是「几分钱的舍入」，是**少解析了一项**。

    那时不能替用户编一个金额出来 —— 宁可让这张发票标红等人核对。
    """
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })
    batch = _record(db, book, lines=[_line("油", "10.00")],
                       base="100.00", tax="0.00", total="100.00")
    preview, ready, errors = service._build_preview(db, batch, [], service.get_config(db, book.id), {})

    assert errors == 1 and ready == 0
    assert any("与发票金额" in e for e in preview[0]["errors"]), preview[0]["errors"]


@pytest.mark.parametrize("total", ["19.03", "31.32", "100.00"])
def test_任何金额下都平(db, book, subjects, total):
    """恒等式不是「大多数情况成立」，是每一种都成立。"""
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })
    lines = [_line("油", "5.27", "0.68"), _line("米", "11.58", "1.50"), _line("面", "10.88", "1.41")]
    base = sum((Decimal(l["amount"]) for l in lines), Decimal("0"))
    tax = sum((Decimal(l["tax"]) for l in lines), Decimal("0"))
    batch = _record(db, book, lines=lines, base=str(base), tax=str(tax),
                       total=str(base + tax))
    preview, ready, errors = service._build_preview(db, batch, [], service.get_config(db, book.id), {})
    entries = _entries(preview[0])
    assert _dr(entries) == _cr(entries), "借贷必须相等"
