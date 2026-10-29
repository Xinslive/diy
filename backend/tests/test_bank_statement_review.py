"""流水对账的**配对**：谁跟谁是一笔 —— 2026-10-29 用户报的那几个问题。

**用户报的三条**：

1. 一笔支出，账上有、对账单也有，但**日期差 10 天**（账上记的是发票日期，
   对账单是实际支付日期），系统没判成「已入账」；
2. 对账单上**四笔相同金额**的支出，账上只记了一笔，四条流水**都显示已入账**；
3. 「你在修这些问题的时候也要看看这个模块还有什么我没遇到的问题」。

这一份把上面这些钉成用例，顺带钉住**两个页面必须用同一把尺子**这件事
（流水明细页与存疑凭证页）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.bankstatement import suspicious
from app.core.config import settings
from app.models import BankStatementRow, Subject

D = Decimal


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


def _csv(rows, *, start="20260901", end="20260930"):
    """造一份 CSV 对账单。

    ``rows`` 每一项是 ``(日期, 收入, 支出, 摘要)`` —— 日期写全（``2026-09-28``），
    因为这些用例要精确控制「差几天」。
    """
    lines = [
        "# 某某银行对账单",
        "# 账号：6222****0001",
        "# 户名：测试账户",
        f"# 起始日期：{start}",
        f"# 结束日期：{end}",
        "交易日期,交易时间,收入,支出,余额,交易类型,交易备注",
    ]
    balance = D("0")
    for day, income, expense, summary in rows:
        amount = D(income) if income else D(expense)
        balance = balance + (amount if income else -amount)
        lines.append(
            f"{day},10:00:00,{income or '0.0'},"
            f"{expense or '0.0'},{balance},行内转账,{summary}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cash(db, book, code="100201", name="基本户", year=None):
    year = year if year is not None else book.start_year
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == year, Subject.code == "1002"))
    assert parent is not None, f"{year} 年度的科目表里没有 1002"
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


def _year_2027(db, book):
    """给账套装上 2027 年度：年度登记 + 12 个期间 + 一份自己的科目表。"""
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart, ensure_book_year

    ensure_book_year(db, book, 2027)
    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()


def _upload(db, book, admin_user, cash, rows, **kwargs):
    data = _csv(rows, **kwargs)
    return service.upload_statement(db, book.id, admin_user, "对账单.csv",
                                    data, cash.id)


def _prepare(db, book, statement, row, subjects, direction="IN"):
    """把一行弄成「可以制证」的状态：定方向 + 定对方科目。"""
    service.set_row_direction(db, book.id, row["id"], direction)
    service.update_row(db, book.id, row["id"], {
        "plan_subject_id": subjects["560202"].id})
    return service.list_rows(db, book.id, statement["id"])


def _by_id(plan):
    out = {}
    for bucket in ("candidates", "skipped", "review"):
        for item in plan.get(bucket) or []:
            out[item["id"]] = item
    return out


# ----------------------------------------------------------------------
# 问题一：日期差 10 天 → 待复核，而不是「没入账」
# ----------------------------------------------------------------------

def test_日期差十天_判为待复核而不是没入账(db, book, admin_user, subjects, store):
    """**用户报的第一条。**

    账上记的是**发票日期**，对账单是**实际支付日期**，差 10 天。
    金额对得上、时间对不上 —— 不许自动判「已入账」（那是另一笔钱），
    也不许当成「没入账」（那会让「生成流水凭证」再记一遍）。
    """
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)

    # 账上那一笔记在**发票日**（比实际支付早 10 天）
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])

    plan = service.plan_vouchers(db, book.id, statement["id"])
    row = _by_id(plan)[rows[0]["id"]]
    assert row["voucher_state"] == "REVIEW", row
    assert row["id"] not in [c["id"] for c in plan["candidates"]], (
        "待复核的行进了可制证清单 —— 点一下就会把同一笔钱记两遍")
    assert plan["review_count"] == 1, plan


def test_待复核的行不生成凭证(db, book, admin_user, subjects, store):
    """**同一笔钱记两遍**是这一档存在的全部理由。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 0, (
        "★ 账上已经有同一笔钱（只差 10 天），却又生成了一张凭证 —— 钱记了两次")


def test_待复核时界面上能看到那张凭证(db, book, admin_user, subjects, store):
    """只说「有笔对不上」用户不知道该去看哪张凭证 —— 要把它摆出来。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])
    service.plan_vouchers(db, book.id, statement["id"])

    listed = service.list_rows(db, book.id, statement["id"])
    row = listed[0]
    assert row["voucher_state"] == "REVIEW", row
    candidate = row["review_candidate"]
    assert candidate, "待复核的行没有给出候选凭证"
    assert candidate["days_diff"] == 10, candidate
    assert candidate["amount"] == "872100.00", candidate


def test_判是同一笔_挂上那张凭证(db, book, admin_user, subjects, store):
    """用户判「是同一笔」→ 这一行变成「已入账」，指着那张凭证。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])
    plan = service.plan_vouchers(db, book.id, statement["id"])
    candidate = plan["review"][0]["review_candidate"]

    result = service.confirm_row_review(db, book.id, rows[0]["id"],
                                        candidate["voucher_id"], admin_user)
    assert result["voucher_state"] == "POSTED", result
    row = db.get(BankStatementRow, rows[0]["id"])
    assert row.status == "POSTED"
    assert row.matched_voucher_id == candidate["voucher_id"]


def test_判不是同一笔_回到可制证且不再反复报(db, book, admin_user, subjects, store):
    """用户判「不是同一笔」→ 放行去制证；**而且下次重算不许又报一遍**。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])
    plan = service.plan_vouchers(db, book.id, statement["id"])
    candidate = plan["review"][0]["review_candidate"]

    service.dismiss_row_review(db, book.id, rows[0]["id"], candidate["voucher_id"])
    row = db.get(BankStatementRow, rows[0]["id"])
    assert row.status == "OPEN", row.status
    assert row.rejected_voucher_id == candidate["voucher_id"]

    # **再算一次**：不该又把它报出来（判了等于没判是最气人的那种 bug）。
    again = service.plan_vouchers(db, book.id, statement["id"])
    assert again["review_count"] == 0, again
    assert rows[0]["id"] in [c["id"] for c in again["candidates"]], (
        "判过「不是同一笔」之后，这一行该回到可制证清单里")


def test_判不是同一笔_钱仍然只记一次(db, book, admin_user, subjects, store):
    """放行之后正常制证 —— 账上那笔是**另一笔**钱，该记还是要记。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "另一笔")],
                 cr=[(subjects["560301"].id, D("872100.00"), "另一笔")])
    plan = service.plan_vouchers(db, book.id, statement["id"])
    candidate = plan["review"][0]["review_candidate"]
    service.dismiss_row_review(db, book.id, rows[0]["id"], candidate["voucher_id"])

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, result


def test_差得太远_不报待复核_就是普通的候选(db, book, admin_user, subjects, store):
    """差 45 天 —— 那是**另一笔钱**，不该报出来让人复核。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 8, 14),
                 dr=[(cash.id, D("872100.00"), "另一笔")],
                 cr=[(subjects["560301"].id, D("872100.00"), "另一笔")])

    plan = service.plan_vouchers(db, book.id, statement["id"])
    assert plan["review_count"] == 0, plan
    assert rows[0]["id"] in [c["id"] for c in plan["candidates"]], (
        "差 45 天的同额凭证被当成了同一笔 —— 那会让真正该记的第二笔进不了账")


def test_确认待复核时凭证对不上要拦住(db, book, admin_user, subjects, store):
    """挂一张根本不是这笔钱的凭证上去 —— 必须拦。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])
    other = make_voucher(date(2026, 9, 19),
                         dr=[(cash.id, D("1.00"), "无关的一笔")],
                         cr=[(subjects["560301"].id, D("1.00"), "无关的一笔")])
    service.plan_vouchers(db, book.id, statement["id"])

    with pytest.raises(ValueError) as excinfo:
        service.confirm_row_review(db, book.id, rows[0]["id"], other.id, admin_user)
    assert "对不上" in str(excinfo.value)


# ----------------------------------------------------------------------
# 问题二：四笔同额、账上一笔 → 只有一行「已入账」
# ----------------------------------------------------------------------

def test_四笔同额账上一笔_只有一行已入账(db, book, admin_user, subjects, store):
    """**用户报的第二条。**

    账上只有一笔，界面上却有四条显示「已入账」—— 另外三笔是**账上根本没
    记**的钱，被「已入账」挡住就永远进不了账。
    """
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash, [
        ("2026-09-20", None, "500.00", "第一笔"),
        ("2026-09-20", None, "500.00", "第二笔"),
        ("2026-09-20", None, "500.00", "第三笔"),
        ("2026-09-20", None, "500.00", "第四笔"),
    ])
    rows = service.list_rows(db, book.id, statement["id"])
    for row in rows:
        _prepare(db, book, statement, row, subjects, direction="OUT")
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 20),
                 dr=[(subjects["560202"].id, D("500.00"), "只有这一笔")],
                 cr=[(cash.id, D("500.00"), "只有这一笔")])

    plan = service.plan_vouchers(db, book.id, statement["id"])
    posted = [r for r in plan["skipped"] if "已入账" in r.get("reason", "")]
    assert len(posted) == 1, (
        f"★ 账上只有 1 笔，界面上却有 {len(posted)} 条显示「已入账」—— "
        "其中 3 条是假的")
    assert len(plan["candidates"]) == 3, (
        "另外三行是账上没记的钱 —— 它们得留在可制证清单里")


def test_四笔同额账上一笔_余额核对说得通(db, book, admin_user, subjects, store):
    """被误标成「已入账」的三行会从「未入账流水净额」里消失 —— 差额就说不通了。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash, [
        ("2026-09-20", None, "500.00", "第一笔"),
        ("2026-09-20", None, "500.00", "第二笔"),
        ("2026-09-20", None, "500.00", "第三笔"),
        ("2026-09-20", None, "500.00", "第四笔"),
    ])
    rows = service.list_rows(db, book.id, statement["id"])
    for row in rows:
        _prepare(db, book, statement, row, subjects, direction="OUT")
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 20),
                 dr=[(subjects["560202"].id, D("500.00"), "只有这一笔")],
                 cr=[(cash.id, D("500.00"), "只有这一笔")])
    # **先算一次可制证清单** —— 「已入账」的标记是那时候落库的。
    service.plan_vouchers(db, book.id, statement["id"])
    # 银行那边四笔都出去了 —— 期末余额比账面少 1500。
    st = service._statement(db, book.id, statement["id"])
    st.closing_balance = D("-2000.00")
    db.commit()

    result = service.reconcile_balance(db, book.id, statement["id"])
    assert result["unposted_count"] == 3, (
        f"★ 未入账的流水只剩 {result['unposted_count']} 条 —— "
        "另外三行被误标成「已入账」了")
    assert result["verdict"] == "EXPLAINED", result


# ----------------------------------------------------------------------
# 问题三（顺手挖出来的）：账上四笔、流水一笔 → 存疑页要报三笔
# ----------------------------------------------------------------------

def test_账上四笔流水一笔_存疑页报出三笔错账(db, book, admin_user, subjects, store):
    """**存疑凭证那一页的镜像 bug。**

    旧算法让四条分录都「对上了」那一行流水 —— 于是三笔**错账一笔都没报**。
    """
    cash = _cash(db, book)
    # **存疑页是按账套查的**，不按对账单查 —— 这一份对账单建出来就行，
    # 不用拿它的 id（存疑页要报的是「账上那几笔没对上行」）。
    _upload(db, book, admin_user, cash,
            [("2026-09-20", None, "500.00", "只有这一行")])
    make_voucher = _voucher_factory(db, book, admin_user)
    for i in range(4):
        make_voucher(date(2026, 9, 20),
                     dr=[(subjects["560202"].id, D("500.00"), f"第{i + 1}笔")],
                     cr=[(cash.id, D("500.00"), f"第{i + 1}笔")])

    result = suspicious.suspicious_vouchers(db, book.id)
    assert result["total_suspicious"] == 3, (
        f"★ 账上 4 笔只对上 1 笔，另外 3 笔是错账 —— 存疑页却只报了 "
        f"{result['total_suspicious']} 笔")


def test_待复核的那笔不进存疑清单(db, book, admin_user, subjects, store):
    """**两页必须用同一把尺子**：待复核的那笔在流水页报出来了，
    存疑页就不该再报一遍 —— 它有对应的流水行，不是错账。"""
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "872100.00", None, "杨育军")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 18),
                 dr=[(cash.id, D("872100.00"), "发票日付款")],
                 cr=[(subjects["560301"].id, D("872100.00"), "发票日付款")])
    service.plan_vouchers(db, book.id, statement["id"])

    result = suspicious.suspicious_vouchers(db, book.id)
    assert result["total_suspicious"] == 0, (
        "那笔钱在流水页已经是「待复核」了 —— 存疑页再报一遍就是两页打架")


# ----------------------------------------------------------------------
# 顺手挖出来的：跨年度搬过科目之后认不出自己那张凭证
# ----------------------------------------------------------------------

def test_跨年度搬过科目_仍然认得出那张凭证(db, book, admin_user, subjects, store):
    """科目表**每个年度各有一份** —— 2026 的 100201 与 2027 的 100201 是
    两行不同的记录、两个 id。按 id 比就认不出自己那张凭证：明明是同一个
    银行账户，却判成「没入账」，于是又生成一张凭证，**同一笔钱记两次**。
    """
    _year_2027(db, book)
    cash_2027 = _cash(db, book, year=2027)
    cash_2026 = _cash(db, book, year=2026)
    assert cash_2026.id != cash_2027.id, "两年的科目必须是两行不同的记录"

    statement = _upload(db, book, admin_user, cash_2026,
                        [("2026-12-30", "500.00", None, "跨年的那一笔")],
                        start="20261228", end="20270105")
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    # 账上那张凭证记在 **2027 年度的 100201** 上（跨年搬过科目）——
    # 对方科目也必须是 2027 年那一份（科目表按年度各有一份）。
    fee_2027 = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == "560301"))
    assert fee_2027 is not None, "2027 年度的科目表里没有 560301"
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2027, 1, 2),
                 dr=[(cash_2027.id, D("500.00"), "跨年记的")],
                 cr=[(fee_2027.id, D("500.00"), "跨年记的")])

    plan = service.plan_vouchers(db, book.id, statement["id"])
    row = _by_id(plan)[rows[0]["id"]]
    assert row["voucher_state"] == "POSTED", (
        "★ 跨年搬过科目之后认不出那张凭证了 —— 同一笔钱会被记两遍")


# ----------------------------------------------------------------------
# 顺手挖出来的：被发票占用的行不许被启发式指认
# ----------------------------------------------------------------------

def test_发票占用的行不被启发式标成已入账(db, book, admin_user, subjects, store):
    """发票还没提交时，账上那张凭证多半是**另一笔**碰巧同额的。

    旧算法会让它标成「已入账（凭证 #X）」而 X 根本不是这笔钱的凭证；
    更糟的是那一行被标成 ``POSTED`` 之后，发票再来认领它时会被拒
    （认领要求这一行还是 ``OPEN``）—— 用户的发票从此再也占不住这一行。
    """
    cash = _cash(db, book)
    statement = _upload(db, book, admin_user, cash,
                        [("2026-09-28", "500.00", None, "发票要占的那一行")])
    rows = service.list_rows(db, book.id, statement["id"])
    _prepare(db, book, statement, rows[0], subjects)
    make_voucher = _voucher_factory(db, book, admin_user)
    make_voucher(date(2026, 9, 28),
                 dr=[(cash.id, D("500.00"), "碰巧同额的一笔")],
                 cr=[(subjects["560301"].id, D("500.00"), "碰巧同额的一笔")])
    # 造一张**还没提交**的发票占住这一行
    record = _pending_invoice(db, book)
    row = db.get(BankStatementRow, rows[0]["id"])
    row.matched_invoice_record_id = record.id
    db.commit()

    plan = service.plan_vouchers(db, book.id, statement["id"])
    assert plan["review_count"] == 0, plan
    entry = _by_id(plan)[rows[0]["id"]]
    assert entry["voucher_state"] == "INVOICE", entry
    db.refresh(row)
    assert row.status != "POSTED", (
        "★ 被发票占用的行被启发式标成了「已入账」—— 发票再也占不住它了")


def _pending_invoice(db, book):
    """造一张还没提交的发票记录。"""
    from app.models import InvoiceBatch, InvoiceRecord

    batch = InvoiceBatch(book_id=book.id, filename="造出来的.pdf", file_path="",
                          status="PENDING", total=1, created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=0,
        invoice_no="24417000000000009999", invoice_type="OUTPUT",
        status="PENDING",
        payload={"meta": {"invoice_no": "24417000000000009999",
                          "issue_date": "2026-09-20", "seller_name": "某客户",
                          "seller_tax_id": "91310000MA0000000X",
                          "buyer_name": "测试公司", "buyer_tax_id": "91310000MA0000000X",
                          "total_excl_tax": "500.00", "total_tax": "0.00",
                          "total_incl_tax": "500.00"},
                 "lines": [{"name": "办公用品", "display_name": "办公用品",
                            "amount": "500.00", "tax": "0.00", "tax_rate": "6%"}]},
    )
    db.add(record)
    db.flush()
    return record


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def _voucher_factory(db, book, admin_user):
    """建凭证的小工厂（借贷平衡、科目年度都对）。

    **不借用 conftest 那个**：那个绑的是单年账套，这里要能指定任意年度的
    科目（跨年搬科目的用例要用 2027 年的科目）。
    """
    from app.accounting.vouchers import create_voucher

    def _make(voucher_date, *, dr, cr, posted=False):
        entries = []
        for subject_id, amount, summary in dr:
            entries.append({"subject_id": subject_id, "direction": "DEBIT",
                            "amount": D(str(amount)), "summary": summary})
        for subject_id, amount, summary in cr:
            entries.append({"subject_id": subject_id, "direction": "CREDIT",
                            "amount": D(str(amount)), "summary": summary})
        voucher = create_voucher(db, book_id=book.id, user=admin_user,
                                 voucher_date=voucher_date, entries=entries,
                                 source="MANUAL", commit=False)
        db.flush()
        if posted:
            from app.accounting.vouchers import post_vouchers
            post_vouchers(db, book.id, [voucher.id], admin_user)
            db.refresh(voucher)
        return voucher

    return _make
