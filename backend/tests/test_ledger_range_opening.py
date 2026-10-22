"""区间期初 = 起始期的上一期期末（审计 P0-03）。

总账的「区间期初」以前在**起始期就是账套首期**时走了一条单独的分支，
取的是**结束期自己**的期初。于是：

    1 月借 100、2 月借 200、3 月借 300、6 月借 600（100201），全部已记账
    查询 from=2026-01, to=2026-06
      正确：期初 0     本期借方 1200  期末 1200
      实际：期初 600（5 月末）本期借方 1200 期末 1800   ← [1,5] 被算了两次

「起 1 月、止当前月」是首年最自然的选法，而总账 Excel 导出走的正是
``queries.general_ledger`` 这一个函数 —— 错误数字直接落到交付的纸上。

这个 bug 行内自洽（期初 + 本期 = 期末 恒成立），所以必须拿**绝对数字**
去钉，不能只断言「三列相加等于期末」。下面每条用例都给了具体金额。

修法不是加一个 ``if`` 绕过，而是把「账套首期之前」这个时点做成引擎的显式语义
（``LedgerEngine.compute`` 对 ``up_to_ord < 账套首期`` 返回首期的期初 + 零发生额），
区间查询两条路径合并成一条。本文件顺带把这条语义钉住。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, update

from app.ledger import queries
from app.ledger.engine import LedgerEngine
from app.models import Period, PeriodOpening, Subject


def _kind(rows, kind, month=None):
    """按**行型**取一行（可再按月份筛）。

    总账是**用友式**的：行型只有四种（上年结转 / 本月合计 / 累计 / 结转下年），
    没有「一行一个科目」那版的科目行，所以不能再按 subject_id 找行 ——
    整张表讲的本来就是**同一个科目**。
    """
    for r in rows:
        if r["kind"] == kind and (month is None or r["month"] == month):
            return r
    raise AssertionError(f"总账里没有 {kind} 行"
                         + (f"（{month} 月）" if month else ""))


def _ledger(db, book, periods, subjects, from_month, to_month, code="1001"):
    """一个**一级科目**在这段区间里的月度台账（用友式总账的行）。

    总账的单位是一级科目：它下面所有明细科目的分录由账簿引擎上卷进来，
    所以问一行一级科目就够，不必问整棵树。
    """
    subject = subjects[code]
    return queries.general_ledger(
        db, book.id, subject, periods[from_month], periods[to_month], False)["rows"]


def test_首年首月起算的区间期初必须是零(db, book, periods, subjects, make_voucher):
    """P0-03 主用例：from 就是账套首期（2026-01）。

    三个数字分别钉住，任何一个被改都会红。
    """
    for month, amt in ((1, 100), (2, 200), (3, 300), (6, 600)):
        make_voucher(f"2026-{month:02d}-10",
                     dr=[("1001", amt, "收款")], cr=[("5001", amt, "收款")],
                     posted=True)

    rows = _ledger(db, book, periods, subjects, 1, 6)
    opening = _kind(rows, "OPENING")
    cum = _kind(rows, "YTD_TOTAL", "06")
    closing = _kind(rows, "CLOSING")

    assert opening["balance"] == Decimal("0"), (
        f"区间期初应为账套首期的期初 0，实际 {opening['balance']}"
        "（取成了结束期自己的期初，即 5 月末的 600）")
    assert cum["debit"] == Decimal("1200")
    assert closing["balance"] == Decimal("1200"), (
        f"期末应为 1200，实际 {closing['balance']}（期初重复计入了 1~5 月的发生额）")

    # 行内自洽不是正确性的证据，所以再钉一条：区间期初必须等于「上一期期末」，
    # 而起始期是首期时不存在上一期 —— 它必须等于账套首期的期初快照。
    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["1001"].id, periods[1].ord) == Decimal("0")


def test_首年起算时父级科目也不重复计入(db, book, periods, subjects, make_voucher):
    """父级 1101 短期投资同样受这条分支影响（上卷后的期初也是错的）。

    父级取 1101 而不是 1002：4-2-2 表里 1002 已是末级，压根没有下级可卷。
    """
    for month, amt in ((1, 100), (2, 200), (3, 300), (6, 600)):
        make_voucher(f"2026-{month:02d}-10",
                     dr=[("110101", amt, "收款")], cr=[("5001", amt, "收款")],
                     posted=True)

    rows = _ledger(db, book, periods, subjects, 1, 6, code="1101")
    opening = _kind(rows, "OPENING")
    cum = _kind(rows, "YTD_TOTAL", "06")
    closing = _kind(rows, "CLOSING")
    assert (opening["balance"], cum["debit"], closing["balance"]) == (
        Decimal("0"), Decimal("1200"), Decimal("1200"))


def test_区间期初等于上一期期末(db, book, periods, subjects, make_voucher):
    """交叉核对：1~6 月的区间期初必须等于 5 月的期末余额。

    这是用户实际的对账方式，也是这个 bug 唯一能暴露的地方
    —— 同一行里期初 + 本期 = 期末 恒成立，看不出破绽。
    """
    for month, amt in ((1, 100), (2, 200), (3, 300), (5, 400), (6, 600)):
        make_voucher(f"2026-{month:02d}-10",
                     dr=[("1001", amt, "收款")], cr=[("5001", amt, "收款")],
                     posted=True)

    eng = LedgerEngine.for_ledger(db, book.id)
    bank = subjects["1001"].id
    may_closing = eng.closing(bank, periods[5].ord)
    assert may_closing == Decimal("1000")

    rows = _ledger(db, book, periods, subjects, 6, 6)
    assert _kind(rows, "OPENING")["balance"] == may_closing == Decimal("1000")


def test_非首期的区间期初行为不变(db, book, periods, subjects, make_voucher):
    """从 3 月起算：期初 = 2 月末，走的是本来就走对的那条路径，别被改坏。"""
    for month, amt in ((1, 100), (2, 200), (3, 300), (6, 600)):
        make_voucher(f"2026-{month:02d}-10",
                     dr=[("1001", amt, "收款")], cr=[("5001", amt, "收款")],
                     posted=True)

    rows = _ledger(db, book, periods, subjects, 3, 6)
    opening = _kind(rows, "OPENING")
    cum = _kind(rows, "YTD_TOTAL", "06")
    closing = _kind(rows, "CLOSING")
    assert (opening["balance"], cum["debit"], closing["balance"]) == (
        Decimal("300"), Decimal("900"), Decimal("1200"))


def test_首期有期初余额时区间期初等于那张快照(db, book, periods, subjects, make_voucher):
    """启用月录了期初：区间期初必须等于**首期的期初快照**，不是 0、也不是末了的期初。"""
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("5000")))
    db.commit()
    make_voucher("2026-03-10", dr=[("1001", 300, "x")], cr=[("5001", 300, "x")],
                 posted=True)

    rows = _ledger(db, book, periods, subjects, 1, 6)
    opening = _kind(rows, "OPENING")
    cum = _kind(rows, "YTD_TOTAL", "06")
    closing = _kind(rows, "CLOSING")
    assert (opening["balance"], cum["debit"], closing["balance"]) == (
        Decimal("5000"), Decimal("300"), Decimal("5300"))


def test_区间期末等于结束期本身的期末(db, book, periods, subjects, make_voucher):
    """两条独立路径必须给出同一个期末：区间口径（期初 + 区间发生额）与引擎单期口径。

    这是最强的一条交叉核对 —— 它对 from 有没有落在首期都成立，
    任何一侧把区间弄错（多算、少算）都会红。

    凭证必须按期间顺序建：夹具在记账时会把之前的期间标成已结账，
    往已结账的月份补凭证会被数据库闸门挡下。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "收款")],
                 cr=[("5001", 100, "收款")], posted=True)
    make_voucher("2026-02-10", dr=[("1001", 200, "收款")],
                 cr=[("5001", 200, "收款")], posted=True)
    make_voucher("2026-03-10", dr=[("1001", 300, "收款")],
                 cr=[("5001", 300, "收款")], posted=True)
    # 4 月：银行存款被**贷方**记一笔，借贷两侧都有发生额，期末 950
    make_voucher("2026-04-10", dr=[("2001", 250, "借入")], cr=[("1001", 250, "还")],
                 posted=True)
    make_voucher("2026-06-10", dr=[("1001", 600, "收款")],
                 cr=[("5001", 600, "收款")], posted=True)

    eng = LedgerEngine.for_ledger(db, book.id)
    tree = queries.subject_tree(db, book.id, book.start_year)
    top = [s for s in tree if s.level == 1]
    for from_month in (1, 3, 6):
        for subject in top:
            rows = _ledger(db, book, periods, subjects, from_month, 6, code=subject.code)
            closing = _kind(rows, "CLOSING")
            amount = Decimal(str(closing["balance"]))
            signed = (amount if closing["direction"] == "借"
                      else -amount if closing["direction"] == "贷" else Decimal("0"))
            assert signed == eng.closing(subject.id, periods[6].ord), (
                f"{subject.code} 从 {from_month} 月起算的区间期末 {signed}"
                f" 与引擎算的 6 月期末 {eng.closing(subject.id, periods[6].ord)} 不一致")


def test_首期启用月不是一月时同样成立(db, admin_user):
    """账套 4 月启用：首期 ord=4，起始期取 4 月时 from-1 = 3，落在首期**之前**。

    这正是「from_ord - 1 小于账套首期 ord」的那条边界：
    引擎必须给出「账套首期期初」，而不是回退成别的数、更不能报错。
    """
    import uuid

    from app.accounting.periods import create_periods
    from app.accounting.vouchers import create_voucher, post_vouchers
    from app.models import Book, Subject, UserBook
    from app.seed.chart_of_accounts import install_chart

    tag = uuid.uuid4().hex[:8]
    book = Book(company_name=f"四月启用{tag}", name=f"账套{tag}",
                start_year=2026, start_month=4)
    db.add(book)
    db.flush()
    create_periods(db, book)
    install_chart(db, book.id)
    db.add(UserBook(user_id=admin_user.id, book_id=book.id, role="OWNER"))
    db.commit()

    subs = {s.code: s.id for s in db.scalars(
        select(Subject).where(Subject.book_id == book.id)).all()}
    by_month = {p.month: p for p in db.scalars(
        select(Period).where(Period.book_id == book.id)).all()}

    def voucher(day: str, amt: int):
        v = create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=date.fromisoformat(day),
            entries=[
                {"subject_id": subs["1001"], "direction": "DEBIT",
                 "amount": str(amt), "summary": "收款"},
                {"subject_id": subs["5001"], "direction": "CREDIT",
                 "amount": str(amt), "summary": "收款"},
            ],
            source="MANUAL", commit=True,
        )
        # 上一期要先结账才能记账（测试里直接标 locked，不走结账流程）
        ord_ = next(p.ord for p in by_month.values() if p.id == v.period_id)
        db.execute(
            update(Period).where(
                Period.book_id == book.id,
                Period.ord < ord_,
                Period.locked.is_(False),
            ).values(locked=True)
        )
        db.commit()
        post_vouchers(db, book.id, [v.id], admin_user)

    voucher("2026-04-10", 100)
    voucher("2026-06-10", 200)

    tree = queries.subject_tree(db, book.id, book.start_year)
    subject = next(s for s in tree if s.code == "1001")
    rows = queries.general_ledger(
        db, book.id, subject, by_month[4], by_month[6], False)["rows"]
    opening = _kind(rows, "OPENING")
    cum = _kind(rows, "YTD_TOTAL", "06")
    closing = _kind(rows, "CLOSING")
    assert (opening["balance"], cum["debit"], closing["balance"]) == (
        Decimal("0"), Decimal("300"), Decimal("300"))

    # 引擎侧：显式的「首期之前」时点
    eng = LedgerEngine.for_ledger(db, book.id)
    before = eng.compute([subs["1001"]], by_month[4].ord - 1)[subs["1001"]]
    assert before.opening == Decimal("0")
    assert (before.period_dr, before.period_cr, before.dr_cum, before.cr_cum) == (
        Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
    assert before.closing == eng.opening(subs["1001"], 4)


def test_首期之前这张图带着首期的期初余额(db, book, periods, subjects):
    """首期之前不是「全零」：首期的期初快照必须出现在那一张图的 opening/closing 上。"""
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("8800")))
    db.commit()

    eng = LedgerEngine.for_ledger(db, book.id)
    sid = subjects["1001"].id
    before = eng.compute([sid], 0)[sid]
    assert before.opening == Decimal("8800")
    assert before.closing == Decimal("8800")     # 首期之前没有任何发生额
    # 与首期的期初一致：区间查询拿 from-1 的 closing 当作首期的期初
    assert before.closing == eng.opening(sid, 1)
