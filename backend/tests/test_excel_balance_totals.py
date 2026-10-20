"""科目余额表导出的**合计行必须是一份试算平衡表**。

以前 ``balance_excel`` 逐行把 ``opening`` / ``closing`` 加起来当合计。两个错
叠在一起，都在同一张交给客户和审计方的纸面上：

1. **方向被吃掉了。** 行里的余额来自 ``display_side()``，是**绝对值** —— 贷方
   余额同样存正数，靠旁边的「方向」列区分。借方余额合计 1000 + 贷方余额合计
   1000 直接相加，合计「期初」写成 **2000**，而同一张纸下方两行试算还打着
   「期末余额平衡 ✓」。一份 2000 的合计不与任何一边相等，纸上两处结论互相
   打脸，而这份文件正是用来免去加法的。
2. **重复计数。** 行是 ``filter_balance_rows`` 过滤**之后**的（仅一级 / 仅明细 /
   全部）。默认那一档是「全部」，父级行与它的明细同时在表上，父级本身又是下级
   的上卷 —— 逐行相加把同一笔钱数了两遍。``filter_balance_rows`` 的 docstring
   明说它**不动 check**，因为 check 是叶子级算的、与档位无关；合计必须与
   check 同源，否则「仅一级」和「全部」打出来的合计不一样。

另外，合计里最后一个数以前写在了**第 9 列（方向）**上，而真正的「期末」列
空着 —— 一格方向列里躺着一个金额。

这个文件钉：合计 = 叶子级、按方向归过边的试算合计；它与 ``check`` 里的八个数
（期初借/贷、本期借/贷、本年借/贷、期末借/贷）一致，且**与科目范围档位无关**。
"""
from __future__ import annotations

import io
from decimal import Decimal

from openpyxl import load_workbook

from app.ledger import queries
from app.printing.excel import balance_excel

# 列号（表头见 balance_excel）：4 期初 / 5 本期借方 / 6 本期贷方 /
# 7 本年借方 / 8 本年贷方 / 9 方向 / 10 期末
COL_OPENING, COL_PDR, COL_PCR, COL_YDR, COL_YCR, COL_DIRECTION, COL_CLOSING = 4, 5, 6, 7, 8, 9, 10


def _sheet(data: bytes):
    return load_workbook(io.BytesIO(data)).active


def _total_row(ws) -> int:
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=2).value == "合计":
            return r
    raise AssertionError(f"这份表里没有合计行（{ws.max_row} 行）")


def _total(ws, col: int) -> Decimal:
    """合计行某一列的值。``Decimal(str(...))`` 与既有导出测试同一个写法：
    openpyxl 读回的可能是 Decimal 也可能是 float，一律经 str 落地。"""
    return Decimal(str(ws.cell(row=_total_row(ws), column=col).value))


def _row(code, *, opening=0, opening_side="借", pdr=0, pcr=0, ydr=0, ycr=0,
         closing=0, closing_side="借", level=1, leaf=True) -> dict:
    """一行余额。金额一律给**绝对值** + 方向，与 queries 的输出一致。"""
    return {"code": code, "name": code, "level": level, "is_leaf": leaf,
            "opening_direction": opening_side, "opening": str(opening),
            "period_debit": str(pdr), "period_credit": str(pcr),
            "ytd_debit": str(ydr), "ytd_credit": str(ycr),
            "direction": closing_side, "closing": str(closing)}


def _payload(rows, **check) -> dict:
    """一份余额表导出入参。

    默认只给三个试算**布尔**位 —— 与 ``balance_sheet_of_accounts`` 的 check
    形状一致，也避免「check 里的数」在纯函数用例里凭空盖掉行里的数：
    合计一旦给了 check 就以 check 为准（真实链路正是这样，check 是叶子级、
    行可能已经被档位过滤过），所以用例里要么给一致的数，要么不给。
    """
    base = {"period_balanced": True, "closing_balanced": True, "opening_balanced": True}
    base.update(check)
    return {"rows": rows, "check": base, "period": "2026-01"}


# ------------------------------------------------------- ① 方向不能被吃掉


def test_合计不是借贷两侧绝对值之和():
    """借 1000 / 贷 1000 的期初，合计期初必须是 1000 —— 不是 2000。

    2000 是「把两个方向的钱堆在一起」，它不与任何一边相等；而同一张纸下方
    正打着「平衡 ✓」。这一条就是缺陷最直观的形状。
    """
    rows = [_row("1001", opening=1000, pdr=500, ydr=500, closing=1500),
            _row("2202", opening=1000, opening_side="贷", pcr=500, ycr=500,
                 closing=1500, closing_side="贷")]
    ws = _sheet(balance_excel(_payload(rows), book_name="乙"))

    assert _total(ws, COL_OPENING) == Decimal("1000.00"), (
        "合计期初把借方与贷方余额加在一起了")
    assert _total(ws, COL_CLOSING) == Decimal("1500.00"), "合计期末同理"
    # 两侧发生额本来就是分列的，不该被这次改动带偏。
    assert _total(ws, COL_PDR) == Decimal("500.00")
    assert _total(ws, COL_PCR) == Decimal("500.00")


def test_不平衡时试算行把两边和差额都写出来():
    """合计行只放得下一边（借方合计），贷方合计必须在别处现身。

    单栏版式在合计行取了借方合计，试算成立时它等于贷方合计；真不平衡时差额
    只有写在纸上才看得见 —— 只打一个 ✗ 等于让读纸面的人自己回头加几百行。
    """
    rows = [_row("1001", opening=1000, pdr=500, ydr=500, closing=1500),
            _row("2202", opening=900, opening_side="贷", pcr=400, ycr=400,
                 closing=1400, closing_side="贷")]
    ws = _sheet(balance_excel(_payload(
        rows, period_balanced=False, closing_balanced=False,
        total_closing_debit=Decimal("1500"), total_closing_credit=Decimal("1400")),
        book_name="乙"))

    text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row)
    assert "本期发生额不平衡" in text and "期末余额不平衡" in text, text
    assert "1,400.00" in text, f"贷方合计没有出现在纸面上：{text}"
    assert "1,500.00" in text, text


# ------------------------------------------------------- ② 叶子级，不重复计数


def test_合计只算叶子不把父级算两遍():
    """「全部科目」下父子同表，逐行相加等于双倍。

    父级行本身就是下级的上卷，所以合计必须只认叶子 —— 这与
    ``balance_sheet_of_accounts`` 累计 check 的口径一致（那份实现只累计
    ``s.is_leaf``）。
    """
    leaf_dr = _row("560206", opening=1000, pdr=100, ydr=100, closing=1100, level=2)
    parent = _row("5602", opening=1000, pdr=100, ydr=100, closing=1100, leaf=False)
    leaf_cr = _row("1002", opening=1000, opening_side="贷", pcr=100, ycr=100,
                   closing=1100, closing_side="贷")
    # 屏幕上的逐行加总确实是双倍 —— 没有这一句，这条断言就没有分辨力。
    assert sum(int(r["period_debit"]) + int(r["period_credit"])
               for r in (leaf_dr, parent, leaf_cr)) == 300

    ws = _sheet(balance_excel(_payload(
        [leaf_dr, parent, leaf_cr], total_debit=Decimal("100"),
        total_credit=Decimal("100"), total_closing_debit=Decimal("1100"),
        total_closing_credit=Decimal("1100")), book_name="乙"))

    assert _total(ws, COL_PDR) == Decimal("100.00"), "合计被父级行重复计了一次"
    assert _total(ws, COL_OPENING) == Decimal("1000.00"), (
        "合计期初被父级行重复计了一次")


def test_合计与check里的试算数一致():
    """合计就是 check 的那四个数，不是「纸上那几行的和」。

    check 是这套系统里唯一一份叶子级试算口径的实现（``queries``），合计与它
    同源，两个通道才可能对得上；口径一旦分成两份，迟早分叉。
    """
    rows = [_row("560206", opening=1000, pdr=100, ydr=100, closing=1100, level=2),
            _row("5602", opening=1000, pdr=100, ydr=100, closing=1100, leaf=False),
            _row("1002", opening=1000, opening_side="贷", pcr=100, ycr=100,
                 closing=1100, closing_side="贷")]
    check = {"total_debit": Decimal("100"), "total_credit": Decimal("100"),
             "total_closing_debit": Decimal("1100"),
             "total_closing_credit": Decimal("1100")}
    ws = _sheet(balance_excel(_payload(rows, **check), book_name="乙"))

    assert _total(ws, COL_PDR) == check["total_debit"]
    assert _total(ws, COL_PCR) == check["total_credit"]
    assert _total(ws, COL_CLOSING) == check["total_closing_debit"]


def test_没有check时按同样的算法自己算():
    """老调用方 / 直接构 payload 时 check 可能只有布尔位。

    那时退回到「同样的算法」（叶子级 + 按方向归边），而不是退回「逐行相加」：
    兜底路径也必须是试算口径，否则缺陷在兜底上原样复现。
    """
    rows = [_row("1001", opening=1000, pdr=500, ydr=500, closing=1500),
            _row("2202", opening=1000, opening_side="贷", pcr=500, ycr=500,
                 closing=1500, closing_side="贷")]
    payload = {"period": "2026-01",
               "check": {"period_balanced": True, "closing_balanced": True},
               "rows": rows}
    ws = _sheet(balance_excel(payload, book_name="乙"))

    assert _total(ws, COL_OPENING) == Decimal("1000.00")
    assert _total(ws, COL_CLOSING) == Decimal("1500.00")


# ------------------------------------------------------- ③ 合计落在对的列


def test_期末合计写在期末列而不是方向列():
    """方向列在合计行没有方向可言。以前期末合计写进了第 9 列，
    而真正的第 10 列（期末）空着 —— 一格「方向」里躺着一个金额。"""
    rows = [_row("1001", opening=1000, pdr=500, ydr=500, closing=1500)]
    ws = _sheet(balance_excel(_payload(rows), book_name="乙"))
    total = _total_row(ws)

    assert Decimal(str(ws.cell(row=total, column=COL_CLOSING).value)) == Decimal("1500.00")
    assert ws.cell(row=total, column=COL_DIRECTION).value in (None, ""), (
        f"方向列里写着 {ws.cell(row=total, column=COL_DIRECTION).value!r}")


def test_合计行的表头与列宽没被动过():
    """改的是「哪一格写哪个数」，不是版式 —— 版式另有 6 个导出共用同一套。"""
    ws = _sheet(balance_excel(_payload([_row("1001", opening=100, closing=100)]),
                               book_name="乙"))
    assert [ws.cell(row=4, column=c).value for c in range(1, 11)] == [
        "科目编码", "科目名称", "方向", "期初", "本期借方", "本期贷方",
        "本年借方", "本年贷方", "方向", "期末"]
    assert ws.cell(row=1, column=1).value == "科目余额表"


# ------------------------------------------------------- ④ 真账走一遍接口


def test_导出接口的合计与试算一致且与档位无关(client, auth, db, book, periods, subjects,
                                        make_voucher):
    """真凭证走一遍 ``balance.xlsx``：合计 = check，且三档档位打出来一样。

    档位无关是这次改动的核心承诺：屏幕上「仅一级科目」与默认的「全部科目」
    行数差几十倍，纸上那一份合计不该跟着变（``filter_balance_rows`` 的
    docstring 讲的就是这件事 —— 屏幕一份、纸上一份，用户只会把纸上的当成对的）。

    **断言元组必须把合计行的六列全带上**（期初 / 本期借 / 本期贷 /
    本年借 / 本年贷 / 期末）。以前这条断言只比了四列，本年那两列根本没进
    元组 —— 而它们恰恰是实际会变的那两列（``check`` 当时不覆盖本年累计，
    合计只能回退去表体里自己加，于是按档位变）。

    **账套必须有期初余额**：没有期初的话期初那一列三档都是 0，「相等」是
    0 == 0 的恒真，证明不了任何事。
    """
    from datetime import date

    # 期初：应收票据 500（借）/ 主营业务收入 500（贷）。两边都有，合计行的
    # 归边才真的在做功 —— 只有借方余额的话，「期初合计」取哪一边都看不出来。
    r = client.post(f"/api/books/{book.id}/periods", headers=auth, json=[
        {"subject_id": subjects["1121"].id, "amount": "500"},
        {"subject_id": subjects["5001"].id, "amount": "-500"},
    ])
    assert r.status_code == 200, r.text

    # 5602 管理费用是带下级的一级科目，560206 水电费是它的明细 ——
    # 父子同时出现在「全部」那一档里，重复计数才显形；而「仅一级」那一档
    # 只有 5602（``is_leaf=False``），它以前被整行丢掉。
    make_voucher(date(2026, 1, 5), dr=[("560206", 100, "水电费")],
                 cr=[("1002", 100, "水电费")], posted=True)

    period = periods[1]
    subjects = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(db, book.id, subjects, period, False)
    assert any(r["code"] == "5602" and not r["is_leaf"] for r in rows), "账套里没有父子结构"

    totals = {}
    for scope in ("all", "leaf", "top"):
        r = client.get(f"/api/books/{book.id}/print/balance.xlsx",
                       params={"period_id": period.id, "subject_scope": scope},
                       headers=auth)
        assert r.status_code == 200, r.text
        ws = _sheet(r.content)
        totals[scope] = (_total(ws, COL_OPENING), _total(ws, COL_PDR), _total(ws, COL_PCR),
                         _total(ws, COL_YDR), _total(ws, COL_YCR), _total(ws, COL_CLOSING))

    assert totals["all"] == totals["leaf"] == totals["top"], (
        f"合计随科目档位变了：{totals}")
    # 合计 = 试算 check 的那八个数（期初/本期/本年/期末，各分借贷两边）。
    # 合计行放不下两边，期初与期末取**借方合计**（试算平衡时两边同值）。
    assert totals["all"] == (
        check["total_opening_debit"].quantize(Decimal("0.01")),
        check["total_debit"].quantize(Decimal("0.01")),
        check["total_credit"].quantize(Decimal("0.01")),
        check["total_ytd_debit"].quantize(Decimal("0.01")),
        check["total_ytd_credit"].quantize(Decimal("0.01")),
        check["total_closing_debit"].quantize(Decimal("0.01")),
    ), f"合计与试算 check 不一致：{totals} / {check}"
    # 绝对数字：期初这一维必须**非零**，否则上面那条「三档相等」是 0 == 0 的恒真
    assert totals["all"][0] == Decimal("500.00"), totals
    assert totals["all"][3] == Decimal("100.00"), totals
    assert totals["all"][5] == Decimal("600.00"), totals
