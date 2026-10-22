"""明细账（用友式）的成册、行结构与打印版式。

**为什么总账做完了还要做明细账**：用友的「明细账」是**明细分类账** ——
把分录**逐笔**记上去；版式与总账**逐格相同**，差别只在「日 / 凭证号数 /
凭证摘要」这三列填的是真值，而总账那三列全程空白（总账只印汇总行）。

所以这组用例盯的是：

* 打印件与总账是**同一套版式**（两行表头 / 八列 / 页眉页脚 / 页码口径）；
* 明细账那三列有值、总账那三列没值；
* **成册**：一本账 = 全账套的**一级科目**连续排下来（用户 2026-10-22 批注 3，
  与总账同一口径），一级科目下面有下级时那一份是**综合明细账**；
* **无发生额的科目不打印**（默认开，开关是**打印时**的参数，不是模板字段）；
* **打印日期可以在打印预览里指定**，不是每次都自动取当天；
* Excel 导出与 HTML 打印是**同一张纸**。
"""
from __future__ import annotations

import io
import re

from openpyxl import load_workbook

from app.ledger import queries


def _book_html(client, auth, book, periods, **params):
    query = {"period_id": periods[12].id, "from_period_id": periods[1].id}
    query.update(params)
    r = client.get(f"/api/books/{book.id}/print/detail-ledger.html",
                   headers=auth, params=query)
    assert r.status_code == 200, r.text
    return r.text


def _groups(db, book, year=None):
    """这个账套的册页单位（一级科目逐个）。"""
    return queries.detail_ledger_groups(db, book.id, year or book.start_year)


# --------------------------------------------------------------------- 版式


def test_明细账与总账是同一套用友版式(client, auth, book, periods):
    """两行表头 + 八列 + 页眉页脚 + 「当前页-该科目总页数」的页号。"""
    html = _book_html(client, auth, book, periods)
    # 两行表头：年份横跨「月 + 日」
    assert re.search(r'colspan="2"[^>]*class="lb-year"', html), "缺少年份表头"
    assert ">月<" in html and ">日<" in html
    # 八个列头（与总账同一套）。标签不带全角空格 —— 基线模板是用户自己
    # 调出来的那份，写法就是「摘要 / 借方 / 贷方 / 余额」。
    for label in ("凭证号数", "摘要", "借方", "贷方", "余额"):
        assert label in html, f"缺列头：{label}"
    # 「方向」列的表头在**这一份基线模板里是横排**（用户调总账那份时改成
    # 了竖排，明细账这份没改）。两份模板各按各的来，渲染层不替用户改。
    assert "<th rowspan=\"2\">方向</th>" in html, "方向列头不对"
    # 页眉：页号 / 本币名称
    assert "本币名称: 人民币" in html
    assert re.search(r"页号:\s+1-1", html), "页号应是「当前页-该科目总页数」"
    # 页脚。**中段是空的** —— 用户自己调出来的那份基线模板把「页脚中段」
    # 清掉了（默认是「制表: {preparer}」）。渲染层照模板印，不替用户补。
    assert "核算单位:" in html and "打印日期:" in html
    assert "制表:" not in html, "页脚中段被清空了，不该凭空印出「制表:」"
    # 右上角那个用友账簿标记**不印**（用户 2026-10-22 批注 1）
    assert "[正式账簿]" not in html
    assert '<div class="lb-book">' not in html


def test_明细账逐笔行有日凭证号与摘要_总账那三列是空的(
        client, auth, book, periods, make_voucher):
    """**这正是明细账与总账的全部差别**：日、凭证号数、凭证摘要。

    总账只印汇总行，那三列没有内容可填；明细账逐笔记上去，三列都有真值。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "现金收入")], cr=[("5001", 100, "x")],
                 posted=True)

    detail = _book_html(client, auth, book, periods)
    assert "现金收入" in detail, "明细账的凭证摘要没印出来"
    assert "记0001" in detail, "明细账的凭证号没印出来"

    ledger = client.get(f"/api/books/{book.id}/print/general-ledger.html",
                         headers=auth,
                         params={"period_id": periods[12].id,
                                 "from_period_id": periods[1].id}).text
    assert "现金收入" not in ledger, "总账只印汇总行，不该出现逐笔摘要"
    # 总账那张纸上「日」「凭证号数」两列全程空白
    bodies = re.findall(r"<tbody>(.*?)</tbody>", ledger, re.S)
    for body in bodies:
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            assert len(cells) == 8
            assert cells[0] == "" or cells[0].isdigit()
            assert cells[1] == "", "总账的「日」列不该有内容"
            assert cells[2] == "", "总账的「凭证号数」列不该有内容"


def test_明细账逐笔行的月与日来自凭证日期(client, auth, book, periods, make_voucher):
    make_voucher("2026-03-15", dr=[("1001", 50, "y")], cr=[("5001", 50, "x")],
                 posted=True)
    html = _book_html(client, auth, book, periods)
    # 3 月那一行：月列 03、日列 15
    assert re.search(r"<td class=\"lb-ctr\">03</td><td class=\"lb-ctr\">15</td>", html), (
        "月/日两列没按凭证日期拆开")


# --------------------------------------------------------------------- 成册


def test_册页单位是一级科目(db, book):
    """用户 2026-10-22 批注 3：明细账与总账**同理**，改成一级科目。"""
    groups = _groups(db, book)
    assert groups, "标准科目表里一级科目不该是空的"
    assert all(g["subject"].level == 1 for g in groups), "册页单位不是一级科目"
    codes = {g["subject"].code for g in groups}
    assert "1001" in codes, "1001 库存现金是一级科目"
    assert "1101" in codes, "1101 短期投资是一级科目"
    assert "110101" not in codes, "110101 是二级科目，不该单独成册"


def test_有下级的一级科目是综合明细账(db, book):
    """1101 短期投资下面是 4 个末级科目 —— 那一份里排成几块。"""
    groups = {g["subject"].code: g for g in _groups(db, book)}
    group = groups["1101"]
    assert group["comprehensive"] is True
    assert [s.code for s in group["leaves"]] == ["110101", "110102", "110103", "110110"]
    # 末级的一级科目就是它自己那一份
    assert groups["1001"]["comprehensive"] is False
    assert [s.code for s in groups["1001"]["leaves"]] == ["1001"]


def test_综合明细账每块首行写明是哪个下级科目(db, book, periods, subjects):
    """纸上**没有「科目」列**（用友八列），所以块首行的摘要要说明归属。

    摘要里**只有科目名、没有编码**（用户 2026-10-22 浏览器批注 1）：
    编码是账套内部的地址，只有科目表里才认它；翻账的人看的是名字。
    """
    groups = {g["subject"].code: g for g in _groups(db, book)}
    books = queries.detail_ledger_book(
        db, book.id, [groups["1101"]], periods[1], periods[12], True)
    assert len(books) == 1, "1101 应该只成一份"
    rows = books[0]["rows"]
    opening_summaries = [r["summary"] for r in rows if r["kind"] == "OPENING"]
    assert opening_summaries == [
        "股票 期初余额", "债券 期初余额",
        "基金 期初余额", "其他 期初余额",
    ], opening_summaries
    # 册页的抬头是**一级科目**，不是某个末级科目
    assert books[0]["code"] == "1101"
    assert books[0]["name"] == "短期投资"


def test_末级一级科目的期初行仍写期初余额(db, book, periods, subjects):
    """1001 库存现金本身就是末级 —— 它的期初行不用再缀一遍科目名。"""
    groups = {g["subject"].code: g for g in _groups(db, book)}
    books = queries.detail_ledger_book(
        db, book.id, [groups["1001"]], periods[1], periods[12], True)
    opening = [r["summary"] for r in books[0]["rows"] if r["kind"] == "OPENING"]
    assert opening == ["期初余额"], opening


def test_不给科目就是整册(client, auth, book, periods, db, make_voucher):
    """一本明细账 = 全账套的**一级科目**连续排下来。"""
    groups = _groups(db, book)
    assert len(groups) > 20, "标准科目表里一级科目太少，这条用例测不到东西"
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    html = _book_html(client, auth, book, periods)
    assert "库存现金" in html
    # 1101 短期投资整段期间没动过 —— 默认跳过
    assert "短期投资" not in html, "没动过的一级科目不该成册"


def test_按科目打印只出那一个(client, auth, book, periods, subjects):
    leaf = subjects["1001"]
    html = _book_html(client, auth, book, periods, subject_id=leaf.id)
    assert "库存现金" in html
    assert "库存商品" not in html, "给了科目就不该带出别的科目"


def test_给父科目就出那一份综合明细账(client, auth, book, periods, subjects,
                                make_voucher):
    """点名要 1101 短期投资 → 出它那一份（几个下级科目排成几块）。"""
    make_voucher("2026-01-10", dr=[("110101", 100, "买股票")], cr=[("5001", 100, "x")],
                 posted=True)
    parent = subjects["1101"]
    html = _book_html(client, auth, book, periods, subject_id=parent.id)
    assert "短期投资" in html
    assert "股票 期初余额" in html
    assert "110101 股票" not in html, "摘要列不该再带科目编码"
    assert "库存现金" not in html, "给了科目就不该带出别的科目"


# --------------------------------------------------------------------- 跳过无发生额


def test_默认跳过无发生额的科目(client, auth, book, periods, subjects, make_voucher):
    """一本明细账动辄几十个一级科目，绝大多数期间内一笔没动。

    印出来是一页全是零的表 —— 那不是账簿，是废纸。所以默认跳过
    「区间内既无发生额、余额也都是零」的科目。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    html = _book_html(client, auth, book, periods)
    assert "库存现金" in html, "有发生额的科目必须在"
    assert "股票" not in html, "没动过的一级科目不该印出来"
    assert "短期投资" not in html, "没动过的一级科目不该印出来"


def test_关掉开关就全都印(client, auth, book, periods, subjects, make_voucher):
    """开关是**打印时**的参数（打印预览弹窗里那个），不是模板里的字段。"""
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    html = _book_html(client, auth, book, periods, skip_empty="false")
    assert "库存现金" in html
    assert "短期投资" in html, "关了开关之后，没动过的科目也该印"
    assert "股票 期初余额" in html
    assert "110101 股票" not in html, "摘要列不该再带科目编码"


def test_开关不在模板里_老模板里那份会被清掉(client, auth, book):
    """**同一个设置只放一处**：模板里存过的那份要被清掉。

    否则同一个开关有两个地方能改，迟早有一次是旧值 —— 而纸上少几页账
    是查不出来的。
    """
    tpl = client.post(f"/api/books/{book.id}/print/templates", headers=auth,
                      json={"scope": "DETAIL_LEDGER", "name": "明细账（老模板）",
                            "content": {"ledger_book": {"skip_empty": False}}})
    assert tpl.status_code == 201, tpl.text
    template_id = tpl.json()["id"]
    detail = client.get(f"/api/books/{book.id}/print/templates/{template_id}",
                        headers=auth).json()
    assert "skip_empty" not in (detail["content"].get("ledger_book") or {})


def test_导出也遵守同一个开关(client, auth, book, periods, make_voucher):
    """打印与导出是**同一张纸** —— 一个跳一个不跳就是两份不一样的账。"""
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    params = {"period_id": periods[12].id, "from_period_id": periods[1].id}

    skipped = client.get(f"/api/books/{book.id}/print/detail-ledger.xlsx",
                         headers=auth, params=params)
    assert skipped.status_code == 200, skipped.text
    wb = load_workbook(io.BytesIO(skipped.content))
    names = [ws.title for ws in wb.worksheets]
    assert "短期投资(1101)" not in names, names

    everything = client.get(f"/api/books/{book.id}/print/detail-ledger.xlsx",
                            headers=auth, params={**params, "skip_empty": "false"})
    assert everything.status_code == 200, everything.text
    names = [ws.title for ws in load_workbook(io.BytesIO(everything.content)).worksheets]
    assert "短期投资(1101)" in names, names


# --------------------------------------------------------------------- 打印日期


def test_打印日期可以在打印预览里指定(client, auth, book, periods):
    """用户 2026-10-22 批注 3：**不要自动取当天**，要能指定。"""
    html = _book_html(client, auth, book, periods, print_date="2026.10.22")
    assert "打印日期: 2026.10.22" in html
    # 不给就回落到当天（页脚那一格仍在）
    default = _book_html(client, auth, book, periods)
    assert "打印日期: " in default


def test_打印日期也适用于总账(client, auth, book, periods):
    html = client.get(f"/api/books/{book.id}/print/general-ledger.html",
                      headers=auth,
                      params={"period_id": periods[12].id,
                              "from_period_id": periods[1].id,
                              "print_date": "2026.10.22"}).text
    assert "打印日期: 2026.10.22" in html


def test_页脚里不漏出字面占位符(client, auth, book, periods):
    """``pv.context`` 会丢掉空值 —— 占位符没被替换就会原样印在纸上。"""
    html = _book_html(client, auth, book, periods)
    assert "{preparer}" not in html
    assert "{print_date}" not in html
    assert "{company}" not in html
    assert "{subject}" not in html


# --------------------------------------------------------------------- 导出


def test_导出与打印是同一张纸(client, auth, book, periods, subjects, make_voucher):
    """Excel 导出与 HTML 打印印的是同一张纸：一个一级科目一张表。"""
    make_voucher("2026-01-10", dr=[("1001", 100, "现金收入")], cr=[("5001", 100, "x")],
                 posted=True)
    r = client.get(f"/api/books/{book.id}/print/detail-ledger.xlsx", headers=auth,
                   params={"period_id": periods[12].id,
                           "from_period_id": periods[1].id,
                           "subject_id": subjects["1001"].id})
    assert r.status_code == 200, r.text
    wb = load_workbook(io.BytesIO(r.content))
    assert len(wb.worksheets) == 1, "只打一个科目就该只有一张表"
    ws = wb.worksheets[0]
    assert "库存现金明细账" in str(ws.cell(row=1, column=1).value)
    # 标题下面那一行是**期间**（与打印件同一张纸、同一串字）
    assert ws.cell(row=2, column=1).value == "2026-01 至 2026-12"
    # 八个列头，与打印件同一套（第 4、5 行是两行表头）
    heads = [ws.cell(row=4, column=c).value for c in range(1, 9)]
    assert heads[0] == "2026年"
    assert [ws.cell(row=5, column=c).value for c in (1, 2)] == ["月", "日"]
    assert heads[2] == "凭证号数" and heads[7] == "余额"
    # 逐笔的摘要与凭证号都在
    blob = " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value is not None)
    assert "现金收入" in blob
    assert "记0001" in blob


def test_导出的打印日期与打印件一致(client, auth, book, periods, subjects):
    r = client.get(f"/api/books/{book.id}/print/detail-ledger.xlsx", headers=auth,
                   params={"period_id": periods[12].id,
                           "from_period_id": periods[1].id,
                           "subject_id": subjects["1001"].id,
                           "print_date": "2026.10.22"})
    assert r.status_code == 200, r.text
    ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
    blob = " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value is not None)
    assert "打印日期: 2026.10.22" in blob


# --------------------------------------------------------------------- 数字口径


def test_明细账与屏幕上是同一本账(db, client, auth, book, periods, subjects, make_voucher):
    """成册里的余额必须与屏幕上那本明细账一致 —— 同一份实现，不是两套算法。"""
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    leaf = subjects["1001"]
    screen = client.get(f"/api/books/{book.id}/ledger/detail", headers=auth,
                        params={"subject_id": leaf.id,
                                "period_id": periods[12].id,
                                "from_period_id": periods[1].id}).json()
    groups = {g["subject"].code: g for g in _groups(db, book)}
    book_rows = queries.detail_ledger_book(
        db, book.id, [groups["1001"]], periods[1], periods[12], False)
    from app.printing.template import ledger_book_detail_row
    printed = [ledger_book_detail_row(r) for r in book_rows[0]["rows"]]
    screen_rows = screen["rows"]
    assert len(printed) == len(screen_rows), (
        f"成册 {len(printed)} 行，屏幕上 {len(screen_rows)} 行")
    for a, b in zip(printed, screen_rows):
        assert a["summary"] == b["summary"], (a, b)
        assert a["debit"] == b["debit"], (a, b)
        assert a["credit"] == b["credit"], (a, b)
        assert a["balance"] == b["balance"], (a, b)


def test_累计行标签与总账同一套规则(db, book, periods, subjects):
    """整年 12 月写「本年累计」，其余写「累　　计」—— 与总账一致。"""
    groups = {g["subject"].code: g for g in _groups(db, book)}
    books = queries.detail_ledger_book(
        db, book.id, [groups["1001"]], periods[1], periods[12], True)
    summaries = [r["summary"] for r in books[0]["rows"] if r["kind"] == "YTD_TOTAL"]
    assert summaries[-1] == "本年累计", summaries[-1]
    assert summaries[0] == "累　　计", repr(summaries[0])
