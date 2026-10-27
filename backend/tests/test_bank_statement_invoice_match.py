"""发票的结算科目：**人工指定 > 对账单识别 > 自动制证配置**。

发票上不写「这笔钱是从哪个账户收/付的」—— 那信息只在银行对账单上。
所以用户上传了对账单之后，这张发票的结算科目就该自动跟过去，
不用每张发票手工选一次。

**命中不到就回落，绝不猜。** 猜出来的结算科目会让凭证上的银行账户是错的，
而那种错要等到对不上账时才发现。

这一组用例盯的是三条：

1. **唯一命中** → 切到那个账户的科目，``settlement_source=STATEMENT``；
2. **命中不到 / 命中不唯一 / 已被占用** → 回落配置科目并说清为什么；
3. **进项找支出、销项找收入** —— 方向反了会把钱记到反方向的账户上。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.invoice import service as invoice_service
from app.models import BankStatementRow, InvoiceBatch, InvoiceRecord, Subject

BUYER_TAX_ID = "91420100MA1234567X"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def invoice_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path / "invoices"))
    return tmp_path / "invoices"


def _csv(rows, account_no="6222****0001", account_name="测试账户"):
    """造一份 CSV 对账单。

    用合成数据而不是真实样例，是因为这里要的是**精确的金额**——
    发票的价税合计要跟某一笔流水**分毫不差**才能命中。
    """
    lines = [
        "# 某某银行对账单",
        f"# 账号：{account_no}",
        f"# 户名：{account_name}",
        "# 起始日期：20260901",
        "# 结束日期：20260930",
        "交易日期,交易时间,收入,支出,余额,交易类型,交易备注",
    ]
    balance = Decimal("0")
    for day, income, expense, summary in rows:
        amount = Decimal(income) if income else Decimal(expense)
        balance = balance + (amount if income else -amount)
        lines.append(
            f"2026-09-{day:02d},10:00:00,{income or '0.0'},"
            f"{expense or '0.0'},{balance},行内转账,{summary}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cash(db, book, code="100201", name="基本户"):
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    parent.is_leaf = False
    db.flush()
    sub = Subject(book_id=book.id, year=book.start_year, code=code, name=name,
                  parent_id=parent.id, level=parent.level + 1, category="ASSET",
                  direction="DEBIT", is_leaf=True, carry_forward=False,
                  sort_no=1, active=True)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _year_2027(db, book):
    """给账套装上 2027 年度：年度登记 + 12 个期间 + 一份自己的科目表。

    科目表**按编码复制**（``copy_chart``）—— 与年度结转铺新年度科目表
    同一套做法：编码是唯一稳定的对应关系，新行是新 id。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart, ensure_book_year

    ensure_book_year(db, book, 2027)
    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()


def _cash_of_year(db, book, year, code="100201", name="基本户"):
    """在指定年度的科目表下挂一个明细科目。"""
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == year,
        Subject.code == "1002"))
    parent.is_leaf = False
    db.flush()
    sub = Subject(book_id=book.id, year=year, code=code, name=name,
                  parent_id=parent.id, level=parent.level + 1, category="ASSET",
                  direction="DEBIT", is_leaf=True, carry_forward=False,
                  sort_no=1, active=True)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _config(db, book, subjects):
    return invoice_service.save_config(db, book.id, {
        "taxpayer_id": BUYER_TAX_ID,
        "output_tax_subject_id": subjects["22210106"].id,
        # 回落到 **1012 其他货币资金**（它本身就是末级）。
        # 不用 1002：测试里给它挂了子科目，它就不再是末级科目了。
        "input_credit_subject_id": subjects["1012"].id,
        "output_debit_subject_id": subjects["1012"].id,
        # 项目名「办公用品」没配规则，落到兜底科目 —— 不然这一张会因为
        # 「没有命中科目规则」整张作废，测的就不是结算科目了。
        "input_fallback_subject_id": subjects["560202"].id,
        "output_fallback_subject_id": subjects["560202"].id,
    })


def _record(db, book, *, total, base, tax, invoice_type, invoice_no,
            seller="某供应商", issue_date="2026-10-01"):
    batch = InvoiceBatch(book_id=book.id, filename="造出来的.pdf", file_path="",
                          status="PENDING", total=1, created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=0,
        invoice_no=invoice_no, invoice_type=invoice_type, status="PENDING",
        payload={
            "meta": {
                "invoice_no": invoice_no, "issue_date": issue_date,
                "seller_name": seller, "seller_tax_id": "91310000MA0000000X",
                "buyer_name": "测试公司", "buyer_tax_id": BUYER_TAX_ID,
                "total_excl_tax": base, "total_tax": tax, "total_incl_tax": total,
            },
            "lines": [{"name": "办公用品", "display_name": "办公用品",
                       "amount": base, "tax": tax, "tax_rate": "6%"}],
        },
    )
    db.add(record)
    db.flush()
    return batch, record


def _upload(db, book, admin_user, cash, rows, **kwargs):
    data = _csv(rows, **kwargs)
    return service.upload_statement(db, book.id, admin_user, "对账单.csv",
                                    data, cash.id)


# ----------------------------------------------------------------------
# 唯一命中
# ----------------------------------------------------------------------

def test_唯一命中_切到那个账户的科目(db, book, admin_user, subjects, store,
                                     invoice_store):
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "100.00", None, "货款")])
    _config(db, book, subjects)
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000001")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    assert errors == 0, previews[0]["errors"]
    resolution = previews[0]["resolution"]
    assert resolution["settlement_source"] == "STATEMENT", resolution
    assert resolution["settlement_note"], "命中了却没说命中了哪一条"
    # **销项的结算科目在借方**（借 银行存款 / 贷 收入 + 税）。
    # 这跟会计上的「贷方=收款」直觉相反，但这里记的是**银行账户**：
    # 钱进账记借方，银行的「贷方发生额」才是收入（见 bankstatement 的说明）。
    debit = [e for e in resolution["entries"] if e["direction"] == "DEBIT"]
    assert any(e["subject_id"] == cash.id for e in debit), resolution["entries"]


def test_没命中_回落配置科目(db, book, admin_user, subjects, store, invoice_store):
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "999.00", None, "别的钱")])
    _config(db, book, subjects)
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000002")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    resolution = previews[0]["resolution"]
    assert resolution["settlement_source"] == "CONFIG", resolution
    debit = [e for e in resolution["entries"] if e["direction"] == "DEBIT"]
    assert any(e["subject_id"] == subjects["1012"].id for e in debit), (
        "没命中时没有回落到配置科目")


def test_人工指定优先于对账单识别(db, book, admin_user, subjects, store,
                                  invoice_store):
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "100.00", None, "货款")])
    _config(db, book, subjects)
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000003")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id),
        {str(record.id): {"settlement_subject_id": subjects["560202"].id}})
    resolution = previews[0]["resolution"]
    assert resolution["settlement_source"] == "MANUAL", resolution
    debit = [e for e in resolution["entries"] if e["direction"] == "DEBIT"]
    assert any(e["subject_id"] == subjects["560202"].id for e in debit), (
        "人工指定的科目没有被采用")


# ----------------------------------------------------------------------
# 跨年度
# ----------------------------------------------------------------------

def test_跨年度_按编码把结算科目搬到开票日那一年(db, book, admin_user, subjects,
                                             store, invoice_store):
    """发票开在 2027 年，结算科目是从 2026 年的对账单上认出来的。

    科目表每个年度各有一份，两年的 ``100201`` 根本不是同一行 ——
    按**编码**搬才是对的。搬不动就报出来，绝不静默记到别的年度上。
    """
    _year_2027(db, book)
    cash_2027 = _cash_of_year(db, book, 2027)
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "100.00", None, "货款")])
    _config(db, book, subjects)
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000005",
                             issue_date="2027-01-15")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    resolution = previews[0]["resolution"]
    assert resolution["settlement_source"] == "STATEMENT", resolution
    debit = [e for e in resolution["entries"] if e["direction"] == "DEBIT"]
    assert any(e["subject_id"] == cash_2027.id for e in debit), (
        f"结算科目没有搬到 2027 年：{resolution['entries']}")


def test_跨年度_新年度没有这个科目时报出来(db, book, admin_user, subjects,
                                       store, invoice_store):
    """2027 年的科目表里没有这个编码 —— 必须说清楚，不能记到别处去。"""
    _year_2027(db, book)
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "100.00", None, "货款")])
    _config(db, book, subjects)
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000006",
                             issue_date="2027-01-15")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    problems = previews[0].get("errors") or []
    assert any("2027 年度的科目表里不存在" in text for text in problems), previews[0]


# ----------------------------------------------------------------------
# 歧义与占用
# ----------------------------------------------------------------------

def test_多账户同额_回落并给出提示(db, book, admin_user, subjects, store,
                                   invoice_store):
    """两个账户各有一笔同额的钱 —— 到底从哪个账户收的，机器不知道。"""
    cash_a = _cash(db, book, "100201", "基本户")
    cash_b = _cash(db, book, "100202", "一般户")
    _upload(db, book, admin_user, cash_a, [(1, "100.00", None, "货款")])
    _upload(db, book, admin_user, cash_b, [(2, "100.00", None, "货款")])
    _config(db, book, subjects)
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000004")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    resolution = previews[0]["resolution"]
    assert resolution["settlement_source"] == "CONFIG", resolution
    assert "多账户同额" in resolution["settlement_note"], resolution


def test_流水已被别的发票占用_这一张回落(db, book, admin_user, subjects, store,
                                          invoice_store):
    """一行流水只被一张发票占用 —— 同一笔钱不可能结两张发票的账。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash, [(1, "100.00", None, "货款")])
    row = db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement["id"])).first()
    _config(db, book, subjects)
    # 先造一张「已经占用了这条流水」的发票（另一个批次）
    other_batch, other_record = _record(
        db, book, total="100.00", base="100.00", tax="0.00",
        invoice_type="OUTPUT", invoice_no="24417000000000000005")
    row.matched_invoice_record_id = other_record.id
    db.commit()
    batch, record = _record(db, book, total="100.00", base="100.00", tax="0.00",
                             invoice_type="OUTPUT", invoice_no="24417000000000000015")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    resolution = previews[0]["resolution"]
    assert resolution["settlement_source"] == "CONFIG", resolution


def test_同批次里先到先得(db, book, admin_user, subjects, store, invoice_store):
    """两张同额发票：第一张占走流水，第二张回落。"""
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "100.00", None, "货款")])
    _config(db, book, subjects)
    batch, _r1 = _record(db, book, total="100.00", base="100.00", tax="0.00",
                          invoice_type="OUTPUT", invoice_no="24417000000000000006")
    second = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=1,
        invoice_no="24417000000000000007", invoice_type="OUTPUT", status="PENDING",
        payload={
            "meta": {
                "invoice_no": "24417000000000000007", "issue_date": "2026-10-01",
                "seller_name": "某客户", "seller_tax_id": "91310000MA0000000X",
                "buyer_name": "测试公司", "buyer_tax_id": BUYER_TAX_ID,
                "total_excl_tax": "100.00", "total_tax": "0.00",
                "total_incl_tax": "100.00",
            },
            "lines": [{"name": "办公用品", "display_name": "办公用品",
                       "amount": "100.00", "tax": "0.00", "tax_rate": "6%"}],
        },
    )
    db.add(second)
    db.flush()
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    sources = [p["resolution"]["settlement_source"] for p in previews]
    assert "STATEMENT" in sources, sources
    assert sources.count("STATEMENT") == 1, (
        f"一行流水被两张发票同时占用了：{sources}")


# ----------------------------------------------------------------------
# 方向不能反
# ----------------------------------------------------------------------

def test_销项找收入_进项找出账(db, book, admin_user, subjects, store, invoice_store):
    """销项（收钱）要命中**收入**行；进项（付钱）要命中**支出**行。

    方向反了，钱就会记到反方向的账户上 —— 而这种错在总额上可能看不出来。
    """
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash,
            [(1, "500.00", None, "收款"), (2, None, "300.00", "付款")])
    _config(db, book, subjects)

    # 销项 500 → 命中那笔收入
    batch, _r = _record(db, book, total="500.00", base="500.00", tax="0.00",
                        invoice_type="OUTPUT", invoice_no="24417000000000000008")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    assert previews[0]["resolution"]["settlement_source"] == "STATEMENT", (
        "销项没命中那笔收入")

    # 进项 300 → 命中那笔支出
    batch2, _r2 = _record(db, book, total="300.00", base="300.00", tax="0.00",
                           invoice_type="INPUT", invoice_no="24417000000000000009")
    previews2, ready2, errors2 = invoice_service._build_preview(
        db, batch2, [], invoice_service.get_config(db, book.id), {})
    assert previews2[0]["resolution"]["settlement_source"] == "STATEMENT", (
        "进项没命中那笔支出")


def test_进项不会误配到收入行(db, book, admin_user, subjects, store, invoice_store):
    """只有一笔**收入** 500 时，一张 500 的**进项**发票不该命中它。"""
    cash = _cash(db, book)
    _upload(db, book, admin_user, cash, [(1, "500.00", None, "收款")])
    _config(db, book, subjects)
    batch, _r = _record(db, book, total="500.00", base="500.00", tax="0.00",
                        invoice_type="INPUT", invoice_no="24417000000000000010")
    previews, ready, errors = invoice_service._build_preview(
        db, batch, [], invoice_service.get_config(db, book.id), {})
    assert previews[0]["resolution"]["settlement_source"] == "CONFIG", (
        "进项发票匹配到了收入流水 —— 方向反了")
