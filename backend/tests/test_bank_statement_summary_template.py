"""流水生成凭证时的**摘要模板**。

对账单上「摘要」那一列装的根本不是一回事：有的银行写「转入」「支取」，
有的写「办公室租金」。内置口径是「用途优先、摘要兜底」，但那只是
**大多数情况**下对的选择 —— 用户要能改。

三件要盯住的：

1. **取值序 = 规则级 > 内置口径**（**账套级已于 2026-10-28 下线**）；
2. **模板写错要当场说清**（不认识的占位符在存的时候就拒），而不是存下来
   让人以为「我设了模板但没生效」；
3. **渲染出错退回内置口径** —— 一条流水的模板写错了，不该让这一份
   对账单的其他流水都生成不出凭证。

**账套级模板为什么下线**（用户原话：「完全使用内置口径就可以了，不要
让用户更改」）：摘要模板是**一个要学语法的东西**（占位符、渲染规则），
而它真正解决的问题很小。放在账套级，意味着每个账套都要有人先学会这套
语法才敢用；大部分账套一句都不需要改。**规则级模板留着** —— 那是
「这一类流水要用另一种说法」的表达方式，与关键词规则放在一起才对得上
语境，天然是可选的。
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


def _builtin_summary(db, row_id) -> str:
    """这一行**按内置口径**算出来的摘要（用途优先、摘要兜底）。

    用它当期望值，而不是把期望值写死 —— 内置口径改了，这条用例会跟着
    一起改，而不是变成一个「钉死了旧口径」的假失败。
    """
    from app.models import BankStatementRow
    return service.render_summary(db.get(BankStatementRow, row_id), "")


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

def test_不认识的占位符在存的时候就拒(db, book, admin_user, subjects):
    """**规则级模板**仍然当场拒 —— 那是现在唯一能设摘要模板的地方。

    账套级模板下线了，但「模板写错不许存」这条纪律没有跟着走：规则级
    模板写错，一样会让渲染退回内置口径，而那正是用户以为「我设了模板
    但没生效」时最难受的一种情况。
    """
    with pytest.raises(ValueError) as exc:
        service.create_rule(db, book.id, admin_user, {
            "match_field": "SUMMARY", "keyword": "运费",
            "subject_id": subjects["560202"].id, "priority": 0, "active": True,
            "summary_template": "{摘要}{瞎写的}"})
    assert "占位符" in str(exc.value), f"提示没说明白：{exc.value}"


def test_账套级模板已下线_显式报错(db, book):
    """账套级摘要模板下线了 —— **显式报错**，不是静默丢掉。

    **为什么不能静默丢掉**：请求体进服务层之前已经被 pydantic 按 schema
    过滤过（``model_dump(exclude_unset=True)``），一个「不认识」的字段会被
    静默丢掉 —— 接口回 200、库里什么都没变。老客户端（或者某个脚本）
    继续发这个字段时，用户以为「改了摘要模板」，实际毫无影响。
    「写了却看不见，等于没写」—— 所以要明确说清「这个字段没有了」。
    """
    with pytest.raises(ValueError) as exc:
        service.save_config(db, book.id, {"summary_template": "流水 {用途}"})
    message = str(exc.value)
    assert "下线" in message, message
    assert "规则级" in message, "报错要说清替代方案在哪：%s" % message


def test_账套级配置接口_带模板回400_不带模板不建空行(client, auth, db, book):
    """接口这一层也要守住 —— 服务层那句「显式报错」得真的传出去。"""
    from app.models import BankStatementConfig

    # **带着这个字段来** → 400 + 说清新口径
    r = client.put(f"/api/books/{book.id}/bank-statement-config", headers=auth,
                   json={"summary_template": "流水 {用途}"})
    assert r.status_code == 400, r.text
    assert "下线" in r.json()["detail"], r.text

    # **不带这个字段来** → 200，而且**不建空行**：这个表现在没有用途了，
    # 建一行空的只是让人以为「这个账套配过什么」。
    r = client.put(f"/api/books/{book.id}/bank-statement-config", headers=auth,
                   json={})
    assert r.status_code == 200, r.text
    assert r.json()["summary_template"] == ""
    db.expire_all()
    assert db.scalars(select(BankStatementConfig).where(
        BankStatementConfig.book_id == book.id)).first() is None


def test_账套级模板不再生效(statement, db, book, admin_user, subjects):
    """**库里就算留着老值，也不再生效。**

    这一条比「接口报错」更要紧：报错拦的是**新**请求，而库里**已经存
    着**的老值（用户以前设过的）还在。前端删掉输入框 ≠ 后端不读它 ——
    那会变成「界面上没有、却还在生效」的口径。
    """
    from app.models import BankStatementConfig

    # **绕过 save_config 直接写库** —— 模拟「用户以前设过、现在还在库里」
    config = BankStatementConfig(book_id=book.id, summary_template="老账套级 {摘要}")
    db.add(config)
    db.commit()

    row = _row(db, book, statement)
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [{"subject_id": subjects["560202"].id,
                           "amount": row["amount"]}]})
    db.commit()

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, f"制证失败：{result['failures']}"
    summary = _voucher_summary(db, result["created"][0]["voucher_id"])
    assert "老账套级" not in summary, f"账套级模板还在生效：{summary}"
    assert summary == _builtin_summary(db, row["id"]), (
        f"摘要该用内置口径，实际是 {summary}")


def test_规则级模板盖过内置口径(statement, db, book, admin_user, subjects):
    """**取值序 = 规则级 > 内置口径**（账套级已下线）。

    规则级模板仍在：那是「这一类流水（运费、工资）要用另一种说法」的
    表达方式，与关键词规则放在一起才对得上语境。
    """
    row = _row(db, book, statement)
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": row["summary"][:4],
        "subject_id": subjects["560202"].id, "priority": 0, "active": True,
        "summary_template": "规则级 {摘要}"})
    db.commit()

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, f"制证失败：{result['failures']}"
    summary = _voucher_summary(db, result["created"][0]["voucher_id"])
    assert summary == f"规则级 {row['summary']}", f"规则级模板没生效：{summary}"


def test_人工指定的行用内置口径(statement, db, book, admin_user, subjects):
    """人工指定过的行不是规则定的 —— 规则级模板不该管它。

    **账套级模板下线后，人工指定的行落到内置口径上**（原来落到账套级
    模板上）。这是「人工优先」这条纪律的延续：用户自己挑了科目、自己
    写的摘要，规则不该去改它。
    """
    row = _row(db, book, statement)
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
    assert "规则级" not in summary, f"人工指定的行不该用规则级模板：{summary}"
    assert summary == _builtin_summary(db, row["id"]), (
        f"人工指定的行该用内置口径，实际是 {summary}")


# ----------------------------------------------------------------------
# 渲染口径：前后端共用**一份基准**
# ----------------------------------------------------------------------
#
# 前端的「预览」要显示「这条流水最后会长成什么样」—— 预览与真生成的摘要
# 对不上，那预览就等于没有预览。而这两处是**两套代码**（Python 与 JS），
# 所以它们共用一份基准文件：各自钉一条用例读它。口径一漂移，至少有一边红。
#
# 基准放在 fixtures 里而不是各写一份期望值 —— 期望值写两遍，它迟早会漂。

_RENDER_CASES = (pathlib.Path(__file__).resolve().parent / "fixtures"
                 / "summary_template_render_cases.json")


def test_渲染口径与前端预览共用同一份基准():
    import json
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    cases = json.loads(_RENDER_CASES.read_text(encoding="utf-8"))
    assert cases["cases"], "基准文件是空的 —— 那这条用例等于没跑"

    for case in cases["cases"]:
        raw = case["row"]
        row = SimpleNamespace(
            seq=raw["seq"],
            tx_date=date.fromisoformat(raw["tx_date"]),
            direction=raw["direction"],
            amount=Decimal(raw["amount"]),
            summary=raw["summary"],
            purpose=raw["purpose"],
            counterparty_name=raw["counterparty_name"],
        )
        out = service.render_summary(row, case["template"])
        assert out == case["expected"], (
            f"{case['name']}：渲染出 {out!r}，基准写的是 {case['expected']!r}"
            f"（前端预览用的是同一份基准，两边对不上就是口径漂了）")
