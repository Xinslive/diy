"""科目余额表的**区间口径**（`from_period_id`）。

余额表本来是单期报表，用户要看一段连续区间时只能自己一月期一期地加 ——
加出来的数没法试算平衡，也对不上账。给它加了区间之后，最容易出错的
是这几处，所以每一条都单独钉住：

* 区间期初是**起始期的上一期期末**，不是止期自己的期初（P0-03 同款陷阱）
* 区间发生额**不能拿本年累计相减** —— 跨年就废了
* 余额表和总账问同一段区间，**答案必须一样**（同一个口径只允许一份实现）
* 屏幕、打印、导出三处**同一段区间**，不能各算各的
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ledger import queries


def _rows(db, book, to_period, from_period=None, include_unposted=True):
    subjects = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(
        db, book.id, subjects, to_period, include_unposted, from_period=from_period)
    return {r["code"]: r for r in rows}, check


def _mv(make_voucher, period, lines, summary="区间测试"):
    """在指定期间记账。lines: [(code, 借方, 贷方)]。

    走 conftest 的 make_voucher 工厂而不是自己拼 ORM：它会自动补借贷平衡、
    处理「上一期结账了本期才能记」这个前置条件。自己拼一份的话，
    这些业务规矩就得在每条用例里重新实现一遍。
    """
    dr = [(code, amt, summary) for code, d, _c in lines for amt in [d] if amt]
    cr = [(code, amt, summary) for code, _d, c in lines for amt in [c] if amt]
    day = date(period.year, period.month, 1)
    return make_voucher(day, dr=dr, cr=cr, posted=True)


def _periods(db, book):
    from sqlalchemy import select
    from app.models import Period
    return list(db.scalars(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord)).all())


# ------------------------------------------------------------------ 恒等式


def test_区间_期初加发生额等于期末(db, book, make_voucher):
    periods = _periods(db, book)
    p3, p4, p5 = periods[2], periods[3], periods[4]
    _mv(make_voucher, p3, [("1002", 1000, 0), ("4001", 0, 1000)])
    _mv(make_voucher, p4, [("1002", 2000, 0), ("4001", 0, 2000)])
    _mv(make_voucher, p5, [("1002", 500, 0), ("4001", 0, 500)])

    rows, _ = _rows(db, book, p5, from_period=p3)
    r = rows["1002"]
    assert r["opening"] == 0, "区间从首期开始时，期初就该是 0"
    assert r["period_debit"] == 3500, "区间发生额要把三段都算进去"
    assert r["closing"] == 3500


def test_区间期初等于上一期期末_不是止期期初(db, book, make_voucher):
    """P0-03 那个陷阱：期初取错时「期初 + 发生额 = 期末」**依然成立**，
    行内看着永远自洽，只有与上一期的期末交叉核对才看得出来。"""
    periods = _periods(db, book)
    p3, p5 = periods[2], periods[4]
    _mv(make_voucher, p3, [("1002", 1000, 0), ("4001", 0, 1000)])
    _mv(make_voucher, periods[3], [("1002", 2000, 0), ("4001", 0, 2000)])
    _mv(make_voucher, p4_ := periods[4], [("1002", 500, 0), ("4001", 0, 500)])

    single, _ = _rows(db, book, p4_)
    ranged, _ = _rows(db, book, p5, from_period=p3)

    # 区间期初必须正好等于起始期的上一期（p2）期末
    assert ranged["1002"]["opening"] == 0
    # 关键对照：止期自己的期初 = 上一期期末 = 3000（p3+p4 的 1000+2000），
    # 拿它当区间期初的话「3000 + 3500 = 6500」看着自洽，但 3000 那部分
    # 已经被区间发生额里又算了一遍 —— 正是 P0-03 那个错
    assert single["1002"]["opening"] == 3000
    assert ranged["1002"]["opening"] == 0
    assert ranged["1002"]["opening"] != single["1002"]["opening"]


def test_单月区间等于单期口径(db, book, make_voucher):
    """起止同一个月 = 就是那一期，不该因为多传了个 from 就变出两套数。"""
    periods = _periods(db, book)
    _mv(make_voucher, periods[2], [("1002", 700, 0), ("4001", 0, 700)])
    single, check1 = _rows(db, book, periods[2])
    ranged, check2 = _rows(db, book, periods[2], from_period=periods[2])
    assert single == ranged, "同起止的区间与单期结果不一致"
    assert check1 == check2


def test_跨年区间不能用本年累计相减(db, book, admin_user, make_voucher):
    """dr_cum 是**本年**累计，区间跨年时相减会得到乱七八糟的数。"""
    from app.accounting.periods import year_end_carry

    periods = _periods(db, book)
    last = periods[-1]
    _mv(make_voucher, last, [("1002", 400, 0), ("4001", 0, 400)])

    # 造下一年（走真实的年度结转，别手工插期间 —— 结转会重刷期初）
    # 结转要求本年**全部结账**，这里直接标 locked：走 close_period 会顺带
    # 记账并刷版本号，那不是这条用例要验的东西
    from sqlalchemy import update
    from app.models import Period
    db.execute(update(Period).where(Period.book_id == book.id, Period.year == last.year)
               .values(locked=True))
    db.commit()
    year_end_carry(db, book, admin_user, last.year, last.year + 1)
    periods = _periods(db, book)
    nxt = periods[-1]
    assert nxt.year == last.year + 1
    _mv(make_voucher, nxt, [("1002", 600, 0), ("4001", 0, 600)])

    rows, _ = _rows(db, book, nxt, from_period=last)
    r = rows["1002"]
    assert r["period_debit"] == 1000, (
        f"跨年区间发生额应是 400+600=1000，实际 {r['period_debit']} —— "
        "多半是拿本年累计相减了")
    assert r["closing"] == 1000


def test_区间也要试算平衡(db, book, make_voucher):
    periods = _periods(db, book)
    _mv(make_voucher, periods[1], [("1002", 300, 0), ("4001", 0, 300)])
    _mv(make_voucher, periods[2], [("1002", 900, 0), ("4001", 0, 900)])
    _, check = _rows(db, book, periods[3], from_period=periods[1])
    assert check["period_balanced"] is True, "区间发生额借贷不平"
    assert check["closing_balanced"] is True, "区间期末借贷不平"


# ------------------------------------------------------- 与总账同一口径


def test_余额表与总账问同一段区间_答案一样(db, book, make_voucher):
    """同一个口径只允许有一份实现。

    以前余额表和总账各写一遍区间算法，总账导出因此栽过一次
    （屏幕是区间、文件是全账，审计第 7.3 节）。现在 general_ledger
    直接委托 balance_sheet_of_accounts，这条用例就是防止它又被拆开。
    """
    periods = _periods(db, book)
    _mv(make_voucher, periods[1], [("1002", 111, 0), ("4001", 0, 111)])
    _mv(make_voucher, periods[2], [("1002", 222, 0), ("4001", 0, 222)])

    subjects = queries.subject_tree(db, book.id)
    bal, _ = queries.balance_sheet_of_accounts(
        db, book.id, subjects, periods[3], True, from_period=periods[1])
    gen = queries.general_ledger(
        db, book.id, subjects, periods[1], periods[3], True)
    assert bal == gen, "余额表与总账对同一段区间给出了不同的行"


# ------------------------------------------------------------------ HTTP 层


def test_接口带区间与不带区间都对(client, auth, db, book, make_voucher):
    periods = _periods(db, book)
    _mv(make_voucher, periods[1], [("1002", 150, 0), ("4001", 0, 150)])
    _mv(make_voucher, periods[2], [("1002", 250, 0), ("4001", 0, 250)])

    single = client.get(f"/api/books/{book.id}/ledger/balance",
                        params={"period_id": periods[2].id, "include_unposted": True},
                        headers=auth).json()
    assert single["range"] is False
    assert single["from_period"] is None

    ranged = client.get(f"/api/books/{book.id}/ledger/balance",
                        params={"period_id": periods[2].id,
                                "from_period_id": periods[1].id,
                                "include_unposted": True},
                        headers=auth).json()
    assert ranged["range"] is True
    assert ranged["from_period"] == periods[1].code
    assert ranged["period"] == periods[2].code
    one = {r["code"]: r for r in single["rows"]}["1002"]
    two = {r["code"]: r for r in ranged["rows"]}["1002"]
    assert two["period_debit"] == one["period_debit"] + 150, "区间发生额没把上一期算进去"


def test_起始晚于止期_400(client, auth, db, book):
    periods = _periods(db, book)
    r = client.get(f"/api/books/{book.id}/ledger/balance",
                   params={"period_id": periods[1].id, "from_period_id": periods[5].id},
                   headers=auth)
    assert r.status_code == 400, r.text
    assert "不能晚于" in r.text


def test_别人的期间_id_404(client, auth, book, db, admin_user, plain_user, plain_auth):
    """外账套的期间拿来当区间起点 = 越权探测，必须 404。"""
    from app.models import Book
    from app.accounting.periods import create_periods
    from app.models import UserBook

    other = Book(company_name="别家", name="别家账套", start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    db.add(UserBook(user_id=plain_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    alien = _periods(db, other)[0]

    r = client.get(f"/api/books/{book.id}/ledger/balance",
                   params={"from_period_id": alien.id}, headers=auth)
    assert r.status_code == 404, r.text


# ------------------------------------------------- 屏幕 / 打印 / 导出同一段


def test_导出与页面是同一段区间(client, auth, db, book, make_voucher):
    """屏幕上一段区间、纸上单期 —— 总账栽过一次，用户只会以为纸上那份是对的。"""
    periods = _periods(db, book)
    _mv(make_voucher, periods[1], [("1002", 640, 0), ("4001", 0, 640)])
    _mv(make_voucher, periods[2], [("1002", 360, 0), ("4001", 0, 360)])

    ranged = client.get(f"/api/books/{book.id}/ledger/balance",
                        params={"period_id": periods[2].id,
                                "from_period_id": periods[1].id,
                                "include_unposted": True}, headers=auth).json()
    total = next(r for r in ranged["rows"] if r["code"] == "1002")["period_debit"]
    assert total == 1000

    r = client.get(f"/api/books/{book.id}/print/balance.xlsx",
                   params={"period_id": periods[2].id, "from_period_id": periods[1].id,
                           "include_unposted": True}, headers=auth)
    assert r.status_code == 200, r.text
    # 文件名里要说清是跨段的，避免一个月一份的命名习惯盖住这件事
    assert f"{periods[1].code}" in r.headers["Content-Disposition"]
    assert f"{periods[2].code}" in r.headers["Content-Disposition"]


def test_打印预览也吃区间(client, auth, db, book, make_voucher):
    periods = _periods(db, book)
    _mv(make_voucher, periods[1], [("1002", 800, 0), ("4001", 0, 800)])
    _mv(make_voucher, periods[2], [("1002", 200, 0), ("4001", 0, 200)])

    r = client.get(f"/api/books/{book.id}/print/balance.html",
                   params={"period_id": periods[2].id, "from_period_id": periods[1].id,
                           "include_unposted": True}, headers=auth)
    assert r.status_code == 200, r.text
    # 区间发生额 1000 必须在纸上；单期只有 200
    assert "1000" in r.text or "1,000" in r.text, "打印页上没有区间合计"
