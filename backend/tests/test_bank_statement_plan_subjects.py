"""一行流水的**多个**对方科目（科目 + 金额）。

**为什么要有这组用例。** 一笔流水可能对应好几个科目：一笔 1000 元的付款
里 800 是房租、200 是押金。对方科目与金额是一一对应的，所以金额必须跟着
科目走 —— 而「合计必须等于流水金额」是这件事唯一说得通的硬约束。

三条要盯住的：

1. **合计必须等于流水金额** —— 不等的话凭证借贷不平（或者更糟：平了但
   钱记在错误的科目上）；
2. **制证时按组分录** —— 一行流水生成一张凭证，但分录不止两条；
3. **规则命中给的是一条** —— 规则没有金额，配不出「800 房租 + 200 押金」
   这种拆分来，而拆分的金额只有用户知道（凭发票、凭单据）。
"""
from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.models import Entry, Subject, Voucher
from app.models.bankstatement import (
    DIRECTION_IN,
    DIRECTION_OUT,
    PLAN_MANUAL,
    PLAN_PENDING,
    PLAN_RULE,
)

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
    return next(r for r in rows if r["direction"] in (DIRECTION_IN, DIRECTION_OUT)
                and r["amount"])


def _half(amount: str) -> tuple[str, str]:
    total = Decimal(amount)
    first = (total / 2).quantize(Decimal("0.01"))
    return str(first), str(total - first)


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------

def test_一行流水可以拆到多个对方科目(statement, db, book, subjects):
    row = _row(db, book, statement)
    first, second = _half(row["amount"])
    result = service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": first},
            {"subject_id": subjects["560204"].id, "amount": second},
        ]})
    assert result["plan_status"] == PLAN_MANUAL
    assert len(result["plan_subjects"]) == 2
    assert result["plan_subject_total"] == row["amount"], (
        "合计必须等于流水金额")
    assert {i["subject_id"] for i in result["plan_subjects"]} == {
        subjects["560202"].id, subjects["560204"].id}


def test_合计不等于流水金额要拒绝(statement, db, book, subjects):
    row = _row(db, book, statement)
    with pytest.raises(ValueError) as exc:
        service.update_row(db, book.id, row["id"], {
            "plan_subjects": [
                {"subject_id": subjects["560202"].id, "amount": "1.00"},
            ]})
    assert "不一致" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"


def test_同一个科目在一行里可以出现两次(statement, db, book, subjects):
    """**2026-10-29 用户批注：「不应该有这个限制」。**

    原来这里报「在这一行里出现了两次 —— 要记两笔请把金额合并到一条上」，
    还带着一条库级 ``UNIQUE (row_id, subject_id)``。两条理由说明为什么
    该放开：

    ① **摘要是挂在这一层上的**（迁移 0043/0044 把它从行级下沉到对方科目
       这一层）。既然摘要的粒度就是这一层，「同一个科目拆成两条、各写各的
       摘要」本来就是这套设计的用法；
    ② 账上两条分录指向同一个科目本来就是合法的 —— 金额各算各的、摘要各写
       各的。禁掉它，用户只能「先把两条合并、再丢掉其中一条的摘要」。

    **真正该拦的是「合计对不上」，那个闸还在**（下一条用例）。
    """
    row = _row(db, book, statement)
    first, second = _half(row["amount"])
    result = service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": first,
             "summary": "第一段"},
            {"subject_id": subjects["560202"].id, "amount": second,
             "summary": "第二段"},
        ]})
    assert result["plan_status"] == PLAN_MANUAL
    assert len(result["plan_subjects"]) == 2, (
        "同一个科目拆成两条被拒了 —— 用户要的就是这个")
    assert result["plan_subject_total"] == row["amount"]
    # **两条各有各的摘要** —— 这正是把摘要下沉到这一层的用处。
    assert [i["summary_override"] for i in result["plan_subjects"]] == [
        "第一段", "第二段"]


def test_同一个科目出现两次_合计不对照样要拒绝(statement, db, book, subjects):
    """放开重复**不等于**放开金额 —— 合计必须等于流水金额。"""
    row = _row(db, book, statement)
    with pytest.raises(ValueError) as exc:
        service.update_row(db, book.id, row["id"], {
            "plan_subjects": [
                {"subject_id": subjects["560202"].id, "amount": "1.00"},
                {"subject_id": subjects["560202"].id, "amount": "1.00"},
            ]})
    assert "不一致" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"


def test_重写对方科目时人工摘要各自跟回自己那一条(statement, db, book, subjects):
    """同一个科目拆成两条、两条各有各的摘要 —— 重写一次之后**不许串成
    两句一样的**，也不许互相顶掉。

    摘要的粒度就是「这一条对方科目」，不是「这个科目」——
    两条同一个科目的分录，说的是两件事。
    """
    row = _row(db, book, statement)
    first, second = _half(row["amount"])
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": first,
             "summary": "第一段"},
            {"subject_id": subjects["560202"].id, "amount": second,
             "summary": "第二段"},
        ]})
    # **不写 summary** = 沿用同一科目上原来那句。
    result = service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": first},
            {"subject_id": subjects["560202"].id, "amount": second},
        ]})
    assert [i["summary_override"] for i in result["plan_subjects"]] == [
        "第一段", "第二段"], (
        "两条人工摘要串了 —— 那会让一笔账上出现两句一模一样的摘要")


def test_金额必须是正数(statement, db, book, subjects):
    row = _row(db, book, statement)
    with pytest.raises(ValueError):
        service.update_row(db, book.id, row["id"], {
            "plan_subjects": [
                {"subject_id": subjects["560202"].id, "amount": "0"},
            ]})


def test_非末级科目要拒绝(statement, db, book, subjects):
    """5602 管理费用下面挂了子科目 —— 凭证只记末级，流水计划也一样。"""
    row = _row(db, book, statement)
    with pytest.raises(ValueError) as exc:
        service.update_row(db, book.id, row["id"], {
            "plan_subjects": [
                {"subject_id": subjects["5602"].id, "amount": row["amount"]},
            ]})
    assert "末级" in str(exc.value)


def test_停用科目要拒绝(statement, db, book, subjects):
    row = _row(db, book, statement)
    subject = subjects["560202"]
    subject.active = False
    db.commit()
    db.expire_all()
    with pytest.raises(ValueError) as exc:
        service.update_row(db, book.id, row["id"], {
            "plan_subjects": [
                {"subject_id": subjects["560202"].id, "amount": row["amount"]},
            ]})
    assert "停用" in str(exc.value)


def test_清空对方科目退回待确认(statement, db, book, subjects):
    row = _row(db, book, statement)
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": row["amount"]},
        ]})
    result = service.update_row(db, book.id, row["id"], {"plan_subjects": []})
    assert result["plan_subjects"] == []
    assert result["plan_status"] == PLAN_PENDING


def test_两种写法不能同时给(statement, db, book, subjects):
    row = _row(db, book, statement)
    with pytest.raises(ValueError):
        service.update_row(db, book.id, row["id"], {
            "plan_subject_id": subjects["560202"].id,
            "plan_subjects": [
                {"subject_id": subjects["560202"].id, "amount": row["amount"]},
            ]})


# ----------------------------------------------------------------------
# 规则
# ----------------------------------------------------------------------

def test_规则命中给的是一条_金额就是流水金额(statement, db, book, admin_user, subjects):
    """规则没有金额，配不出拆分来 —— 所以规则命中只给一条。"""
    rows = service.list_rows(db, book.id, statement["id"])
    keyword = rows[0]["summary"][:4]
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": keyword,
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    rows = service.list_rows(db, book.id, statement["id"])
    hit = [r for r in rows if r["summary"].startswith(keyword)]
    assert hit, "找不到摘要以关键词开头的行"
    assert hit[0]["plan_status"] == PLAN_RULE
    assert len(hit[0]["plan_subjects"]) == 1
    assert hit[0]["plan_subjects"][0]["amount"] == hit[0]["amount"]


# ----------------------------------------------------------------------
# 制证
# ----------------------------------------------------------------------

def test_制证按组生成分录(statement, db, book, admin_user, subjects):
    """一行流水 → 一张凭证，但分录是「现金一条 + 对方两条」。"""
    row = _row(db, book, statement)
    first, second = _half(row["amount"])
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": first},
            {"subject_id": subjects["560204"].id, "amount": second},
        ]})
    db.commit()

    result = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert result["created_count"] == 1, f"制证失败：{result['failures']}"
    voucher = db.get(Voucher, result["created"][0]["voucher_id"])
    entries = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).all()
    assert len(entries) == 3, f"应当是三条分录（现金 + 两个对方科目），实际 {len(entries)}"

    debit = sum((e.amount for e in entries if e.direction == "DEBIT"), Decimal("0"))
    credit = sum((e.amount for e in entries if e.direction == "CREDIT"), Decimal("0"))
    assert debit == credit, "凭证借贷不平"
    assert debit == Decimal(row["amount"]), "分录合计不等于流水金额"

    # 现金那一条的方向：流水 IN（收钱）记借方，OUT 记贷方。
    cash_entry = [e for e in entries if e.subject_id == subjects["100201"].id]
    assert len(cash_entry) == 1
    expect = "DEBIT" if row["direction"] == DIRECTION_IN else "CREDIT"
    assert cash_entry[0].direction == expect

    # 对方两条：各记各的金额。
    others = {e.subject_id: e.amount for e in entries
              if e.subject_id != subjects["100201"].id}
    assert others == {subjects["560202"].id: Decimal(first),
                      subjects["560204"].id: Decimal(second)}


def test_制证前对方科目金额对不上就跳过(statement, db, book, admin_user, subjects):
    """合计对不上的行不进候选 —— 凭证会不平衡。

    这里是**第二道闸**：写入口（``_parse_plan_payload``）已经挡住了，
    但金额可能在这之后被别处改过（行是旧的、别的路径写的）。所以制证
    之前还要再判一次。
    """
    from app.models import BankStatementRowSubject

    row = _row(db, book, statement)
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": row["amount"]},
        ]})
    # 绕过写入口直接改库，模拟「别处写进去的脏数据」。
    child = db.scalars(select(BankStatementRowSubject).where(
        BankStatementRowSubject.row_id == row["id"])).first()
    child.amount = Decimal("1.00")
    db.commit()
    db.expire_all()

    plan = service.plan_vouchers(db, book.id, statement["id"])
    target = next(s for s in plan["skipped"] if s["id"] == row["id"])
    assert "对不上" in target["reason"], f"没被拦住：{target['reason']}"
    assert row["id"] not in [c["id"] for c in plan["candidates"]]


# ----------------------------------------------------------------------
# 已入账 / 已忽略：状态与科目的可改性
# ----------------------------------------------------------------------
#
# 这两条是**用户在实际使用里撞出来的**：
#
# 1. 「流水明细点击忽略之后怎么没办法点击恢复？」—— 挡住的不是后端，是
#    界面上的那个按钮（条件写成 ``status !== 'OPEN'``，忽略之后自己把自己
#    禁用了）。后端本来就允许恢复，这里钉住它**不许退**。
# 2. 「状态是已入账的流水，设置成不可以再指定科目了！」—— 已入账的流水，
#    科目**由凭证定死**：只改流水行、不改凭证，两边就对不上，而且界面上
#    看不出来。要改，改的是凭证。


def test_已忽略的行可以恢复(statement, db, book):
    """点完「忽略」还要点得回来 —— 这是后端本来就允许的，别把它堵死。"""
    row = _row(db, book, statement)
    service.update_row(db, book.id, row["id"], {"status": "IGNORED"})
    db.commit()
    result = service.update_row(db, book.id, row["id"], {"status": "OPEN"})
    assert result["status"] == "OPEN"


def test_已入账的行不能再指定对方科目(statement, db, book, admin_user, subjects):
    """凭证已经把科目定死了 —— 流水行不许再单独改它。"""
    row = _row(db, book, statement)
    service.update_row(db, book.id, row["id"], {
        "plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": row["amount"]},
        ]})
    db.commit()
    created = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert created["created_count"] == 1, f"制证失败：{created['failures']}"
    db.expire_all()

    with pytest.raises(ValueError) as exc:
        service.update_row(db, book.id, row["id"], {
            "plan_subjects": [
                {"subject_id": subjects["560204"].id, "amount": row["amount"]},
            ]})
    msg = str(exc.value)
    assert "凭证" in msg, f"提示没说出为什么不能改：{msg}"
    db.rollback()

    # **改不动**：科目还是原来那个（没被偷偷改掉）
    db.expire_all()
    after = next(r for r in service.list_rows(db, book.id, statement["id"])
                 if r["id"] == row["id"])
    assert [i["subject_id"] for i in after["plan_subjects"]] == [subjects["560202"].id]


def test_规则重算不动已入账的行(statement, db, book, admin_user, subjects):
    """规则改的是「还没入账的行该怎么记」，碰不到已经记好的账。

    这是**另一扇门**：用户点不了「指定」，可改一条规则，全量重算一样会
    把已入账行的对方科目改掉 —— 一样是两边对不上、一样看不出来。
    """
    rows = service.list_rows(db, book.id, statement["id"])
    keyword = rows[0]["summary"][:4]
    rule = service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": keyword,
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    db.commit()

    row = next(r for r in service.list_rows(db, book.id, statement["id"])
               if r["summary"].startswith(keyword))
    assert row["plan_status"] == PLAN_RULE, row["plan_status"]

    created = service.commit_vouchers(db, book.id, statement["id"], admin_user)
    assert created["created_count"] >= 1, created
    db.expire_all()

    posted = next(r for r in service.list_rows(db, book.id, statement["id"])
                  if r["id"] == row["id"])
    assert posted["status"] == "POSTED", posted["status"]
    assert [i["subject_id"] for i in posted["plan_subjects"]] == [subjects["560202"].id]

    # 删掉那条规则 → 全量重算。没修的话，已入账那行的科目会被清掉，
    # 界面上显示成「待确认科目」—— 而它其实早记好了。
    service.delete_rule(db, book.id, rule["id"])
    db.commit()
    db.expire_all()

    after = next(r for r in service.list_rows(db, book.id, statement["id"])
                 if r["id"] == row["id"])
    assert [i["subject_id"] for i in after["plan_subjects"]] == [subjects["560202"].id], (
        "已入账的行被规则改了科目 —— 它与账上的凭证对不上，且看不出来")
    assert after["plan_status"] == PLAN_RULE, after["plan_status"]
