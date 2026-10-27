"""流水 ↔ 账上已有的现金分录：匹配与「改科目」。

**这一组用例盯的是「钱记错账户了」这条路径。**

典型场景：用户收到一笔款，凭印象记在「1001 库存现金」上（钱还没到手就先
记了现金），后来银行对账单出来了 —— 这笔钱明明走的是 1002 银行存款。
于是账上：1001 挂着一笔不该有的现金，1002 少了一笔。

改完之后必须成立的四件事：

1. 那条分录的**科目**从 1001 变成 1002；
2. **金额、方向、摘要、对方分录一个字都没动**（只改科目）；
3. 这条流水因此变成「已入账」（钱已经在 1002 上了）；
4. 一切改动**留了痕**（审计日志），而且**已记账 / 已结账的凭证动不得**。

用例跑的是真的账套与真的凭证表 —— 改科目改的是账上的数字，
在假对象上验证它等于没验证。
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.models import AuditLog, Entry, Subject, Voucher
from app.models.bankstatement import (
    DIRECTION_IN,
    DIRECTION_OUT,
    STATUS_POSTED,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def cash(db, book):
    """1002 下面挂一个明细科目（真实用户就是这么用的：1002 下面挂好几个
    子科目，一账户一份对账单）。"""
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    assert parent is not None
    parent.is_leaf = False
    db.flush()
    sub = Subject(
        book_id=book.id, year=book.start_year, code="100201", name="基本户",
        parent_id=parent.id, level=parent.level + 1, category="ASSET",
        direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=1,
        active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


@pytest.fixture
def statement(db, book, admin_user, cash, store):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    return service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                    data, cash.id)


def _first_row(db, book, statement):
    rows = service.list_rows(db, book.id, statement["id"])
    row = next(r for r in rows if r["direction"] in (DIRECTION_IN, DIRECTION_OUT)
               and r["amount"])
    return row


def _as_date(text: str):
    from datetime import date

    return date.fromisoformat(text)


def _voucher_on_cash(db, book, admin_user, subjects, row, cash_code="1001"):
    """造一张「现金侧记在 1001 上」的凭证，日期金额与那一行流水对齐。

    **方向按流水翻**：流水 IN（收钱）在会计上现金记借方，流水 OUT 记贷方。
    """
    if row["direction"] == DIRECTION_IN:
        cash_side = ("DEBIT", cash_code)
        other_side = ("CREDIT", "5001")
    else:
        cash_side = ("CREDIT", cash_code)
        other_side = ("DEBIT", "560202")
    entries = [
        {"subject_id": subjects[cash_side[1]].id, "direction": cash_side[0],
         "amount": str(row["amount"]), "summary": "现金那侧"},
        {"subject_id": subjects[other_side[1]].id, "direction": other_side[0],
         "amount": str(row["amount"]), "summary": "对方那侧"},
    ]
    voucher = service.voucher_service.create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=_as_date(row["tx_date"]), entries=entries,
        source="MANUAL", commit=False)
    db.commit()
    db.refresh(voucher)
    return voucher


# ----------------------------------------------------------------------
# 匹配
# ----------------------------------------------------------------------

def test_流水能匹配到记在1001上的分录(statement, db, book, admin_user, subjects):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    candidates = hit["reclassify_candidates"]
    assert candidates, "记在 1001 上的同额分录没被认出来"
    first = candidates[0]
    assert first["subject_code"] == "1001"
    assert first["voucher_id"] == voucher.id
    assert first["changeable"] is True
    assert first["block_reason"] == ""


def test_同一个账户上的分录不算候选(statement, db, book, admin_user, subjects):
    """已经在 1002 上的那笔是「已入账」，由去重那条路处理 —— 不是「可改科目」。"""
    row = _first_row(db, book, statement)
    # 现金侧记在**这份对账单绑定的账户**上（100201）。
    dr = [("100201", row["amount"], "收款")]
    cr = [("5001", row["amount"], "收入")]
    if row["direction"] == DIRECTION_OUT:
        dr, cr = [("5602", row["amount"], "费用")], [("100201", row["amount"], "付款")]
    service.voucher_service.create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=_as_date(row["tx_date"]),
        entries=[{"subject_id": subjects[dr[0][0]].id, "direction": "DEBIT",
                  "amount": dr[0][1], "summary": dr[0][2]},
                 {"subject_id": subjects[cr[0][0]].id, "direction": "CREDIT",
                  "amount": cr[0][1], "summary": cr[0][2]}],
        source="MANUAL", commit=False)
    db.commit()
    db.expire_all()

    # **「已入账」这个状态是 plan_vouchers 算出来的**（去重就在那儿），
    # 界面上也是先算清单再看行 —— 所以这里照着界面的次序来。
    plan = service.plan_vouchers(db, book.id, statement["id"])
    db.expire_all()
    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["reclassify_candidates"] == [], (
        "同一个账户上的分录不该出现在「可改科目」候选里")
    assert hit["voucher_state"] == "POSTED", "它该被去重认成已入账"
    skipped = [s for s in plan["skipped"] if s["id"] == row["id"]]
    assert skipped and skipped[0]["reason"].startswith("已入账"), (
        f"清单里没有把它标成已入账：{skipped}")


def test_金额对不上就匹配不上(statement, db, book, admin_user, subjects):
    row = _first_row(db, book, statement)
    other = {"amount": str(float(row["amount"]) + 1), "tx_date": row["tx_date"],
             "direction": row["direction"]}
    _voucher_on_cash(db, book, admin_user, subjects, other)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["reclassify_candidates"] == []


def test_超出七天窗口就匹配不上(statement, db, book, admin_user, subjects):
    from datetime import date, timedelta

    row = _first_row(db, book, statement)
    when = date.fromisoformat(row["tx_date"]) - timedelta(days=8)
    shifted = {"amount": row["amount"], "tx_date": when.isoformat(),
               "direction": row["direction"]}
    _voucher_on_cash(db, book, admin_user, subjects, shifted)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["reclassify_candidates"] == [], (
        "窗口外的分录不该被当成同一笔钱")


# ----------------------------------------------------------------------
# 改科目
# ----------------------------------------------------------------------

def test_改科目之后现金侧挪到本账户对方侧不动(statement, db, book, admin_user, subjects):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row)
    before = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).all()
    before_other = [e for e in before if e.subject_id != subjects["1001"].id][0]
    before_summary = before[0].summary
    db.expire_all()

    result = service.reclassify_row_subject(
        db, book.id, row["id"], before[0].id, admin_user)
    assert result["from_subject"]["code"] == "1001"
    assert result["to_subject"]["code"] == "100201"

    db.expire_all()
    after = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).all()
    cash_side = [e for e in after if e.subject_id == subjects["100201"].id]
    assert len(cash_side) == 1, "现金侧应当挪到这份对账单绑定的账户上"
    assert cash_side[0].amount == before[0].amount, "金额不能动"
    assert cash_side[0].direction == before[0].direction, "方向不能动"
    assert cash_side[0].summary == before_summary, "摘要不能动"
    # **对方侧一个字都没动** —— 这是用户定的口径：只改现金那一侧。
    other = [e for e in after if e.id != cash_side[0].id][0]
    assert other.subject_id == before_other.subject_id
    assert other.amount == before_other.amount


def test_改完之后这条流水变成已入账(statement, db, book, admin_user, subjects):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["voucher_state"] == "POSTED", (
        "钱已经在 1002 上了，这条流水不该再被当成没入账")
    assert hit["reclassify_candidates"] == [], (
        "改完之后不该还把它列成候选")


def test_改科目留了审计痕迹(statement, db, book, admin_user, subjects):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    db.expire_all()

    log = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_id == str(voucher.id), AuditLog.action == "UPDATE")
        .order_by(AuditLog.id.desc())
    ).first()
    assert log is not None, "改科目没有留下审计 —— 事后没人查得出这张怎么变的"


def test_已记账的凭证改不了(statement, db, book, admin_user, subjects):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    voucher.status = "POSTED"
    db.commit()
    db.expire_all()

    with pytest.raises(ValueError) as exc:
        service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    assert "已记账" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"

    db.expire_all()
    still = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    assert still.subject_id == subjects["1001"].id, "拒绝之后不该动过分录"


def test_不能改不是现金科目的分录(statement, db, book, admin_user, subjects):
    """对方侧那条不是现金科目 —— 改它跟「钱记错账户」无关。"""
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row)
    other = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id,
        Entry.subject_id != subjects["1001"].id)).first()
    db.expire_all()

    with pytest.raises(ValueError) as exc:
        service.reclassify_row_subject(db, book.id, row["id"], other.id, admin_user)
    assert "现金科目" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"


def test_不属于本账套的分录改不了(statement, db, book, admin_user, subjects):
    """别账套的分录不许碰 —— 跨账套改分录等于把 A 公司的账改到 B 公司上。"""
    row = _first_row(db, book, statement)
    with pytest.raises(ValueError) as exc:
        service.reclassify_row_subject(db, book.id, row["id"], 999999, admin_user)
    assert "分录" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"
