"""净额抵成 0 的发票明细行：**不生成分录**（用户 2026-10-24 明确的口径）。

发票上常见「整额折扣」：一行商品原价 2.04，紧跟着一行 -2.04 的折扣行，
净额正好抵成 0（超市的买一送一、满减就是这么印的）。解析器把折扣行并进
商品行，于是那行的金额就是 0 —— 见 ``test_invoice_amount_precision`` 里
那组「折扣行合并」的用例。

这一行**不该**变成一条 0 金额的分录：

* 数据库的 ``ck_entry_amount_nonzero`` 会拦下来。拦下来的后果是
  「确认生成」时**整批回滚** —— 几十张本来没问题的凭证陪着一张有问题的
  票一起白干（生产上就是这么炸的：六月_2 批次 61 张卡在一张票上）。
* 就算存进去，凭证上凭空多一行 0，报表上多一行没有金额的格子，
  还查不出是哪张票干的。

所以口径是「**项目金额零就不产生分录**」。但恒等式「各项目之和 = 发票
金额」不能破：0 加进任何和里都不变，剔掉它不影响合计。唯一要小心的是
**税额并入第一条明细行** —— 「第一条」得在剔掉净零行**之后**数，否则
税额会落在一个根本不会成行的 0 元行上，那张票的借方永远少一笔。
"""
from __future__ import annotations

from decimal import Decimal

from app.invoice import service


def _d(value: str) -> Decimal:
    return Decimal(value)


def _items(*rows: tuple[int, str, str]) -> list[dict]:
    return [{"subject_id": subject, "amount": _d(amount), "summary": summary}
            for subject, amount, summary in rows]


def _sum(entries: list[dict], direction: str) -> Decimal:
    return sum((_d(e["amount"]) for e in entries if e["direction"] == direction), Decimal("0"))


def _balances(entries: list[dict]) -> bool:
    return _sum(entries, "DEBIT") == _sum(entries, "CREDIT")


# --------------------------------------------------------------- 纯函数层


def test_净零的项目不产生分录():
    """用户口径：项目金额为 0 就不产生分录。"""
    entries = service._input_entries(
        items=_items((1, "18.61", "*水果*黄金蜜露"), (1, "0.00", "*软饮料*可口可乐330ml"),
                     (1, "10.00", "*水果*台农芒果")),
        tax=_d("3.00"),
        settlement_subject_id=100,
        total=_d("31.61"),
        summary="发票 XX",
    )
    assert all(_d(e["amount"]) != 0 for e in entries), "不该留下 0 金额的分录"
    assert [e["summary"] for e in entries if e["direction"] == "DEBIT"] == [
        "*水果*黄金蜜露", "*水果*台农芒果"], "净零的那项不该占一行"
    assert _balances(entries)


def test_净零项排在第一条时税额落到第一条非零行上():
    """并了税额的那一行得是**真正会成行**的那行。

    税额并的是第一条明细行；净零行不成行，税额要是并到它身上，
    这张票的借方就永远少一笔，凭证直接不成立。
    """
    entries = service._input_entries(
        items=_items((1, "0.00", "*软饮料*可口可乐330ml"), (1, "28.61", "*水果*黄金蜜露")),
        tax=_d("3.00"),
        settlement_subject_id=100,
        total=_d("31.61"),
        summary="发票 XX",
    )
    debits = [e for e in entries if e["direction"] == "DEBIT"]
    assert len(debits) == 1, f"只有一条借方，实际 {debits}"
    assert debits[0]["summary"] == "*水果*黄金蜜露", "并了税额的仍是发票上的项目名"
    assert debits[0]["amount"] == "31.61"
    assert _balances(entries)


def test_销项的净零项也不产生分录():
    entries = service._output_entries(
        items=_items((1, "1000.00", "咨询服务"), (1, "0.00", "赠品"), (1, "500.00", "培训服务")),
        tax=_d("60.00"),
        tax_subject_id=2221,
        settlement_subject_id=100,
        total=_d("1560.00"),
        summary="发票 XX",
    )
    assert all(_d(e["amount"]) != 0 for e in entries)
    assert [e["summary"] for e in entries if e["direction"] == "CREDIT"] == [
        "咨询服务", "培训服务", "发票 XX（销项税额）"]
    assert _balances(entries)


def test_全部项目都抵成零时只剩结算那一行():
    """极端情况：所有明细都被折扣抵成 0。借方一条都不成行。"""
    entries = service._input_entries(
        items=_items((1, "0.00", "甲"), (1, "0.00", "乙")),
        tax=_d("0.00"),
        settlement_subject_id=100,
        total=_d("0.00"),
        summary="发票 XX",
    )
    assert [e["direction"] for e in entries] == ["CREDIT"]


# --------------------------------------------------------------- 服务层


def _record(db, book, *, lines, base, tax, total, invoice_no="24417000000044444444"):
    from app.models import InvoiceBatch, InvoiceRecord

    batch = InvoiceBatch(book_id=book.id, filename="折扣.pdf", file_path="",
                          status="PENDING", total=1, created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=0,
        invoice_no=invoice_no, invoice_type="INPUT", status="PENDING",
        payload={
            "meta": {"invoice_no": invoice_no, "issue_date": "2026-10-01",
                     "seller_name": "某供应商", "seller_tax_id": "91310000MA2AYXC79Y",
                     "buyer_name": "测试公司", "buyer_tax_id": "91420100MA1234567X",
                     "total_excl_tax": base, "total_tax": tax, "total_incl_tax": total},
            "lines": lines,
        },
    )
    db.add(record)
    db.flush()
    return batch, record


def _line(name, amount, tax="0.00"):
    return {"name": name, "display_name": name, "amount": amount, "tax": tax}


def _config(db, book, subjects):
    service.save_config(db, book.id, {
        "taxpayer_id": "91420100MA1234567X",
        "input_credit_subject_id": subjects["1002"].id,
        "input_fallback_subject_id": subjects["560201"].id,
    })


def test_预览里净零项不成行且标出来(db, book, subjects):
    _config(db, book, subjects)
    batch, _ = _record(db, book,
                        lines=[_line("*水果*黄金蜜露", "18.61", "1.67"),
                               _line("*软饮料*可口可乐330ml", "0.00", "0.00"),
                               _line("*水果*台农芒果", "10.00", "0.99")],
                        base="28.61", tax="2.66", total="31.27")
    preview, ready, errors = service._build_preview(
        db, batch, [], service.get_config(db, book.id), {})

    assert errors == 0, preview[0]["errors"]
    entries = preview[0]["resolution"]["entries"]
    assert all(_d(e["amount"]) != 0 for e in entries), entries
    assert _sum(entries, "DEBIT") == _sum(entries, "CREDIT") == Decimal("31.27")
    # 净零的那项要**看得见**：用户在明细表里看得到它被抵掉了，
    # 但界面上要告诉他它没进凭证（否则会以为漏记了）。
    assert preview[0]["resolution"]["zero_lines"] == [1], preview[0]["resolution"]["zero_lines"]
    # 明细表里的金额照旧显示发票上印的那个数。
    assert preview[0]["resolution"]["line_amounts"]["1"] == "0.00"


def test_尾项对齐不落到净零行上(db, book, subjects):
    """差的那一分要落到**最后一个非零行**，不是最后一行。

    最后一行是净零行时，差值落到它身上会让它变成 0.01 —— 一个被整额
    折扣抵掉的商品，凭空多出一分钱；而且它还会因此多出一条分录。
    """
    _config(db, book, subjects)
    batch, _ = _record(db, book,
                        lines=[_line("油", "10.00"), _line("可乐", "0.00")],
                        base="10.01", tax="0.00", total="10.01")
    preview, ready, errors = service._build_preview(
        db, batch, [], service.get_config(db, book.id), {})

    assert errors == 0, preview[0]["errors"]
    resolution = preview[0]["resolution"]
    assert resolution["line_amounts"] == {"0": "10.01", "1": "0.00"}, resolution["line_amounts"]
    assert resolution["rounding"] == "0.01"
    entries = resolution["entries"]
    assert len(entries) == 2, f"一条借方一条贷方，实际 {entries}"
    assert _sum(entries, "DEBIT") == _sum(entries, "CREDIT") == Decimal("10.01")


def test_整张票抵成零时预览标红(db, book, subjects):
    """这种票生成不出凭证 —— 在预览页就该标红，别留到确认时整批回滚。"""
    _config(db, book, subjects)
    batch, _ = _record(db, book,
                        lines=[_line("甲", "0.00"), _line("乙", "0.00")],
                        base="0.00", tax="0.00", total="0.00")
    preview, ready, errors = service._build_preview(
        db, batch, [], service.get_config(db, book.id), {})

    assert errors == 1 and ready == 0
    assert any("金额为 0" in e for e in preview[0]["errors"]), preview[0]["errors"]


# --------------------------------------------------------------- 确认时兜底


def test_旧预览里的零金额分录在确认时被滤掉(db, book, subjects, admin_user):
    """批次可能是**上一版代码**算出来的：那一版还会把抵成 0 的行变成
    一条 0 元的分录，点「确认生成」就整批回滚。

    这里模拟那个状态（把一条 0 金额分录塞回 payload），确认时必须照样
    生成出凭证 —— 而不是把 61 张陪葬。
    """
    from sqlalchemy import select

    from app.models import Entry, InvoiceRecord, Voucher

    _config(db, book, subjects)
    batch, record = _record(db, book,
                            lines=[_line("*水果*黄金蜜露", "18.61", "1.67"),
                                   _line("*软饮料*可口可乐330ml", "0.00", "0.00")],
                            base="18.61", tax="1.67", total="20.28")
    service._build_preview(db, batch, [], service.get_config(db, book.id), {})

    # 把「上一版代码」的产物塞回去：净零项变成一条 0 元的分录。
    payload = dict(record.payload or {})
    resolution = dict(payload.get("resolution") or {})
    entries = list(resolution.get("entries") or [])
    entries.insert(1, {"subject_id": entries[0]["subject_id"], "direction": "DEBIT",
                       "amount": "0.00", "summary": "*软饮料*可口可乐330ml"})
    resolution["entries"] = entries
    payload["resolution"] = resolution
    record.payload = payload
    db.flush()

    result = service.commit_batch(db, batch, admin_user)
    assert result["created"] == 1, result
    voucher = db.scalars(select(Voucher).where(Voucher.id == record.voucher_id)).one()
    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert all(Decimal(row.amount) != 0 for row in rows), "0 金额的分录不许入库"
    assert sum(Decimal(row.amount) for row in rows if row.direction == "DEBIT") == Decimal("20.28")
    assert sum(Decimal(row.amount) for row in rows if row.direction == "CREDIT") == Decimal("20.28")


# ------------------------------------------- 回归：生产上炸过的那张真发票


# 一张超市发票的文字层（购销方名称与税号已脱敏，明细与合计原样）。
#
# 这是 2026-10-24 生产上真实炸过的那张票：六月_2 批次 61 张凭证卡在它
# 身上，点「确认生成」报「凭证金额不能是 0：第 5 行（借方）」，整批回滚。
#
# 票上「*软饮料*可口可乐330ml」原价 2.04，紧跟一行 -2.04 的**整额折扣**，
# 净额正好抵成 0 —— 就是它。
REAL_PAGE_TEXT = """合 计
价税合计（大写） （小写）
备
注
开票人：
电子发票（普通发票） 发票号码：
开票日期：
购
买
方
信
息
统一社会信用代码/纳税人识别号：
销
售
方
信
息
统一社会信用代码/纳税人识别号：
名称： 名称：
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
24417000000055555555
2026年10月01日
测试购买方有限公司
91420100MA1234567X
测试销售方有限公司
91310000MA2AYXC79Y
¥179.65 ¥17.59
壹佰玖拾柒圆贰角肆分 ¥197.24
测试开票人
*水果*黄金蜜露 kg 2.064 9.1521317829457 18.89 9% 1.70
*水果*黄金蜜露 -0.28 9% -0.03
*水果*洛川红富士85# kg 2.77 18.158844765343 50.30 9% 4.53
*水果*洛川红富士85# -0.75 9% -0.07
*水果*夏橙 kg 2.318 7.3166522864538 16.96 9% 1.53
*水果*夏橙 -1.94 9% -0.17
*熟肉制品*得瑞市鲜猪肉
地道肠（原味）175g
包 1 14.87 14.87 13% 1.93
*熟肉制品*得瑞市鲜猪肉
地道肠（原味）175g
-1.88 13% -0.24
*软饮料*可口可乐330ml 瓶 1 2.04 2.04 13% 0.26
*软饮料*可口可乐330ml -2.04 13% -0.26
*焙烤食品*沙拉面包210g 包 1 8.76 8.76 13% 1.14
*焙烤食品*沙拉面包210g -0.13 13% -0.02
*焙烤食品*旺旺雪饼原味
84g
包 1 4.87 4.87 13% 0.63
*焙烤食品*旺旺雪饼原味
84g
-0.95 13% -0.12
*焙烤食品*牛奶软蛋糕 份 1 8.76 8.76 13% 1.14
*焙烤食品*牛奶软蛋糕 -0.13 13% -0.02
*水果*台农芒果 kg 1.216 9.1529605263158 11.13 9% 1.00
*水果*台农芒果 -0.17 9% -0.01
*水果*春蜜脆桃 kg 1.296 10.2469135802469 13.28 9% 1.20
*水果*春蜜脆桃 -1.60 9% -0.14
*水果*佳农香蕉 kg 4.322 10.2545118000926 44.32 9% 3.99
*水果*佳农香蕉 -5.35 9% -0.48
*软饮料*得瑞市天然弱碱
性矿泉水350ml
瓶 1 0.88 0.88 13% 0.12
*软饮料*得瑞市天然弱碱
性矿泉水350ml
-0.19 13% -0.02
"""


def test_真发票的折扣行被解析成净额(db, book, subjects):
    """先确认解析层：整额折扣抵成 0 的那行，金额确实是 0。"""
    from app.invoice.text_parser import parse_page

    invoice = parse_page(0, REAL_PAGE_TEXT)
    zero = [line for line in invoice.lines if line.amount == 0]
    assert len(zero) == 1, [line.name for line in invoice.lines]
    assert zero[0].name == "*软饮料*可口可乐330ml"
    assert invoice.meta.total_excl_tax == Decimal("179.65")
    assert invoice.meta.total_incl_tax == Decimal("197.24")


def test_真发票生成的分录没有零金额且借贷相等(db, book, subjects):
    """这张票以前会把整批 61 张凭证拖下水，现在自己走通了。"""
    from app.invoice.text_parser import parse_page

    _config(db, book, subjects)
    invoice = parse_page(0, REAL_PAGE_TEXT)
    payload = invoice.to_dict()
    batch, _ = _record(db, book,
                        lines=payload["lines"],
                        base=payload["meta"]["total_excl_tax"],
                        tax=payload["meta"]["total_tax"],
                        total=payload["meta"]["total_incl_tax"],
                        invoice_no=payload["meta"]["invoice_no"])
    preview, ready, errors = service._build_preview(
        db, batch, [], service.get_config(db, book.id), {})

    assert errors == 0, preview[0]["errors"]
    entries = preview[0]["resolution"]["entries"]
    assert all(_d(e["amount"]) != 0 for e in entries), "不该有 0 金额的分录"
    assert _sum(entries, "DEBIT") == _sum(entries, "CREDIT") == Decimal("197.24")
    assert preview[0]["resolution"]["zero_lines"] == [4]
    # 净零的那项在明细表里照旧看得见（金额 0.00），只是不进凭证。
    assert preview[0]["resolution"]["line_amounts"]["4"] == "0.00"
