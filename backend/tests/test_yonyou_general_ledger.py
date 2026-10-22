"""总账（用友式）的行结构、数字算法与打印版式。

**为什么这组用例盯的是「用友逐格对齐」**：公司现在用的是用友，迁到本系统
之后，用户与审计方认的是用友那张纸。总账一旦与用友对不上，迁移就是一次
「数字打架」—— 而数字对不上的原因通常不是算错，是**口径/版式不同**
（用友的总账是「一个一级科目一份、按月展开」，不是「一行一个科目」）。

所以这里断言的不只是「有数」，还有：

* 行型与行序（上年结转 / 本月合计 / 累计 / 本年累计 / 结转下年）；
* 每行的**字段占用**（累计行不写方向与余额，期初行不写借贷方）；
* 「期初 + 累计借 − 累计贷 = 期末」这个恒等式；
* 一级科目要把它下面所有明细科目的分录**上卷**进来；
* 打印件上的页号是「当前页-该科目总页数」，两行表头，8 列。
"""
from __future__ import annotations

import io
import re
from decimal import Decimal

from openpyxl import load_workbook

from app.ledger import queries


def _ledger(db, book, subject_code, periods, from_month=1, to_month=12):
    subject = next(s for s in db.query(
        queries.Subject).filter(
            queries.Subject.book_id == book.id,
            queries.Subject.code == subject_code).all())
    return queries.general_ledger(
        db, book.id, subject, periods[from_month], periods[to_month], True)


def _rows_of(payload, kind):
    return [r for r in payload["rows"] if r["kind"] == kind]


# --------------------------------------------------------------------- 行结构


def test_整年区间是上年结转加每月两行加结转下年(db, book, periods, subjects):
    """一整年 = 1 + 12×2 + 1 = 26 行，与用友样张一致。"""
    payload = _ledger(db, book, "1001", periods)
    kinds = [r["kind"] for r in payload["rows"]]
    assert kinds[0] == "OPENING", f"第一行应是上年结转，实际 {kinds[0]}"
    assert kinds[-1] == "CLOSING", f"最后一行应是结转下年，实际 {kinds[-1]}"
    assert len(payload["rows"]) == 26, f"整年总账应是 26 行，实际 {len(payload['rows'])}"
    assert kinds.count("MONTH_TOTAL") == 12
    assert kinds.count("YTD_TOTAL") == 12


def test_行标签与用友一致(db, book, periods, subjects):
    payload = _ledger(db, book, "1001", periods)
    summaries = [r["summary"] for r in payload["rows"]]
    assert summaries[0] == "上年结转", summaries[0]
    assert summaries[-1] == "结转下年", summaries[-1]
    # 1~11 月的累计行写「累　　计」，12 月那行写「本年累计」——用友的固定写法。
    # 「累　　计」中间是**两个全角空格**（U+3000）：用友的标签按四字宽排版，
    # 普通空格只有半字宽，与样张并排看字距对不上。
    assert summaries[2] == "累　　计", repr(summaries[2])
    assert summaries[-2] == "本年累计", summaries[-2]
    assert summaries[1] == "本月合计" and summaries[3] == "本月合计"


def test_累计行不写方向与余额_期初行不写借贷方(db, book, periods, subjects):
    """**字段占用也照用友来**：累计行只有借/贷两列有数，方向与余额是空的。

    用友的样张里，方向与余额只出现在「上月结转」「本月合计」「结转下年」
    三种行上。填了就是多出来的格子 —— 与用友并排放两张纸，一眼就能看出
    不是同一张。
    """
    payload = _ledger(db, book, "1001", periods)
    for row in payload["rows"]:
        if row["kind"] == "YTD_TOTAL":
            assert row["direction"] == "", "累计行不该有方向"
            assert row["balance"] is None, "累计行不该有余额"
            assert row["debit"] is not None and row["credit"] is not None
        if row["kind"] == "OPENING":
            assert row["debit"] is None and row["credit"] is None, "期初行不该有借贷方"
            assert row["balance"] is not None
        if row["kind"] == "MONTH_TOTAL":
            assert row["direction"] and row["balance"] is not None


def test_月列在本月合计与累计两行都写同一个月份(db, book, periods, subjects):
    payload = _ledger(db, book, "1001", periods)
    by_month = {}
    for row in payload["rows"]:
        if row["kind"] in ("MONTH_TOTAL", "YTD_TOTAL"):
            by_month.setdefault(row["month"], []).append(row["month"])
    assert by_month["01"] == ["01", "01"], by_month.get("01")
    assert by_month["12"] == ["12", "12"], by_month.get("12")
    # 期初行与结转行不写月份
    assert payload["rows"][0]["month"] == ""
    assert payload["rows"][-1]["month"] == ""


# --------------------------------------------------------------------- 数字算法


def test_本月合计等于该月发生额_累计逐月累加(
        db, book, periods, subjects, make_voucher):
    """造两笔凭证，逐月核对。"""
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    make_voucher("2026-03-15", dr=[("1001", 50, "y")], cr=[("5001", 50, "y")],
                 posted=True)
    db.expire_all()

    payload = _ledger(db, book, "1001", periods)
    months = {r["month"]: r for r in _rows_of(payload, "MONTH_TOTAL")}
    assert months["01"]["debit"] == Decimal("100")
    assert months["01"]["credit"] == Decimal("0")
    assert months["02"]["debit"] == Decimal("0"), "2 月没有凭证"
    assert months["03"]["debit"] == Decimal("50")

    cums = {r["month"]: r for r in _rows_of(payload, "YTD_TOTAL")}
    assert cums["01"]["debit"] == Decimal("100")
    assert cums["02"]["debit"] == Decimal("100"), "2 月累计 = 1 月 + 2 月"
    assert cums["03"]["debit"] == Decimal("150")
    assert cums["12"]["debit"] == Decimal("150"), "本年累计 = 全年"


def test_期初加累计等于期末(db, book, periods, subjects, make_voucher):
    """**「期初 + 累计借 − 累计贷 = 期末」在每一行都成立。**

    这是总账这张纸的自洽性：期初行、月度合计行、结转行说的是同一个账，
    三处对不上就是印错了一页。区间不是整年时累计是区间累计，恒等式照样成立。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    make_voucher("2026-03-15", dr=[("1001", 50, "y")], cr=[("5001", 50, "y")],
                 posted=True)
    db.expire_all()

    payload = _ledger(db, book, "1001", periods)
    opening = _rows_of(payload, "OPENING")[0]
    closing = _rows_of(payload, "CLOSING")[0]
    assert opening["balance"] == Decimal("0"), "没有期初余额时是 0"
    assert closing["balance"] == Decimal("150")

    cum = _rows_of(payload, "YTD_TOTAL")[-1]
    assert (opening["balance"] + cum["debit"] - cum["credit"]) == closing["balance"]


def test_月末余额逐月递推(db, book, periods, subjects, make_voucher):
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    make_voucher("2026-03-15", dr=[("1001", 50, "y")], cr=[("5001", 50, "y")],
                 posted=True)
    db.expire_all()

    payload = _ledger(db, book, "1001", periods)
    months = _rows_of(payload, "MONTH_TOTAL")
    assert months[0]["balance"] == Decimal("100")
    assert months[1]["balance"] == Decimal("100"), "2 月没动，余额不变"
    assert months[2]["balance"] == Decimal("150")


def test_一级科目把明细科目的分录上卷进来(
        db, book, periods, subjects, make_voucher):
    """分录挂在 110101（短期投资的明细）上，一级科目 1101 的总账要算到它。

    **这是总账与明细账的根本区别**：用友的总账是「总分类账」，一个一级科目
    一份，把它下面所有明细科目的流水汇总进去。少了这一层，一级科目的总账
    就是空的 —— 而它在用友那张纸上是有数的。
    """
    make_voucher("2026-02-10", dr=[("110101", 800, "x")], cr=[("5001", 800, "x")],
                 posted=True)
    db.expire_all()

    payload = _ledger(db, book, "1101", periods)
    feb = [r for r in _rows_of(payload, "MONTH_TOTAL") if r["month"] == "02"][0]
    assert feb["debit"] == Decimal("800"), "明细科目的分录没上卷到一级科目"
    closing = _rows_of(payload, "CLOSING")[0]
    assert closing["balance"] == Decimal("800")


def test_贷方余额的方向写贷(db, book, periods, subjects, make_voucher):
    """方向看**符号**，不看科目属性（引擎口径，见 display_side）。"""
    make_voucher("2026-01-10", dr=[("5001", 100, "x")], cr=[("1001", 100, "x")],
                 posted=True)
    db.expire_all()

    payload = _ledger(db, book, "1001", periods)
    closing = _rows_of(payload, "CLOSING")[0]
    assert closing["direction"] == "贷", closing["direction"]
    assert closing["balance"] == Decimal("100"), "贷方余额存绝对值"


def test_非整年区间退回期初余额与期末余额(db, book, periods, subjects):
    """区间不是整年时，标签**不许**写「上年结转」/「结转下年」。

    写「上年结转」而实际是三月一日的期初，是拿标签骗人 —— 审计一眼就看出来。
    """
    payload = _ledger(db, book, "1001", periods, from_month=3, to_month=6)
    summaries = [r["summary"] for r in payload["rows"]]
    assert summaries[0] == "期初余额", summaries[0]
    assert summaries[-1] == "期末余额", summaries[-1]
    assert "本年累计" not in summaries
    assert len(payload["rows"]) == 1 + 4 * 2 + 1


# --------------------------------------------------------------------- 打印


def test_打印件是每页一份的用友版式(client, auth, book, periods):
    html = client.get(f"/api/books/{book.id}/print/general-ledger.html",
                       headers=auth,
                       params={"period_id": periods[12].id,
                               "from_period_id": periods[1].id}).text
    assert "总账" in html
    # 两行表头：年份横跨「月 + 日」
    assert re.search(r'colspan="2"[^>]*class="lb-year"', html), "缺少年份表头"
    assert ">月<" in html and ">日<" in html
    # 八个列头
    for label in ("凭证号数", "摘 要", "借 方", "贷 方", "余 额"):
        assert label in html, f"缺列头：{label}"
    # 「方向」列窄到 2.9%，表头必须竖排（方在上、向在下）——用友的固定写法
    assert "<th rowspan=\"2\">方<br>向</th>" in html, "方向列头没有竖排"
    # 页眉：正式账簿 / 页号 / 本币名称
    assert "[正式账簿]" in html
    assert "本币名称: 人民币" in html
    assert re.search(r"页号:\s+1-1", html), "页号应是「当前页-该科目总页数」"


def test_页号是该科目自己的总页数(client, auth, book, periods, subjects):
    """**每个一级科目各自从 1 开始编页**：1-1 = 只有一页，这是第一页。

    用友的页号不是「第几册-第几页」，而是「当前页-该科目总页数」。
    """
    html = client.get(f"/api/books/{book.id}/print/general-ledger.html",
                       headers=auth,
                       params={"period_id": periods[12].id,
                               "from_period_id": periods[1].id}).text
    # 一个一级科目一份总账，每份第一页都是 1-1
    assert html.count("页号:  1-1") >= 1
    # 不该出现「册号-页号」那种 2-1
    assert not re.search(r"页号:\s+2-1", html)


def test_每页的数据行数不超过模板设定(client, auth, book, periods):
    html = client.get(f"/api/books/{book.id}/print/general-ledger.html",
                       headers=auth,
                       params={"period_id": periods[12].id,
                               "from_period_id": periods[1].id}).text
    bodies = re.findall(r"<tbody>(.*?)</tbody>", html, re.S)
    assert bodies, "没有表体"
    for body in bodies:
        rows = re.findall(r"<tr>", body)
        assert len(rows) <= 26, f"一页 {len(rows)} 行，超过模板设定的 26 行"


def test_导出与打印是同一张纸(client, auth, book, periods):
    """Excel 导出与 HTML 打印印的是同一张纸：都是一级科目的月度台账。"""
    r = client.get(f"/api/books/{book.id}/print/general-ledger.xlsx",
                   headers=auth,
                   params={"period_id": periods[12].id,
                           "from_period_id": periods[1].id})
    assert r.status_code == 200, r.text
    wb = load_workbook(io.BytesIO(r.content))
    # 一级科目一张表
    assert len(wb.sheetnames) >= 1
    ws = wb[wb.sheetnames[0]]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    blob = " ".join(texts)
    assert "总账" in blob
    assert "上年结转" in blob, "导出件里没有「上年结转」行"
    assert "本月合计" in blob
    assert "本年累计" in blob
    assert "结转下年" in blob
    # 表头两行：年份 + 月/日
    assert any(t.endswith("年") and len(t) == 5 for t in texts), "表头缺年份"
