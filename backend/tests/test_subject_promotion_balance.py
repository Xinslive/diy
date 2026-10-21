"""「有分录的科目被升为汇总级」不能丢余额（审计 P1-1）。

**失效链路。** 建子科目 / 改父级过去只做归属与环路校验，随后把父科目
标成 ``is_leaf=False``；而年度结转只取**末级**科目写次年期初。于是：

    100201 银行存款-工行  1–5 月已记账借记 1000
    6 月在 100201 下建子科目 10020101
    年末点年度结转
    → 100201 非末级被排除，10020101 期末为 0 被跳过
    → 次年 100201 期初 = 0，1000 元凭空消失，零提示

这与 ``periods.py`` 里已修掉的「停用科目导致账务断层」是同一类缺陷，
只是触发条件从 ``active`` 换成了 ``is_leaf``。而 ``is_leaf`` 恰恰是
服务端派生的字段 —— 派生化把「能不能升格」的判断责任从人工交给了系统，
却没补上这条护栏。

**双保险。** 两层都要有，任何一层单独存在都不够：

1. **写入侧闸门**：建子科目 / 改父级时，父科目若已有分录或期初余额就拒绝
   —— 让用户先把余额下沉到子科目（fail-closed：余额连续性优先于便利）。
2. **结转侧兜底**：年度结转按「有余额的科目」而非「末级科目」选取，
   并按子树聚合算金额 —— 这样**历史数据里已经发生的升格也能被救回来**。

第 2 条里的「不重复计数」是它成立的前提：``engine._openings`` 读期初时做的是
``自身快照 + 所有下级快照之和``（subtree_total），父级和子级各写一条同额快照，
次年读出来就会翻倍。所以写快照时必须写**自身余额**
（``closing(自身) - Σ closing(直接下级)``），见 ``_own_balances``。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accounting.periods import (carry_forward_profit, close_period,
                                    year_end_carry)
from app.ledger.engine import LedgerEngine
from app.models import Entry, Period, PeriodOpening, Subject
from tests.accounting_helpers import close_any

ZERO = Decimal("0")


def _close_year(db, book, periods, admin_user):
    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        if p.locked:
            continue
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass
        close_any(db, book, p, admin_user)


def _payload(**kw):
    base = {"code": "10020101", "name": "工行-基本户", "category": "ASSET",
            "direction": "DEBIT"}
    base.update(kw)
    return base


def _sid_2027(db, book, subject_or_code) -> int:
    """按**编码**找 2027 年那张科目表上的对应科目。

    科目表每个年度各有一份（迁移 0032）：2026 的 1001 与 2027 的 1001
    是两行不同的记录、两个 id —— 拿 2026 的 id 去读 2027 年的期初，
    读到的永远是「没有这一行」。

    入参可以是 2026 年的科目对象、它的 id，或者编码本身。
    """
    if isinstance(subject_or_code, int):
        code = db.scalar(select(Subject.code).where(Subject.id == subject_or_code))
    elif isinstance(subject_or_code, str):
        code = subject_or_code
    else:
        code = subject_or_code.code
    return db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == code))


def _opening_2027(db, book, subject_id) -> Decimal:
    """2027 年 1 月该科目的期初 —— 读的是引擎口径（自身 + 子树），不是单行快照。"""
    row = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    eng = LedgerEngine(db, book.id)
    return eng.figures(subject_id, row.ord).opening


def _closing_2026(db, book, subject_id) -> Decimal:
    row = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))
    return LedgerEngine(db, book.id).figures(subject_id, row.ord).closing


def _make_child_directly(db, book, parent_id, code="10020101", name="工行-基本户",
                         commit=True) -> int:
    """绕开接口直接挂子科目 —— 用来构造**历史数据里已经发生的升格**。

    写入侧闸门拦的是「今后」，存量库里那些升格是在闸门存在之前发生的，
    结转侧必须能自己救回来，否则这笔钱永远回不来。
    """
    s = Subject(
        # 年度必填：科目表每个年度各有一份（迁移 0032），
        # 父节点也在同一张表上（库层护栏 fn_subject_parent_year_guard）
        book_id=book.id, year=book.start_year,
        code=code, name=name, category="ASSET", direction="DEBIT",
        parent_id=parent_id, level=3, is_leaf=True, carry_forward=False,
        qty_calc=False, aux_calc=False, fx_calc=False, sort_no=0,
        active=True, ext_attrs={},
    )
    db.add(s)
    parent = db.get(Subject, parent_id)
    parent.is_leaf = False
    if commit:
        db.commit()
    db.refresh(s)
    return s.id


# --------------------------------------------------------------------- 写入侧闸门


def test_父科目有分录时不能建子科目(client, auth, db, book, subjects, make_voucher):
    """闸门的第一半：父科目自身挂着分录，就不许在它下面长出子科目。"""
    bank = subjects["1001"]
    make_voucher("2026-01-10", dr=[("1001", 1000, "存入")],
                 cr=[("5001", 1000, "收入")], posted=True)
    db.expire_all()

    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(parent_id=bank.id))
    assert r.status_code == 400, f"应当拒绝，实际 {r.status_code}：{r.text}"
    msg = r.json()["detail"]
    # 错误信息要能指导用户，而不是只说「不允许」
    assert "1001" in msg and "分录" in msg, msg
    assert "改挂" in msg, f"没告诉用户该怎么处理：{msg}"
    db.expire_all()
    assert db.get(Subject, bank.id).is_leaf is True, "被拒之后父科目不该变成汇总级"


def test_父科目有期初余额时也不能建子科目(client, auth, db, book, subjects, periods):
    """闸门的第二半：期初也算「它自己有余额」。

    年度结转写下的期初**大量处于「有余额、无分录」状态**，
    只查 Entry 会漏掉它。
    """
    bank = subjects["1001"]
    p1 = periods[1]
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=bank.id, amount=Decimal("500")))
    db.commit()

    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(parent_id=bank.id))
    assert r.status_code == 400, f"应当拒绝，实际 {r.status_code}：{r.text}"
    assert "期初" in r.json()["detail"], r.text


def test_零额期初不算余额(client, auth, db, book, subjects, periods):
    """零额期初行不构成余额 —— 否则系统自己写出来的 0 也会把闸门堵死。"""
    bank = subjects["1001"]
    p1 = periods[1]
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=bank.id, amount=Decimal("0")))
    db.commit()

    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(parent_id=bank.id))
    assert r.status_code == 201, f"不该被零额期初挡住：{r.text} {r.json()}"


def test_父科目没有余额时正常建子科目(client, auth, db, book, subjects):
    """闸门不能变成拦路虎：干净科目下面照常建子科目。"""
    bank = subjects["1001"]
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(parent_id=bank.id))
    assert r.status_code == 201, r.text
    db.expire_all()
    assert db.get(Subject, bank.id).is_leaf is False, "父科目应当变成汇总级"


def test_改父级时父科目有分录被拒(client, auth, db, book, subjects, make_voucher):
    """改父级是同一个洞的另一半：把有余额的科目挂到别人下面也一样丢余额。"""
    make_voucher("2026-01-10", dr=[("1001", 1000, "存入")],
                 cr=[("5001", 1000, "收入")], posted=True)
    child = _make_child_directly(db, book, subjects["1002"].id, code="100299",
                                 name="待挂科目")
    db.expire_all()
    bank = db.get(Subject, subjects["1001"].id)

    r = client.patch(
        f"/api/books/{book.id}/subjects/{child}", headers=auth,
        json=_payload(code="100299", name="待挂科目", category="ASSET",
                      direction="DEBIT", parent_id=bank.id),
    )
    assert r.status_code == 400, f"应当拒绝，实际 {r.status_code}：{r.text}"
    db.expire_all()
    assert db.get(Subject, child).parent_id == subjects["1002"].id, "被拒之后不该改父"


def test_把有分录的叶子科目改挂到父下也被拒(client, auth, db, book, subjects,
                                       make_voucher):
    """被移动的这一方也会因此从末级变成汇总级 —— 同样要挡。

    用一个**新建成的一级科目**当被移动方：种子科目表里的 100201 本来就挂在
    1002 下面，把它 PATCH 成 1002 的下级等于什么都没改，测不到任何东西。
    """
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(code="1900", name="新一级科目"))
    assert r.status_code == 201, r.text
    sid_new = r.json()["id"]
    db.expire_all()
    make_voucher("2026-01-10", dr=[(sid_new, 1000, "记在这条一级科目上")],
                 cr=[("5001", 1000, "收入")], posted=True)
    db.expire_all()
    new_subject = db.get(Subject, sid_new)
    assert new_subject.parent_id is None and new_subject.is_leaf is True, "前置"

    r = client.patch(
        f"/api/books/{book.id}/subjects/{sid_new}", headers=auth,
        json=_payload(code="1900", name=new_subject.name, category="ASSET",
                      direction="DEBIT", parent_id=subjects["1002"].id),
    )
    assert r.status_code == 400, f"应当拒绝，实际 {r.status_code}：{r.text}"
    assert "1900" in r.json()["detail"], r.json()["detail"]
    db.expire_all()
    assert db.get(Subject, sid_new).parent_id is None, "被拒之后不该改父"


# --------------------------------------------------------------------- 结转侧兜底


def test_端到端_历史升格的余额被结转到次年(
    db, book, periods, subjects, admin_user, make_voucher
):
    """P1-1 的核心验收点：1000 元不许在年度结转里凭空消失。"""
    bank = subjects["1001"]
    make_voucher("2026-05-10", dr=[("1001", 1000, "存入")],
                 cr=[("5001", 1000, "收入")], posted=True)
    # 6 月在它下面挂了个子科目并录了 0 期初 —— 存量数据里就是这样
    child_id = _make_child_directly(db, book, bank.id)
    db.expire_all()
    assert db.get(Subject, bank.id).is_leaf is False, "前置：父科目确实已升格"

    _close_year(db, book, periods, admin_user)
    r = year_end_carry(db, book, admin_user, 2026, 2027)
    assert r["new_year"] == 2027

    db.expire_all()
    opening = _opening_2027(db, book, _sid_2027(db, book, bank))
    assert opening == Decimal("1000"), (
        f"次年 100201 期初丢了余额：{opening}（应为 1000）"
        " —— 这就是 P1-1 的账务断层")
    assert _opening_2027(db, book, _sid_2027(db, book, child_id)) == ZERO, "子科目本身没有余额"
    # 总额守恒：借方那一侧一分不多一分不少
    assert _closing_2026(db, book, bank.id) == Decimal("1000")
    assert (_opening_2027(db, book, _sid_2027(db, book, bank))
            == _closing_2026(db, book, bank.id))


def test_端到端_父与子都有余额时总额守恒且不重复计数(
    db, book, periods, subjects, admin_user, make_voucher
):
    """父级快照必须是**子树合计**，既不能只记自己那份，也不能让父子算两遍。

    ``engine._roll_up_openings`` 读期初的口径是「自身快照是基准总额，
    下级只补增量（子树期初 − 下级自身快照）」。于是：

      父级快照写 1300（子树合计）+ 子级快照写 300 → 父级读成 1300 ✓
      父级快照写 1000（只有自己那份）→ 父级读成 1000，把子级的 300 弄丢了 ✗
      父子各写「自己的增量」1000 / 300 → 引擎认为两者都是总额，父级读成 1000 ✗

    写重了和写漏了同样要命：余额翻倍与余额消失，从账面上看是同一件事 ——
    「下年期初 ≠ 上年期末」。
    """
    bank = subjects["1001"]
    # 顺序要紧：父级自己的分录必须发生在「挂子科目」之前 ——
    # 挂上之后它就不是末级了，凭证校验会拒收挂到它上面的新分录。
    make_voucher("2026-03-10", dr=[("1001", 1000, "父级历史分录")],
                 cr=[("5001", 1000, "收入")], posted=True)
    child_id = _make_child_directly(db, book, bank.id)
    make_voucher("2026-04-10", dr=[(child_id, 300, "子级分录")],
                 cr=[("5401", 300, "成本")], posted=True)

    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()

    assert _closing_2026(db, book, bank.id) == Decimal("1300")
    assert _opening_2027(db, book, _sid_2027(db, book, bank)) == Decimal("1300"), (
        "父科目次年期初应当等于子树合计 1000+300，实际 "
        f"{_opening_2027(db, book, _sid_2027(db, book, bank))} —— 写重了或写漏了")
    assert _opening_2027(db, book, _sid_2027(db, book, child_id)) == Decimal("300")
    # 快照行本身：父级写的是子树合计 1300
    jan27 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    own = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id,
        PeriodOpening.subject_id == _sid_2027(db, book, bank)))
    assert own is not None and own.amount == Decimal("1300"), (
        "父级快照应当是子树合计 1300（自身快照是引擎读期初的基准总额），"
        f"实际 {own.amount if own else None}")


def test_端到端_零余额科目不写期初行(
    db, book, periods, subjects, admin_user, make_voucher
):
    """兜底不是「全科目都写一遍」：没余额的科目仍然不落行。"""
    make_voucher("2026-05-10", dr=[("1001", 1000, "存入")],
                 cr=[("5001", 1000, "收入")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    jan27 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    n = db.scalar(select(PeriodOpening).where(PeriodOpening.period_id == jan27.id)
                  .with_only_columns(__import__("sqlalchemy").func.count()))
    assert n and 0 < n < 10, f"不该把整棵科目表都写进期初，实际 {n} 行"


def test_端到端_停用科目余额照样结转(
    db, book, periods, subjects, admin_user, make_voucher
):
    """结转侧的选取条件里不能出现 ``active``：停用 ≠ 删除（ARCHITECTURE §9.6）。"""
    bank = subjects["1001"]
    make_voucher("2026-05-10", dr=[("1001", 1000, "存入")],
                 cr=[("5001", 1000, "收入")], posted=True)
    bank.active = False
    db.commit()

    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()
    assert _opening_2027(db, book, _sid_2027(db, book, bank)) == Decimal("1000"), (
        "停用科目的期末余额没被结转过去 —— 结转选取里混进了 active 过滤")


def test_升格科目上的分录仍然可查(db, book, subjects, admin_user, make_voucher):
    """升格本身没被禁止（那是存量数据），要保证这类账仍然查得到数。"""
    bank = subjects["1001"]
    make_voucher("2026-05-10", dr=[("1001", 1000, "存入")],
                 cr=[("5001", 1000, "收入")], posted=True)
    _make_child_directly(db, book, bank.id)
    db.expire_all()
    n = db.scalar(select(Entry).where(
        Entry.subject_id == bank.id).with_only_columns(
        __import__("sqlalchemy").func.count()))
    assert n == 1, "分录不该被升格动作改动"