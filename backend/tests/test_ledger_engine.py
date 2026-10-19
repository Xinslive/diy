"""聚合引擎。

整个系统只有这一处算余额，账簿 / 报表 / 打印全部复用它。
所以这里的每条用例都值得单独钉住 —— 引擎错了，别的模块「一起错」也会显得对。
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.ledger import queries
from app.ledger.engine import ZERO, LedgerEngine, display_side
from app.models import Entry, Subject, Voucher


def sid(subjects, code):
    return subjects[code].id


# --------------------------------------------------------------------- 基本四原语


def test_期初由period_opening给出(db, book, periods, subjects):
    from app.models import PeriodOpening

    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=sid(subjects, "1001"), amount=Decimal("200000")))
    db.commit()

    eng = LedgerEngine(db, book.id)
    for m in range(1, 13):
        assert eng.opening(sid(subjects, "1001"), periods[m].ord) == Decimal("200000"), m


def test_期末等于期初加本期发生(db, book, periods, subjects, make_voucher):
    from app.models import PeriodOpening

    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=sid(subjects, "1001"), amount=Decimal("1000")))
    db.commit()

    make_voucher("2026-01-10", dr=[("1001", 500, "收")], cr=[("5001", 500, "收")], posted=True)
    make_voucher("2026-02-10", dr=[("1001", 300, "收")], cr=[("5001", 300, "收")], posted=True)

    eng = LedgerEngine(db, book.id)
    bank = sid(subjects, "1001")

    f1 = eng.figures(bank, periods[1].ord)
    assert (f1.opening, f1.period_dr, f1.period_cr, f1.closing) == (
        Decimal("1000"), Decimal("500"), ZERO, Decimal("1500"))

    f2 = eng.figures(bank, periods[2].ord)
    assert (f2.opening, f2.period_dr, f2.closing) == (Decimal("1500"), Decimal("300"), Decimal("1800"))

    # 累计发生额跨月累加，但期末只算一次发生额（历史上这里出过 200000 -> 1980000 的 bug）
    assert f2.dr_cum == Decimal("800")


def test_未记账凭证不进账簿(db, book, periods, subjects, make_voucher):
    make_voucher("2026-01-10", dr=[("1001", 500, "x")], cr=[("5001", 500, "x")], posted=False)
    eng = LedgerEngine(db, book.id)
    assert eng.figures(sid(subjects, "1001"), periods[1].ord).closing == ZERO

    eng_all = LedgerEngine(db, book.id, include_unposted=True)
    assert eng_all.figures(sid(subjects, "1001"), periods[1].ord).closing == Decimal("500")


def test_结转损益凭证可被排除(db, book, periods, subjects, make_voucher):
    """利润表必须排除结转凭证，否则月度结转后损益永远是 0。

    断言打在 **5001 收入科目** 上，而不是银行存款：
    银行存款两张凭证方向相反，period_dr 都是 900，排除与否结果一模一样，
    根本测不出 exclude_carry_forward（早期版本就栽在这里，测试是绿的）。
    """
    make_voucher("2026-01-10", dr=[("1001", 900, "租金")], cr=[("5001", 900, "租金")], posted=True)
    # 月度结转的真实形态：借 收入 / 贷 本年利润
    make_voucher("2026-01-31",
                 dr=[("5001", 900, "结转")], cr=[("3103", 900, "结转")],
                 posted=True, source="CARRY_FORWARD")

    revenue = sid(subjects, "5001")
    normal = LedgerEngine(db, book.id).figures(revenue, periods[1].ord)
    pure = LedgerEngine(db, book.id, exclude_carry_forward=True).figures(revenue, periods[1].ord)

    # 真实业务贷记收入 900，结转凭证借记收入 900 —— 两张凭证方向相反，
    # 所以必须同时比 period_dr 和 closing 才区分得出来。
    assert normal.period_dr == Decimal("900") and normal.period_cr == Decimal("900")
    assert normal.closing == Decimal("0")         # 结转后收入科目归零
    assert pure.period_dr == Decimal("0")          # 排除结转凭证后只剩真实经营
    assert pure.period_cr == Decimal("900")
    assert pure.closing == Decimal("-900")        # 这才是利润表要用的营业收入


# ---------------------------------------------------------------------- 上卷


def test_父级科目发生额由子科目上卷(db, book, periods, subjects, make_voucher):
    """1101 短期投资是父级，它本身没有分录，余额必须来自 110101 等下级。

    父级用 1101：它是表里真实存在的借方资产父科目，方向与旧用例的
    「1002 银行存款」一致；1002 自己在 4-2-2 表里已经是末级了。
    """
    make_voucher("2026-01-10",
                 dr=[("110101", 700, "a")], cr=[("5001", 700, "a")], posted=True)

    eng = LedgerEngine(db, book.id)
    parent = sid(subjects, "1101")
    child = sid(subjects, "110101")

    assert eng.figures(parent, periods[1].ord).closing == Decimal("700")
    assert eng.figures(child, periods[1].ord).closing == Decimal("700")


def test_多级上卷(db, book, periods, subjects, make_voucher):
    """走全表唯一的三层链：分录挂在 222101 的两个末级上，祖父级 2221 要合计到。

    余额是「借正贷负」的分录口径，与科目自身的借贷方向无关 —— 这里是
    借方 800，所以 222101 与 2221 都是 +800（见 engine.Figures.closing）。
    """
    make_voucher("2026-01-10",
                 dr=[("22210101", 300, "a"), ("22210106", 500, "b")],
                 cr=[("5001", 800, "ab")], posted=True)
    eng = LedgerEngine(db, book.id)
    assert eng.figures(sid(subjects, "222101"), periods[1].ord).closing == Decimal("800")
    assert eng.figures(sid(subjects, "2221"), periods[1].ord).closing == Decimal("800")


# ---------------------------------------------------------------------- 方向


def test_负债类科目的负号表示(db, book, periods, subjects, make_voucher):
    """贷方科目余额为负（借正贷负），显示层再翻成「贷 1000」。"""
    make_voucher("2026-01-10",
                 dr=[("1001", 1000, "a")], cr=[("2001", 1000, "借入")], posted=True)
    eng = LedgerEngine(db, book.id)
    f = eng.figures(sid(subjects, "2001"), periods[1].ord)
    assert f.closing == Decimal("-1000")
    assert display_side(subjects["2001"], f.closing) == ("贷", Decimal("1000"))


def test_余额方向只看符号不看科目属性(db, book, periods, subjects, make_voucher):
    """方向必须与试算平衡的累计口径一致 —— 两者都按符号正负判定。

    借方属性科目出现贷方余额（预收货款）时，方向同样要显示「贷」，
    否则同一行会被一边算进贷方合计、一边标成借方。
    """
    make_voucher("2026-01-10",
                 dr=[("1001", 1000, "a")], cr=[("2001", 1000, "借入")], posted=True)
    # 银行存款（借方属性）净被贷记 1500，扣掉借方 1000 后净额为贷方 500
    make_voucher("2026-01-11",
                 dr=[("1122", 1500, "预收")], cr=[("1001", 1500, "预收")], posted=True)

    eng = LedgerEngine(db, book.id)
    bank = sid(subjects, "1001")
    assert display_side(subjects["1001"], eng.figures(bank, periods[1].ord).closing) == ("贷", Decimal("500"))
    assert display_side(subjects["2001"], ZERO) == ("平", Decimal("0"))

    # 与试算平衡口径一致：每行都只按符号归一边
    tree = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(db, book.id, tree, periods[1], False)
    by_id = {r["subject_id"]: r for r in rows}
    assert (by_id[bank]["direction"], by_id[bank]["closing"]) == ("贷", Decimal("500"))
    # closing 存的是绝对值，方向必须与引擎 signed 同号（这正是试算平衡的口径）
    for r in rows:
        if not r["is_leaf"]:
            continue
        signed = eng.figures(r["subject_id"], periods[1].ord).closing
        expected = "平" if signed == 0 else ("借" if signed > 0 else "贷")
        assert r["direction"] == expected, r["code"]
    assert check["closing_balanced"]


# ---------------------------------------------------------------------- 试算平衡


def test_试算平衡(db, book, periods, subjects, make_voucher):
    make_voucher("2026-01-10", dr=[("1001", 5000, "收")], cr=[("5001", 5000, "收")], posted=True)
    make_voucher("2026-01-15", dr=[("560206", 800, "水电")], cr=[("1001", 800, "水电")], posted=True)

    tree = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(db, book.id, tree, periods[1], False)

    assert check["period_balanced"]
    assert check["closing_balanced"]
    assert check["opening_balanced"]
    assert check["total_debit"] == check["total_credit"]


def test_试算平衡不重复计算父级(db, book, periods, subjects, make_voucher):
    """父级有余额但不应再和子级相加，否则合计会翻倍。"""
    make_voucher("2026-01-10",
                 dr=[("1001", 500, "a")], cr=[("5001", 500, "a")], posted=True)
    tree = queries.subject_tree(db, book.id)
    _rows, check = queries.balance_sheet_of_accounts(db, book.id, tree, periods[1], False)
    assert check["total_closing_debit"] == Decimal("500")


# ---------------------------------------------------------------------- 分录冗余列


def test_分录冗余列由触发器同步(db, book, make_voucher):
    """book_id / period_ord 是从凭证反规范化的，触发器出错会让账簿全错。"""
    v = make_voucher("2026-03-15", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    rows = db.scalars(select(Entry).where(Entry.voucher_id == v.id)).all()
    assert rows
    for e in rows:
        assert e.book_id == book.id
        assert e.period_ord == 3


def test_改凭证日期后冗余列跟着变(db, book, admin_user, make_voucher):
    from datetime import date

    from app.accounting.vouchers import update_voucher

    v = make_voucher("2026-03-15", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    assert db.scalar(select(Entry).where(Entry.voucher_id == v.id)).period_ord == 3

    update_voucher(db, v, admin_user, {"voucher_date": date(2026, 5, 20)})
    db.refresh(v)
    rows = db.scalars(select(Entry).where(Entry.voucher_id == v.id)).all()
    assert all(e.period_ord == 5 for e in rows)
