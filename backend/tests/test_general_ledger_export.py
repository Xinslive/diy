"""总账导出的区间必须与页面一致（审计 P3-08）。

导出接口原来只认 ``period_id``，把 from/to 都当成同一期 —— 而前端页面
显示的是「起始月 ~ 结束月」这一段。于是屏幕上是一页、下载下来是全账，
数字对不上，用户只会以为导出坏了。

断言的是**导出文件里的实际内容**，不是「参数有没有传」：只查参数的话，
后端把 from_period_id 收下却不用，测试照样绿。
"""
from __future__ import annotations

import io

from openpyxl import load_workbook

from app.models import Period


def _export(client, auth, book, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/books/{book.id}/print/general-ledger.xlsx?{q}", headers=auth)
    assert r.status_code == 200, r.text
    return load_workbook(io.BytesIO(r.content)).active


def _texts(ws):
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


def _num_total(ws):
    out = 0.0
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, (int, float)):
                out += float(c.value)
    return out


def test_指定区间时只导区间内的月份(db, client, auth, book, periods, subjects, make_voucher):
    """1 月与 7 月都有凭证，导出 3~5 月不该看到这两个月。"""
    make_voucher("2026-01-10", dr=[("1002", 100, "x")], cr=[("5001", 100, "x")], posted=True)
    make_voucher("2026-07-10", dr=[("1002", 500, "x")], cr=[("5001", 500, "x")], posted=True)
    db.expire_all()

    ws = _export(client, auth, book,
                 period_id=periods[5].id, from_period_id=periods[3].id)
    blob = " ".join(_texts(ws))
    assert "2026-01" not in blob, "1 月跑到 3~5 月的区间里来了"
    assert "2026-07" not in blob, "7 月跑到 3~5 月的区间里来了"


def _span(ws):
    """表头第二行写着「{from} 至 {to}」。"""
    return str(ws.cell(row=2, column=1).value or "")


def test_表头写明的区间与传入的一致(db, client, auth, book, periods, subjects):
    """从库名能看出导出的到底是什么区间。

    不比「所有数字之和」：单月与多月两种情况下，期初/本期/期末三列里
    出现的数字集合可能恰好相同（期初 11111 + 本期 22222 = 期末 33333），
    求和相等并不能说明区间用对了。第一版就是这么写的，结果两种区间
    算出同一个数，测试差点绿。
    """
    single = _span(_export(client, auth, book,
                           period_id=periods[5].id, from_period_id=periods[5].id))
    assert "2026-05 至 2026-05" in single, f"单月区间的表头不对：{single}"

    multi = _span(_export(client, auth, book,
                          period_id=periods[5].id, from_period_id=periods[3].id))
    assert "2026-03 至 2026-05" in multi, f"多月区间的表头不对：{multi}"


def test_区间外的凭证不计入本期发生额(db, client, auth, book, periods, subjects, make_voucher):
    """5 月单月导出：1 月那笔不能出现在「本期借方」里。

    这才是「导出的数字对不对」——总账的「本期」列按区间内的分录汇总，
    区间被忽略时 1 月的钱会被算进 5 月。
    """
    make_voucher("2026-01-10", dr=[("1002", 11111, "x")], cr=[("5001", 11111, "x")], posted=True)
    make_voucher("2026-05-10", dr=[("1002", 22222, "x")], cr=[("5001", 22222, "x")], posted=True)
    db.expire_all()

    ws = _export(client, auth, book,
                 period_id=periods[5].id, from_period_id=periods[5].id)
    rows = list(ws.iter_rows(min_row=5))
    row = next((r for r in rows if r[0].value == "1002"), None)
    assert row is not None, "总账里没有 100201 这一行"

    period_debit = float(row[4].value or 0)     # 本期借方
    assert period_debit == 22222.0, (
        f"本期借方应为 5 月那笔 22222，实际 {period_debit} —— 区间外的分录被算进来了")


def test_不传起始期间时退化成单期(client, auth, db, book, periods, subjects):
    """老调用方只传 period_id，行为不能变。"""
    ws = _export(client, auth, book, period_id=periods[6].id)
    assert "2026-06" in " ".join(_texts(ws))


def test_不存在的起始期间返回404(client, auth, db, book, periods):
    r = client.get(f"/api/books/{book.id}/print/general-ledger.xlsx"
                   f"?period_id={periods[6].id}&from_period_id=999999", headers=auth)
    assert r.status_code == 404, f"不存在的起始期间应 404，实际 {r.status_code}"


def test_起始晚于结束时返回400而不是自动纠正(client, auth, db, book, periods, subjects):
    """**倒置区间必须报错，不能悄悄交换起止期。**

    以前这里是「起止选反了就交换一下继续跑」：同一组参数在余额表导出、
    总账打印、明细账导出上都是 400「起始期间不能晚于结束期间」，唯独总账
    Excel 导出给一份文件 —— 同一件事，三种结果。

    用户若在页面上看到过那句报错，改走导出却拿到文件，会以为页面报错是多余
    的；反过来，先导出、再在页面上核对时，两边对不上。
    """
    r = client.get(f"/api/books/{book.id}/print/general-ledger.xlsx"
                   f"?period_id={periods[2].id}&from_period_id={periods[8].id}",
                   headers=auth)
    assert r.status_code == 400, f"倒置区间应当回 400，实际 {r.status_code}：{r.text[:200]}"
    assert "起始期间不能晚于结束期间" in r.text, r.text[:200]
