"""停用科目的历史口径（审计 P2-08）。

停用的语义是「**不让再选它**」，不是「删掉它」—— 老凭证还指着它，
账还得算得出来。这条设计意图就写在 ``api/subjects.py`` 的注释里，
但账簿与结转侧没贯彻：一堆查询硬编码 ``Subject.active.is_(True)``，
把停用科目从历史统计里整体抹掉。

最严重的一条是年度结转刷期初（``periods.py`` 里的科目集合）：
停用科目的期末余额不会被带进次年期初 —— 于是「停用」这个本该安全的
操作变成了**账务断层**，次年账凭空少一笔，而且没有任何提示。

另一个方向的反向风险同样要钉住：写路径**必须**继续拦住停用科目，
否则用户能给一个已经停用的科目记新账，这条线一松，整个口径就废了。

所以下面两组用例成对出现：读路径放开、写路径收紧。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.accounting_helpers import close_any
from app.accounting.periods import (carry_forward_profit, close_period,
                                    year_end_carry)
from app.ledger import queries
from app.ledger.engine import LedgerEngine
from app.models import Period, PeriodOpening, Subject


def _close_year(db, book, periods, admin_user):
    for m in range(1, 13):
        period = db.get(Period, periods[m].id)
        if period.locked:
            continue
        try:
            carry_forward_profit(db, book.id, period, admin_user)
        except ValueError:
            pass          # 该月没损益，跳过
        close_any(db, book, period, admin_user)


def _deactivate(db, subject):
    subject.active = False
    db.commit()


def _closing(db, book, subject_id, ord_):
    return LedgerEngine(db, book.id).figures(subject_id, ord_).closing


def _dec26_ord(db, book):
    return db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))


def _opening_of(db, book, year, month, subject_id):
    p = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == year, Period.month == month))
    o = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == p.id, PeriodOpening.subject_id == subject_id))
    return None if o is None else o.amount


# ---------------------------------------------- 核心：年度结转不能丢停用科目余额


def test_停用科目年度结转后次年期初仍有余额(db, book, periods, subjects,
                                       admin_user, make_voucher):
    """审计 P2-08 验收项：停用科目不丢失任何历史余额。

    前置条件先钉死：停用前该科目 12 月确实有 300 的借方余额。
    少了这一步，结转没带上余额也会「因为本来就是 0」而通过。
    """
    make_voucher("2026-03-10", dr=[("1002", 300, "x")],
                 cr=[("5001", 300, "x")], posted=True)
    target = subjects["1002"]
    assert _closing(db, book, target.id, _dec26_ord(db, book)) == Decimal("300"), \
        "前置条件不成立：停用前该科目应当有 300 余额"

    _deactivate(db, target)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user, from_year=2026, new_year=2027)

    # 次年期初挂在**2027 年那张科目表**的科目上（科目表每个年度各有一份），
    # 所以按 (账套, 年度, 编码) 找，不是拿 2026 年的 id 去查。
    s2027 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027,
        Subject.code == target.code))
    assert _opening_of(db, book, 2027, 1, s2027) == Decimal("300"), (
        "停用科目的期末余额没被带进次年期初 —— 次年账凭空少 300")


def test_停用科目次年账簿余额连续(db, book, periods, subjects,
                                admin_user, make_voucher):
    """不止期初快照：次年账簿算出来的期初也必须连得上。"""
    make_voucher("2026-03-10", dr=[("1002", 300, "x")],
                 cr=[("5001", 300, "x")], posted=True)
    _deactivate(db, subjects["1002"])
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user, from_year=2026, new_year=2027)

    jan27 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    # 2027 年那张表上的 1002（与 2026 的不是同一行）
    s2027 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == "1002"))
    figures = LedgerEngine(db, book.id).figures(s2027, jan27.ord)
    assert figures.opening == Decimal("300")


# ---------------------------------------------- 账簿/科目树口径


def test_科目树包含停用科目(db, book, subjects):
    """历史余额得在树上看得见，否则用户以为账丢了。"""
    _deactivate(db, subjects["1002"])
    codes = {s.code for s in queries.subject_tree(db, book.id, book.start_year)}
    assert "1002" in codes, "停用科目从科目树里消失了，历史余额在界面上查不到"


def test_账簿引擎默认科目集包含停用科目(db, book, subjects, make_voucher):
    """不指定科目集合时，引擎默认只算在用科目 —— 报表总额会偏小。"""
    make_voucher("2026-03-10", dr=[("1002", 300, "x")],
                 cr=[("5001", 300, "x")], posted=True)
    _deactivate(db, subjects["1002"])
    db.expire_all()

    figures = LedgerEngine(db, book.id).compute(None, _dec26_ord(db, book))
    assert subjects["1002"].id in figures, "引擎默认科目集漏掉了停用科目"
    assert figures[subjects["1002"].id].closing == Decimal("300")


# ---------------------------------------------- 结账校验与损益结转


def test_停用的损益科目有余额时不允许结账(db, book, periods, subjects,
                                       admin_user, make_voucher):
    """结账校验只查在用科目的话，挂着余额的停用损益科目能被直接结过去。

    后果是账结平了、期间锁了，损益却还留在科目上跨年。
    """
    make_voucher("2026-05-10", dr=[("560206", 500, "x")],
                 cr=[("1002", 500, "x")], posted=True)
    _deactivate(db, subjects["560206"])

    dec = db.get(Period, periods[12].id)
    with pytest.raises(ValueError) as e:
        close_any(db, book, dec, admin_user)
    assert "560206" in str(e.value), (
        f"报错里应当点名那个停用但仍有余额的损益科目，实际：{e}")


def test_停用的收入科目仍然参与结转损益(db, book, periods, subjects,
                                       admin_user, make_voucher):
    """结转损益的科目集合漏掉停用收入 → 收入留在科目上不结转，本年利润算错。"""
    make_voucher("2026-05-10", dr=[("1002", 800, "x")],
                 cr=[("5001", 800, "x")], posted=True)
    _deactivate(db, subjects["5001"])
    may = db.get(Period, periods[5].id)

    carry_forward_profit(db, book.id, may, admin_user)

    # 「被结转」= 本期借贷两边的发生额对冲掉，期末余额归零：
    # 原凭证把 800 记在贷方，结转凭证再借记 800 冲平。
    # 只查 period_cr 会被「结转凭证没生成」和「生成方向反了」一起放过去，
    # 所以查净额。
    figures = LedgerEngine(db, book.id, include_unposted=True).figures(
        subjects["5001"].id, may.ord)
    assert figures.closing == Decimal("0"), (
        f"停用收入科目没被结转，期末仍挂着 {figures.closing}"
        f"（借 {figures.period_dr} / 贷 {figures.period_cr}）")
    assert figures.period_dr == figures.period_cr != Decimal("0"), (
        f"结转应当生成一张对冲凭证，实际借 {figures.period_dr} / 贷 {figures.period_cr}")


# ---------------------------------------------- 期初录入：读写两个方向


def test_期初页要能看见停用科目(client, auth, db, book, periods, subjects):
    """否则年度结转刷出来的次年期初，用户在页面上根本看不到。"""
    _deactivate(db, subjects["1002"])
    p1 = db.get(Period, periods[1].id)

    r = client.get(f"/api/books/{book.id}/openings?period_id={p1.id}", headers=auth)
    assert r.status_code == 200, r.text

    codes = {s["code"] for s in r.json()["subjects"]}
    assert "1002" in codes, (
        "期初页看不到停用科目，年度结转刷出来的期初在界面上是隐形的")


def test_期初页加载再保存不会删掉停用科目的期初(client, auth, db, book, periods, subjects):
    """**这条是数据丢失**：set_openings 先删光本期的期初行再按 payload 重建。

    页面把停用科目藏起来，用户看到的载荷里自然没有它 —— 保存一次，
    年度结转刚刷过去的期初就被抹了，而且没有任何提示。
    """
    p1 = db.get(Period, periods[1].id)
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=subjects["1002"].id, amount=Decimal("100")))
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=subjects["5001"].id, amount=Decimal("-100")))
    db.commit()
    _deactivate(db, subjects["1002"])

    r = client.get(f"/api/books/{book.id}/openings?period_id={p1.id}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    # 提交 signed 而不是 amount：响应里 amount 是**绝对值**，
    # 方向在 side/signed 里（前端 OpeningBalance.vue 的 signedOf 同理）。
    # 用 amount 会把贷方余额洗成借方，一进去就「试算不平」。
    payload = [{"subject_id": s["id"], "amount": s["signed"]}
               for s in body["subjects"]]

    r2 = client.post(f"/api/books/{book.id}/periods?period_id={p1.id}",
                     json=payload, headers=auth)
    assert r2.status_code == 200, r2.text

    amt = db.scalar(select(PeriodOpening.amount).where(
        PeriodOpening.period_id == p1.id,
        PeriodOpening.subject_id == subjects["1002"].id))
    assert amt == Decimal("100"), (
        f"停用科目的期初在「加载→保存」往返后变成了 {amt}，100 不见了")


# ---------------------------------------------- 写路径：约束必须继续在


def test_停用科目不能记新凭证(client, auth, db, book, periods, subjects):
    """读路径放开 ≠ 写路径放开。停用科目的闸门一旦撤掉，这个修复就是反的。"""
    _deactivate(db, subjects["1002"])
    p = db.get(Period, periods[3].id)

    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "period_id": p.id, "voucher_date": "2026-03-15",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "50", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "50", "summary": "x"},
        ]})
    assert r.status_code == 400, f"停用科目竟然能记新凭证：{r.status_code} {r.text}"
    assert "停用" in r.text or "不存在" in r.text, r.text


def test_期初余额导出与页面口径一致(client, auth, db, book, periods, subjects):
    """页面看得见、导出看不见 → 用户核对时只会以为导出坏了。"""
    p1 = db.get(Period, periods[1].id)
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=subjects["1002"].id, amount=Decimal("100")))
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=subjects["5001"].id, amount=Decimal("-100")))
    db.commit()
    _deactivate(db, subjects["1002"])

    r = client.get(f"/api/books/{book.id}/openings.xlsx?period_id={p1.id}", headers=auth)
    assert r.status_code == 200, r.text

    import io
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.content)).active
    codes = [str(row[0].value) for row in ws.iter_rows(min_row=2) if row[0].value]
    assert "1002" in codes, f"期初导出少了停用科目：{codes}"


def test_期初余额导出再解析能认回停用科目(client, auth, db, book, periods, subjects):
    """导出→解析必须往返得回来。

    只修「导出」不修「解析」的话，会造出另一个更隐蔽的坑：文件导得出来，
    却再也导不回去，用户只能手工改表。
    """
    p1 = db.get(Period, periods[1].id)
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=subjects["1002"].id, amount=Decimal("100")))
    db.add(PeriodOpening(book_id=book.id, period_id=p1.id,
                         subject_id=subjects["5001"].id, amount=Decimal("-100")))
    db.commit()
    _deactivate(db, subjects["1002"])

    xlsx = client.get(f"/api/books/{book.id}/openings.xlsx?period_id={p1.id}",
                      headers=auth).content

    r = client.post(f"/api/books/{book.id}/openings/parse?year=2026", headers=auth,
                    files={"file": ("opening.xlsx", xlsx,
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    body = r.json()

    row = next((x for x in body["rows"] if x["code"] == "1002"), None)
    assert row is not None, f"导出的文件解析不回来，停用科目被当成不存在：{body['rows']}"
    assert not row["error"], f"停用科目被误判为错误：{row['error']}"
    assert row["subject_id"] == subjects["1002"].id
