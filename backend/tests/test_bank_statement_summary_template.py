"""流水生成凭证时的**摘要模板**。

对账单上「摘要」那一列装的根本不是一回事：有的银行写「转入」「支取」，
有的写「办公室租金」。内置口径是「用途优先、摘要兜底」，但那只是
**大多数情况**下对的选择 —— 用户要能改。

三件要盯住的：

1. **取值序 = 规则级 > 账套级 > 内置口径**；
2. **模板写错要当场说清**（不认识的占位符在存的时候就拒），而不是存下来
   让人以为「我设了模板但没生效」；
3. **渲染出错退回内置口径** —— 一条流水的模板写错了，不该让这一份
   对账单的其他流水都生成不出凭证。
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.models import Entry, Subject, Voucher

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def cash(db, book):
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


def _row(db, book, statement):
    rows = service.list_rows(db, book.id, statement["id"])
    return next(r for r in rows if r["direction"] in ("IN", "OUT") and r["amount"])


def _voucher_summary(db, voucher_id) -> str:
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher_id).order_by(Entry.line_no)).first()
    return entry.summary


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------

def test_模板占位符替换(db, book):
    class _Row:
        summary = "转入"
        purpose = "办公室租金"
        counterparty_name = "某某公司"
        tx_date = None
        amount = None
        seq = 3
        direction = "IN"

    out = service.render_summary(_Row(), "{用途}·{对方户名}")
    assert out == "办公室租金·某某公司"


def test_模板为空退回内置口径(db, book):
    class _Row:
        summary = "转入"
        purpose = "办公室租金"
        counterparty_name = "某某公司"
        tx_date = None
        amount = None
        seq = 3
        direction = "IN"

    assert service.render_summary(_Row(), "") == "办公室租金"
    assert service.render_summary(_Row(), "{不存在的占位符}") == "办公室租金", (
        "模板写错要退回内置口径，而不是渲染成半截文字")


def test_没有用途时用摘要():
    class _Row:
        summary = "转入"
        purpose = ""
        counterparty_name = "某某公司"
        tx_date = None
        amount = None
        seq = 3
        direction = "IN"

    assert service.render_summary(_Row(), "") == "转入"


def test_两样都没有时用对方户名():
    class _Row:
        summary = ""
        purpose = ""
        counterparty_name = "某某公司"
        tx_date = None
        amount = None
        seq = 3
        direction = "OUT"

    assert service.render_summary(_Row(), "") == "银行付 某某公司"


# ----------------------------------------------------------------------
# 存取
# ----------------------------------------------------------------------

def test_不认识的占位符在存的时候就拒(db, book):
    with pytest.raises(ValueError) as exc:
        service.save_config(db, book.id, {"summary_template": "{摘要}{瞎写的}"})
    assert "占位符" in str(exc.value), f"提示没说明白：{exc.value}"


def test_账套级模板存得下取得到(db, book):
    service.save_config(db, book.id, {"summary_template": "流水 {用途}"})
    db.commit()
    config = service.get_config(db, book.id)
    assert config is not None
    assert config.summary_template == "流水 {用途}"
    assert service.serialize_config(config)["summary_template"] == "流水 {用途}"


def test_账套级模板生效(statement, db, book, admin_user, subjects):
    row = _row(db, book, statement)
    service.save_config(db, book.id, {"summary_template": "对账 {摘要}"})
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [{"subject_id": subjects["560202"].id,
                           "amount": row["amount"]}]})
    db.commit()

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, f"制证失败：{result['failures']}"
    summary = _voucher_summary(db, result["created"][0]["voucher_id"])
    assert summary == f"对账 {row['summary']}", f"摘要没按模板渲染：{summary}"


def test_规则级模板盖过账套级(statement, db, book, admin_user, subjects):
    row = _row(db, book, statement)
    service.save_config(db, book.id, {"summary_template": "账套级 {摘要}"})
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": row["summary"][:4],
        "subject_id": subjects["560202"].id, "priority": 0, "active": True,
        "summary_template": "规则级 {摘要}"})
    db.commit()

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, f"制证失败：{result['failures']}"
    summary = _voucher_summary(db, result["created"][0]["voucher_id"])
    assert summary == f"规则级 {row['summary']}", f"规则级模板没盖过账套级：{summary}"


def test_人工指定的行用账套级模板(statement, db, book, admin_user, subjects):
    """人工指定过的行不是规则定的 —— 规则级模板不该管它。"""
    row = _row(db, book, statement)
    service.save_config(db, book.id, {"summary_template": "账套级 {摘要}"})
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": row["summary"][:4],
        "subject_id": subjects["560202"].id, "priority": 0, "active": True,
        "summary_template": "规则级 {摘要}"})
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [{"subject_id": subjects["560204"].id,
                           "amount": row["amount"]}]})
    db.commit()

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, f"制证失败：{result['failures']}"
    summary = _voucher_summary(db, result["created"][0]["voucher_id"])
    assert summary == f"账套级 {row['summary']}", f"人工指定的行不该用规则级模板：{summary}"
