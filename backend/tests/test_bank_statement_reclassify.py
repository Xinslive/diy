"""流水 ↔ 账上已有的现金分录：匹配与「改科目」。

**这一组用例盯的是「钱记错账户了」这条路径。**

典型场景：用户收到一笔款，凭印象记在**另一个银行账户**上（钱还没到手就
先记了），后来对账单出来了 —— 这笔钱明明走的是 100201 基本户。
于是账上：那个账户挂着一笔不该有的钱，100201 少了一笔。

**为什么「错账户」是另一个银行账户而不是库存现金**（2026-10-28 口径变了）：
流水对账这一条线只认 1002 银行存款（见 ``app.bankstatement.caliber``）——
1001 库存现金与 1012 第三方支付备付金都不算。所以「钱记错账户了」这个
场景里的「错账户」，只能是**另一个银行账户**。

改完之后必须成立的四件事：

1. 那条分录的**科目**从 100202 变成 100201；
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
def other_cash(db, book):
    """**另一个银行账户**（1002 下的第二个明细科目）。

    「钱记错账户了」这个场景里的「错账户」只能是它 —— 这一条线只认
    1002 银行存款（``app.bankstatement.caliber``），1001 库存现金与
    1012 第三方支付备付金都不算（银行流水对不上它们）。
    """
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    assert parent is not None
    parent.is_leaf = False
    db.flush()
    sub = Subject(
        book_id=book.id, year=book.start_year, code="100202", name="一般户",
        parent_id=parent.id, level=parent.level + 1, category="ASSET",
        direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=2,
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


def _voucher_on_cash(db, book, admin_user, subjects, row, cash_subject):
    """造一张「现金侧记在**另一个银行账户**上」的凭证，日期金额与那一行流水对齐。

    **方向按流水翻**：流水 IN（收钱）在会计上现金记借方，流水 OUT 记贷方。

    **那个「另一个账户」是 1002 下的另一个明细** —— 这一条线只认 1002
    银行存款（``app.bankstatement.caliber``），1001 库存现金不算。
    """
    if row["direction"] == DIRECTION_IN:
        cash_side = ("DEBIT", cash_subject.id)
        other_side = ("CREDIT", subjects["5001"].id)
    else:
        cash_side = ("CREDIT", cash_subject.id)
        other_side = ("DEBIT", subjects["560202"].id)
    entries = [
        {"subject_id": cash_side[1], "direction": cash_side[0],
         "amount": str(row["amount"]), "summary": "账户那侧"},
        {"subject_id": other_side[1], "direction": other_side[0],
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

def test_流水能匹配到记在别的银行账户上的分录(statement, db, book, admin_user, subjects, other_cash):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    candidates = hit["reclassify_candidates"]
    assert candidates, "记在别的银行账户上的同额分录没被认出来"
    first = candidates[0]
    assert first["subject_code"] == "100202"
    assert first["voucher_id"] == voucher.id
    assert first["changeable"] is True
    assert first["block_reason"] == ""


def test_同一个账户上的分录不算候选(statement, db, book, admin_user, subjects, other_cash):
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


def test_金额对不上就匹配不上(statement, db, book, admin_user, subjects, other_cash):
    row = _first_row(db, book, statement)
    other = {"amount": str(float(row["amount"]) + 1), "tx_date": row["tx_date"],
             "direction": row["direction"]}
    _voucher_on_cash(db, book, admin_user, subjects, other, other_cash)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["reclassify_candidates"] == []


def test_跨过主窗口的仍会列为候选但标成跨期(statement, db, book, admin_user, subjects, other_cash):
    """日期差在主窗口（7 天）之外、但在提示窗口（30 天）之内 —— **照样列出来**，
    只是标成跨期、让人工确认。

    为什么不能一刀切掉：银行流水与会计记账的日期本来就会差 —— 跨月入账、
    月末才记账、票到才付款，都是常事。按 7 天砍掉，等于把「这笔钱记错在
    别的账户上」的那类候选整个藏起来，用户在界面上永远看不到「可改科目」，
    于是又去制一张新凭证 —— 同一笔钱记两遍。
    """
    from datetime import date, timedelta

    row = _first_row(db, book, statement)
    when = date.fromisoformat(row["tx_date"]) - timedelta(days=8)
    shifted = {"amount": row["amount"], "tx_date": when.isoformat(),
               "direction": row["direction"]}
    _voucher_on_cash(db, book, admin_user, subjects, shifted, other_cash)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    candidates = hit["reclassify_candidates"]
    assert candidates, "跨期的候选不该被一刀切掉（用户就靠它找回记错账户的钱）"
    first = candidates[0]
    assert first["in_window"] is False, "跨过主窗口的必须标成跨期"
    assert first["days_diff"] == 8, f"日期差没算对：{first}"
    # 跨期不等于不能改 —— 它是「改了要人确认」，不是「改不了」。
    assert first["changeable"] is True
    assert first["block_reason"] == ""


def test_主窗口之内的候选不算跨期(statement, db, book, admin_user, subjects, other_cash):
    from datetime import date, timedelta

    row = _first_row(db, book, statement)
    when = date.fromisoformat(row["tx_date"]) - timedelta(days=3)
    shifted = {"amount": row["amount"], "tx_date": when.isoformat(),
               "direction": row["direction"]}
    _voucher_on_cash(db, book, admin_user, subjects, shifted, other_cash)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    candidates = hit["reclassify_candidates"]
    assert candidates
    assert candidates[0]["in_window"] is True, "主窗口之内的不该标成跨期"
    assert candidates[0]["days_diff"] == 3


def test_超出提示窗口就匹配不上(statement, db, book, admin_user, subjects, other_cash):
    """超过 30 天就不提示了 —— 再宽就全是噪音（同额在现实里不稀有）。

    生产实测：同额同向的候选在 ±7 天 0 条、±30 天 3 条、±60 天 4 条、
    ±90 天 6 条（73 天、87 天那两条几乎肯定是不同的钱）。
    """
    from datetime import date, timedelta

    row = _first_row(db, book, statement)
    when = date.fromisoformat(row["tx_date"]) - timedelta(days=31)
    shifted = {"amount": row["amount"], "tx_date": when.isoformat(),
               "direction": row["direction"]}
    _voucher_on_cash(db, book, admin_user, subjects, shifted, other_cash)
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["reclassify_candidates"] == [], (
        "超出提示窗口的分录不该被当成同一笔钱")


# ----------------------------------------------------------------------
# 改科目
# ----------------------------------------------------------------------

def test_改科目之后现金侧挪到本账户对方侧不动(statement, db, book, admin_user, subjects, other_cash):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
    before = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).all()
    before_other = [e for e in before if e.subject_id != other_cash.id][0]
    before_summary = before[0].summary
    db.expire_all()

    result = service.reclassify_row_subject(
        db, book.id, row["id"], before[0].id, admin_user)
    assert result["from_subject"]["code"] == "100202"
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


def test_改完之后这条流水变成已入账(statement, db, book, admin_user, subjects, other_cash):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
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


def test_改科目留了审计痕迹(statement, db, book, admin_user, subjects, other_cash):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
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


def test_已记账的凭证改不了(statement, db, book, admin_user, subjects, other_cash):
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
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
    assert still.subject_id == other_cash.id, "拒绝之后不该动过分录"


def test_跨期的候选确认之后也改得动(statement, db, book, admin_user, subjects, other_cash):
    """跨期是「多问一句」，**不是「改不了」**。

    界面上跨期的候选点下去会弹一次确认；确认之后后端该照改不误 ——
    如果这里也拦，用户确认完了还是改不动，那次确认就白问了。
    """
    from datetime import date, timedelta

    row = _first_row(db, book, statement)
    when = date.fromisoformat(row["tx_date"]) - timedelta(days=20)
    shifted = {"amount": row["amount"], "tx_date": when.isoformat(),
               "direction": row["direction"]}
    voucher = _voucher_on_cash(db, book, admin_user, subjects, shifted, other_cash)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    result = service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    assert result["from_subject"]["code"] == "100202"
    assert result["to_subject"]["code"] == "100201"


def test_不能改不是现金科目的分录(statement, db, book, admin_user, subjects, other_cash):
    """对方侧那条不是现金科目 —— 改它跟「钱记错账户」无关。"""
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
    other = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id,
        Entry.subject_id != other_cash.id)).first()
    db.expire_all()

    with pytest.raises(ValueError) as exc:
        service.reclassify_row_subject(db, book.id, row["id"], other.id, admin_user)
    assert "现金科目" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"


def test_不属于本账套的分录改不了(statement, db, book, admin_user, subjects, other_cash):
    """别账套的分录不许碰 —— 跨账套改分录等于把 A 公司的账改到 B 公司上。"""
    row = _first_row(db, book, statement)
    with pytest.raises(ValueError) as exc:
        service.reclassify_row_subject(db, book.id, row["id"], 999999, admin_user)
    assert "分录" in str(exc.value), f"提示没说出为什么不能改：{exc.value}"


# ----------------------------------------------------------------------
# 「已入账」这个标记的寿命
# ----------------------------------------------------------------------

def test_跨期改完之后再算清单不会退回待确认科目(statement, db, book, admin_user, subjects, other_cash):
    """改完之后**再算一次清单**，这一行必须还是「已入账」。

    回归：改科目那一步会把这一行标成已入账（``matched_voucher_id`` 指向那张
    凭证），可清单是**每次打开界面都重算**的 —— 重算时那张凭证因为日期差
    20 天（超出 7 天的去重窗口）根本扫不到，于是标记被收回，行状态从
    「已入账」退回「待确认科目」。用户明明刚确认过，看着却像没改成。

    生产实测就是这个问题：对账单 1 的 row#24（差 28 天）、row#26（差 16 天）
    两行都这样。
    """
    from datetime import date, timedelta

    row = _first_row(db, book, statement)
    when = date.fromisoformat(row["tx_date"]) - timedelta(days=20)
    shifted = {"amount": row["amount"], "tx_date": when.isoformat(),
               "direction": row["direction"]}
    voucher = _voucher_on_cash(db, book, admin_user, subjects, shifted, other_cash)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    db.expire_all()

    # **照着界面的次序来**：改完科目前端会立刻重算清单（plan-vouchers）。
    plan = service.plan_vouchers(db, book.id, statement["id"])
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["voucher_state"] == "POSTED", (
        f"改完科目又算了一次清单，状态就退回去了：{hit['voucher_state']}")
    skipped = [s for s in plan["skipped"] if s["id"] == row["id"]]
    assert skipped and skipped[0]["reason"].startswith("已入账"), (
        f"清单里没有把它标成已入账：{skipped}")


def test_同期的改完之后再算清单也还是已入账(statement, db, book, admin_user, subjects, other_cash):
    """主窗口之内的那一类也不能被重算带走 —— 同一件事的两种日期。"""
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    db.expire_all()
    service.plan_vouchers(db, book.id, statement["id"])
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["voucher_state"] == "POSTED"


def test_凭证删掉之后标记照样收回来(statement, db, book, admin_user, subjects, other_cash):
    """「已入账」这个标记是**借来的** —— 凭证没了，标记就得还回去。

    回收标记的用意是「用户把那张凭证删了之后，这一行应该重新成为候选」。
    修了「跨期候选被误收」之后，这条老路不能跟着瘫掉，否则那一行会永远
    赖在「已入账」上，再也制不出凭证。
    """
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    db.commit()
    db.expire_all()

    service.voucher_service.delete_voucher(db, voucher, admin_user)
    db.commit()
    db.expire_all()

    service.plan_vouchers(db, book.id, statement["id"])
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["voucher_state"] != "POSTED", (
        "凭证都删了，这一行还赖着「已入账」—— 它再也制不出凭证了")
    assert hit["matched_voucher_id"] is None


def test_凭证后来被挪走之后标记照样收回来(statement, db, book, admin_user, subjects, other_cash):
    """保留标记的另一个条件：**那张凭证现在还覆盖这一行**。

    用户把科目改过来之后，如果又把这笔现金挪回别的账户，那一行就不再被
    那张凭证覆盖了 —— 这时标记必须收回去，否则它会一直显示「已入账」，
    而钱其实记在别的账户上，对账时差额永远对不上。
    """
    row = _first_row(db, book, statement)
    voucher = _voucher_on_cash(db, book, admin_user, subjects, row, other_cash)
    entry = db.scalars(select(Entry).where(
        Entry.voucher_id == voucher.id).order_by(Entry.line_no)).first()
    db.expire_all()

    service.reclassify_row_subject(db, book.id, row["id"], entry.id, admin_user)
    db.commit()
    db.expire_all()

    # 又把这笔钱挪回原来那个账户（等于用户自己反悔了）。
    voucher = db.get(Voucher, voucher.id)
    entries = [{"subject_id": other_cash.id, "direction": e.direction,
                "amount": str(e.amount), "summary": e.summary or "",
                "aux_ref": e.aux_ref or {}}
               for e in sorted(voucher.entries, key=lambda x: x.line_no)]
    token = service.voucher_service.version_token(voucher.updated_at)
    service.voucher_service.update_voucher(db, voucher, admin_user,
                                           {"entries": entries},
                                           expected_version=token)
    db.commit()
    db.expire_all()

    service.plan_vouchers(db, book.id, statement["id"])
    db.expire_all()

    rows = service.list_rows(db, book.id, statement["id"])
    hit = next(r for r in rows if r["id"] == row["id"])
    assert hit["voucher_state"] != "POSTED", (
        "现金侧都被挪走了，这一行还赖着「已入账」")
    assert hit["matched_voucher_id"] is None
