"""期初余额表解析：用友 .xls 余额表与本系统四列表。

**为什么单独给用友那份表钉一整套用例。**

期初是整个账的起点：这里少读一行、读错一个方向，后面每个月、每张账簿、
每份报表都跟着歪，而界面上**看不出任何异常** —— 用户看到的是一句
「已读入 N 条」。所以每一条会「静默丢数」的处理都要有用例盯着：

- 借贷分列定方向（不靠正负猜）
- 期初 / 期末两套栏位取哪一套
- 「资产小计 / 合计」这几行静默剔除（判据是编码非数字，不是名字里有「合计」——
  真实科目里就有「累计折旧」）
- 全零行跳过（用友表几百行里绝大多数是 0）
- 科目名称带前导空格表示级次，匹配前要 strip

行号必须能在源文件里对上：报「第 37 行」而用户数出来是第 38 行，
等于没报错。
"""
from __future__ import annotations

import types
from decimal import Decimal

import pytest

from app.accounting.opening_importer import parse_opening_rows, parse_opening_workbook
from app.core.uploads import UploadTooLarge


def _subject(sid: int, code: str, name: str, direction: str = "DEBIT",
             is_leaf: bool = True) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=sid, code=code, name=name,
                                  direction=direction, is_leaf=is_leaf)


def _index(*subjects):
    return ({s.code: s for s in subjects}, {s.name: s for s in subjects})


YONYOU_HEADER = ["会计年度", "会计期间", "科目编码", "科目名称", "外币名称",
                 "期初借方", "期初贷方", "本期发生借方", "本期发生贷方",
                 "期末借方", "期末贷方"]


def _yonyou(rows, header=YONYOU_HEADER):
    return [list(header)] + [list(r) for r in rows]


def _yonyou_row(code, name, od=0.0, oc=0.0, ed=0.0, ec=0.0, year="2026",
                period="月份：2026.10-2026.10"):
    return [year, period, code, name, "", od, oc, 0.0, 0.0, ed, ec]


# ------------------------------------------------------------------ 用友：方向


def test_用友表方向由借贷分列决定():
    """借方列有值是借方、贷方列有值是贷方 —— 不靠正负猜，也不按科目属性强推。

    1122 应收账款是借方属性，这里挂着贷方余额（客户预付），强推属性就洗掉了。
    """
    by_code, by_name = _index(
        _subject(1, "100201", "银行存款-工商"),
        _subject(2, "112201", "应收账款-甲", "DEBIT"),
    )
    out = parse_opening_rows(_yonyou([
        _yonyou_row("100201", "  银行存款-工商", od=3056.43),
        _yonyou_row("112201", "  应收账款-甲", oc=4148.43),
    ]), subjects_by_code=by_code, subjects_by_name=by_name)

    rows = {r["code"]: r for r in out["rows"]}
    assert rows["100201"]["signed"] == "3056.43"
    assert rows["100201"]["display_side"] == "借"
    assert rows["112201"]["signed"] == "-4148.43"
    assert rows["112201"]["display_side"] == "贷"
    assert out["source"] == "用友余额表"
    # 3056.43 与 4148.43 本来就不相等 —— 合计是用来暴露差额的，不是用来「通过」的
    assert out["balanced"] is False
    assert Decimal(out["total_debit"]) == Decimal("3056.43")
    assert Decimal(out["total_credit"]) == Decimal("4148.43")


def test_用友表期末与期初是两套数():
    """同一张表里期初与期末分列。取错了不会报错，只会让用户对着一堆差额发呆。"""
    by_code, by_name = _index(
        _subject(1, "100201", "银行存款-工商"),
        _subject(2, "4001", "实收资本", "CREDIT"),
    )
    rows = [
        _yonyou_row("100201", "银行存款-工商", od=1000, ed=1500),
        _yonyou_row("4001", "实收资本", oc=1000, ec=1500),
    ]
    opening = parse_opening_rows(_yonyou(rows), subjects_by_code=by_code,
                                 subjects_by_name=by_name, basis="opening")
    closing = parse_opening_rows(_yonyou(rows), subjects_by_code=by_code,
                                 subjects_by_name=by_name, basis="closing")
    assert Decimal(opening["total_debit"]) == Decimal("1000")
    assert Decimal(opening["total_credit"]) == Decimal("1000")
    assert Decimal(closing["total_debit"]) == Decimal("1500")
    assert Decimal(closing["total_credit"]) == Decimal("1500")
    assert closing["has_closing"] is True


def test_用友表借贷同列都有值要报错():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("100201", "银行存款-工商", od=100, oc=50)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert "借方和贷方" in out["rows"][0]["error"]
    assert out["rows"][0]["subject_id"] == 1
    # 报错的行**不参与合计**：这一行方向定不下来，硬塞进合计只会让差额
    # 变成一个查不出原因的数字
    assert Decimal(out["total_debit"]) == Decimal("0")
    assert Decimal(out["total_credit"]) == Decimal("0")
    assert any("借方和贷方" in e for e in out["errors"])


# ------------------------------------------------------------------ 用友：剔掉的东西


def test_合计行静默剔除而不是报错():
    """用友表末尾有「资产小计 / 负债小计 / 权益小计 / 损益小计 / 合计」。

    当成科目会刷出一堆「科目在本账套不存在」，当成错误更糟。
    """
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(_yonyou([
        _yonyou_row("100201", "银行存款-工商", od=100),
        ["2026", "月份：2026.10-2026.10", "资产小计", "", "", 100.0, 0.0, 0.0, 0.0, 100.0, 0.0],
        ["2026", "月份：2026.10-2026.10", "损益小计", "", "", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        ["2026", "月份：2026.10-2026.10", "合计", "", "", 100.0, 0.0, 0.0, 0.0, 100.0, 0.0],
    ]), subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["errors"] == [], out["errors"]
    assert len(out["rows"]) == 1
    assert out["skipped"] == 3
    # 关键：合计行**没有**被算进合计里，否则同一笔钱会被数两遍
    assert Decimal(out["total_debit"]) == Decimal("100")


def test_名字里带合计的正经科目不能被剔掉():
    """用友表里真有一个科目叫「累计折旧」（编码 1602）。

    判据必须是「编码非数字」，不是「名字里有合计」—— 否则这笔余额被静默丢掉。
    """
    by_code, by_name = _index(_subject(1, "1602", "累计折旧"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("1602", "累计折旧", oc=449446.07)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert len(out["rows"]) == 1
    assert out["rows"][0]["code"] == "1602"
    assert Decimal(out["total_credit"]) == Decimal("449446.07")


def test_全零行跳过且计入skipped():
    """用友余额表几百行里绝大多数是 0.0，全塞进预览框没法看。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(_yonyou([
        _yonyou_row("100201", "银行存款-工商"),
        _yonyou_row("100202", "银行存款-建行"),
        _yonyou_row("100203", "银行存款-农商"),
    ]), subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["rows"] == []
    assert out["skipped"] == 3
    # 行级入口交出空预览（前端自己显示「读入 0 条」），字节流入口才报错 ——
    # 「这份文件里一行余额都没有」是用户必须知道的，不能只给一个空列表
    with pytest.raises(ValueError, match="没有解析出任何数据行"):
        parse_opening_workbook(
            _xlsx_with(YONYOU_HEADER, [_yonyou_row("100201", "银行存款-工商")]),
            "全零.xlsx", subjects_by_code=by_code, subjects_by_name=by_name)


def test_表头前有大标题行仍能认出表头():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        [["科目余额表"], ["单位：某某公司    2026年10月"]] + _yonyou([
            _yonyou_row("100201", "银行存款-工商", od=100)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert len(out["rows"]) == 1
    assert out["header_row"] == 3


def test_两行表头也能认出来():
    """用友某些版本第一行只写「期初 / 期末」，第二行才写「借方 / 贷方」。

    拼起来是「期初借方」「贷方」「期末借方」「贷方」这种半空的串 ——
    认表头的代码必须按**位置**认，不能因为某一格缺字就整体认不出。
    """
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    rows = [
        ["会计年度", "会计期间", "科目编码", "科目名称", "期初", "", "期末", ""],
        ["", "", "", "", "借方", "贷方", "借方", "贷方"],
        ["2026", "月份：2026.10-2026.10", "100201", "银行存款-工商", 100, 0, 0, 0],
    ]
    out = parse_opening_rows(rows, subjects_by_code=by_code, subjects_by_name=by_name)
    assert len(out["rows"]) == 1, out
    assert Decimal(out["rows"][0]["signed"]) == Decimal("100")
    assert out["source"] == "用友余额表"


# ------------------------------------------------------------------ 用友：匹配


def test_科目名称前导空格要strip后才匹配得上():
    by_code, by_name = _index(_subject(1, "112204020803", "256、260、261"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("112204020803", "        256、260、261", od=6000)]),
        subjects_by_code={}, subjects_by_name=by_name)
    assert out["rows"][0]["subject_id"] == 1
    assert out["rows"][0]["error"] == ""


def test_编码匹配不到时按名称兜住():
    """很多人拿到的表只有名称对得上（编码体系不一样）。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("9-9-9", "银行存款-工商", od=888)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["rows"][0]["subject_id"] == 1
    assert out["errors"] == []


def test_本账套没有的科目要报出来并计入unmatched():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(_yonyou([
        _yonyou_row("100201", "银行存款-工商", od=100),
        _yonyou_row("999999", "不存在", od=50),
    ]), subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["unmatched"] == 1
    assert any("科目在本账套不存在" in e for e in out["errors"])
    # 不参与合计 —— 否则差额会被一个挂不上的科目掩盖掉
    assert Decimal(out["total_debit"]) == Decimal("100")


def test_非明细科目要挡住():
    by_code, by_name = _index(_subject(1, "1002", "银行存款", "DEBIT", is_leaf=False))
    out = parse_opening_rows(_yonyou([_yonyou_row("1002", "银行存款", od=100)]),
                             subjects_by_code=by_code, subjects_by_name=by_name)
    assert "不是明细科目" in out["rows"][0]["error"]
    assert Decimal(out["total_debit"]) == Decimal("0")


def test_期间标签被带回来():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(_yonyou([
        _yonyou_row("100201", "全零行"),
        _yonyou_row("100201", "银行存款-工商", od=100, period="月份：2026.10-2026.10"),
    ]), subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["period_label"] == "月份：2026.10-2026.10"


def test_行号是Excel真实行号():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(_yonyou([
        _yonyou_row("100201", "A", od=1),
        _yonyou_row("100201", "B", od=2),
    ]), subjects_by_code=by_code, subjects_by_name=by_name)
    # 表头是第 1 行，第一条数据是第 2 行 —— 报错行号要能对上源文件
    assert [r["line"] for r in out["rows"]] == [2, 3]


# ------------------------------------------------------------------ 金额容错


@pytest.mark.parametrize("raw,expected", [
    (3056.43, "3056.43"),
    ("3,056.43", "3056.43"),
    ("￥598.5", "598.50"),
    ("  598.5  ", "598.50"),
    ("１２３４．５", "1234.50"),      # 全角数字：NFKC 折回半角再解析
])
def test_金额各种写法都吃得下(raw, expected):
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("100201", "银行存款-工商", od=raw, oc=0)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    row = out["rows"][0]
    assert Decimal(row["signed"] or "0") == Decimal(expected), row


@pytest.mark.parametrize("raw", ["", None, 0, 0.0])
def test_空值与零就是零_不算错也不进预览(raw):
    """用友表里几百行是 0.0。全零行不是错，但也不该占着预览框。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("100201", "银行存款-工商", od=raw, oc=0)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["errors"] == []
    assert out["rows"] == []
    assert out["skipped"] == 1


def test_金额解析不出来必须报错而不是当0():
    """期初差一笔的代价是之后每个月都跟着歪，而用户看到的仍是「已读入 N 条」。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        _yonyou([_yonyou_row("100201", "银行存款-工商", od="abc", oc=0)]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert "金额无法识别" in out["rows"][0]["error"]
    assert Decimal(out["total_debit"]) == Decimal("0")


# ------------------------------------------------------------------ 本系统四列表


def _xlsx_with(header, rows):
    """按给定表头 + 行造一个 .xlsx。表头是关键：用友那套表头只在 xlsx 里能造
    （环境里没有 xlwt 写不出 .xls），但行解析完全一致，够测了。"""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _legacy_xlsx(rows):
    return _xlsx_with(["科目编码", "科目名称", "方向", "期初余额"], rows)


def test_四列表行为不变_方向以文件为准():
    by_code, by_name = _index(
        _subject(1, "1122", "应收账款", "DEBIT"),
    )
    out = parse_opening_rows(
        [["科目编码", "科目名称", "方向", "期初余额"],
         ["1122", "应收账款", "", 500],
         ["1122", "应收账款", "贷", 500]],
        subjects_by_code=by_code, subjects_by_name=by_name)
    by_side = {x["natural"]: x for x in out["rows"]}
    assert Decimal(by_side[""]["signed"]) == Decimal("500")
    assert Decimal(by_side["贷"]["signed"]) == Decimal("-500")
    assert out["source"] == "Excel 期初表"


def test_四列表的零值行照旧返回():
    """与用友表相反：四列表保留 0 值行，前端与既有用例都按这个行为走。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        [["科目编码", "科目名称", "方向", "期初余额"],
         ["100201", "银行存款-工商", "借", 0]],
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert len(out["rows"]) == 1
    assert out["rows"][0]["amount"] == "0"
    assert out["rows"][0]["error"] == ""


def test_认不出表头就按位置取ABCD():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        [["100201", "银行存款-工商", "借", 100]],
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert len(out["rows"]) == 1
    assert Decimal(out["rows"][0]["signed"]) == Decimal("100")
    assert out["header_row"] == 0


def test_xlsx走openpyxl也能解析():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_workbook(
        _legacy_xlsx([("100201", "银行存款-工商", "借", 100)]),
        "期初余额.xlsx",
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["file_format"] == "xlsx"
    assert Decimal(out["rows"][0]["signed"]) == Decimal("100")


def test_xls文件走xlrd那条路():
    """``.xls`` 是 OLE 复合文档，openpyxl 只认 .xlsx。

    报错文案就能证明走了 xlrd：openpyxl 那条路说的是「无法读取 Excel 文件」。
    """
    with pytest.raises(ValueError) as exc:
        parse_opening_workbook(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1garbage",
                               "科目余额表.xls",
                               subjects_by_code={}, subjects_by_name={})
    assert ".xls" in str(exc.value), str(exc.value)


def test_空文件报错():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    with pytest.raises(ValueError, match="空文件"):
        parse_opening_rows([], subjects_by_code=by_code, subjects_by_name=by_name)


def test_取数口径只认期初和期末():
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    with pytest.raises(ValueError, match="取数口径"):
        parse_opening_rows(_yonyou([_yonyou_row("100201", "银行存款-工商", od=1)]),
                           subjects_by_code=by_code, subjects_by_name=by_name,
                           basis="average")


def test_行数超限要挡住():
    """容量闸的第三道：前两道都能被伪造，行数不会。"""
    from app.core.config import settings

    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    head = [["科目编码", "科目名称", "方向", "期初余额"]]
    rows = head + [["100201", "银行存款-工商", "借", i + 1] for i in range(10)]
    original = settings.import_max_rows
    try:
        object.__setattr__(settings, "import_max_rows", 5)
        with pytest.raises(UploadTooLarge):
            parse_opening_workbook(_legacy_xlsx(rows[1:]), "x.xlsx",
                                   subjects_by_code=by_code,
                                   subjects_by_name=by_name)
    finally:
        object.__setattr__(settings, "import_max_rows", original)


# ------------------------------------------------------- 汇总行的完整清单
#
# 只钉「资产小计 / 合计」两种是不够的：用友各版本写的小计标签并不统一，
# 少认一种就等于把一行**已经加过总**的数当成科目读进来 —— 借贷合计当场翻倍，
# 而界面只显示「已读入 N 条」。


@pytest.mark.parametrize("label", [
    "资产小计", "负债小计", "权益小计", "损益小计",
    "合计", "总计", "本页合计", "累计", "本年累计",
])
def test_每一种汇总标签都被剔掉(label):
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    summary = ["2026", "月份：2026.10-2026.10", label, "", "", 100.0, 0.0, 0.0, 0.0, 100.0, 0.0]
    out = parse_opening_rows(_yonyou([_yonyou_row("100201", "银行存款-工商", od=100), summary]),
                             subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["errors"] == [], out["errors"]
    assert [r["code"] for r in out["rows"]] == ["100201"]
    assert out["skipped"] == 1
    # 剔掉了才有意义：汇总行那个 100 若进来，这笔银行存款就被数了两遍
    assert Decimal(out["total_debit"]) == Decimal("100")


@pytest.mark.parametrize("label", ["资产小计", "合计", "本年累计"])
def test_汇总标签写在名称列里也一样剔掉(label):
    """有的版本把标签写进「科目名称」列而不是编码列。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    out = parse_opening_rows(
        _yonyou([["2026", "月份：2026.10-2026.10", "", label, "", 100.0, 0.0, 0.0, 0.0, 100.0, 0.0]]),
        subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["rows"] == []
    assert out["skipped"] == 1
    assert out["errors"] == []


def test_贷方属性科目挂借方余额时signed为正():
    """反方向余额的镜像：4001 实收资本是贷方属性，这里借方有 300。

    方向由**值所在的列**决定，与科目属性无关 —— 按属性强推就把这笔预收洗成了
    实收资本的正余额。
    """
    by_code, by_name = _index(_subject(1, "4001", "实收资本", "CREDIT"))
    out = parse_opening_rows(_yonyou([_yonyou_row("4001", "实收资本", od=300)]),
                             subjects_by_code=by_code, subjects_by_name=by_name)
    row = out["rows"][0]
    assert Decimal(row["signed"]) == Decimal("300")
    assert row["display_side"] == "借"
    assert Decimal(out["total_debit"]) == Decimal("300")


# ------------------------------------------------------------------ 读取器分流
#
# 环境里没有 xlwt，写不出真 .xls（xlwt 只能写、xlrd 只能读），所以 .xls 那条路
# 只能靠 monkeypatch 断言「谁被调用了」。断言的是**分流决策**，
# 不是解析结果 —— 解析结果那边由 xlsx 的同一套行数据覆盖。


_OLE_HEAD = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_HEAD = b"PK\x03\x04"


def test_OLE魔数会分流到xlrd读取器(monkeypatch):
    from app.accounting import subject_importer

    calls = []

    def fake_iter_xls(data):
        calls.append(data)
        return "Sheet1", iter([["x"]]), "xls"

    monkeypatch.setattr(subject_importer, "_iter_xls", fake_iter_xls)

    _, _, fmt = subject_importer.iter_workbook_rows(_OLE_HEAD + b"payload", "科目余额表.xls")
    assert calls == [_OLE_HEAD + b"payload"]
    assert fmt == "xls"

    # .xlsx 可能被起成 .xls 的名字（有些系统导出时按内容命名）：
    # 文件名只是兼容补充，zip 魔字节优先 —— 否则 .xlsx 会被喂给 xlrd 直接炸。
    # 两个读取器都替换掉：这里断言的是**分流决策**，不是真去解析假字节。
    calls.clear()
    monkeypatch.setattr(subject_importer, "_iter_xlsx", lambda data: ("Sheet2", iter([["y"]]), "xlsx"))
    _, _, fmt = subject_importer.iter_workbook_rows(_ZIP_HEAD + b"payload", "其实是xlsx.xls")
    assert calls == []
    assert fmt == "xlsx"


def test_期初解析走的是同一份公开读取器(monkeypatch):
    """.xls 的分流逻辑只能有一份：这里与科目表共用 iter_workbook_rows。"""
    import app.accounting.opening_importer as oi

    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    rows = _yonyou([_yonyou_row("100201", "银行存款-工商", od=100)])
    seen = {}

    def fake_rows(data, filename):
        seen["data"] = data
        return "UFPrn2026", iter(rows), "xls"

    monkeypatch.setattr(oi, "iter_workbook_rows", fake_rows)
    out = parse_opening_workbook(_OLE_HEAD + b"payload", "科目余额表.xls",
                                 subjects_by_code=by_code, subjects_by_name=by_name)
    assert seen["data"] == _OLE_HEAD + b"payload"
    assert out["file_format"] == "xls"          # 以字节流判定为准，不看文件后缀
    assert out["source"] == "用友余额表"
    assert Decimal(out["rows"][0]["signed"]) == Decimal("100")


def test_科目表读取器的旧名别名还在():
    """``_workbook_rows`` 被改成公开名时必须留别名。

    既有测试按旧名调用；不留别名的话那些用例会「什么都没测」地通过。
    """
    from app.accounting import subject_importer

    assert subject_importer._workbook_rows is subject_importer.iter_workbook_rows
    assert "iter_workbook_rows" in subject_importer.__all__


# ------------------------------------------------------------------ 契约护栏


def test_两个入口返回同一个结构():
    """路由与前端只认一套 payload：多一个键就等着有人按不存在的字段写代码。"""
    by_code, by_name = _index(_subject(1, "100201", "银行存款-工商"))
    row_level = parse_opening_rows(_yonyou([_yonyou_row("100201", "银行存款-工商", od=100)]),
                                   subjects_by_code=by_code, subjects_by_name=by_name)
    byte_level = parse_opening_workbook(
        _xlsx_with(YONYOU_HEADER, [_yonyou_row("100201", "银行存款-工商", od=100)]),
        "科目余额表.xlsx", subjects_by_code=by_code, subjects_by_name=by_name)
    assert set(row_level) == set(byte_level), (
        set(row_level) ^ set(byte_level))
    assert set(row_level["rows"][0]) == set(byte_level["rows"][0])


def test_解析模块里不能出现HTTPException():
    """这个模块没有 HTTP 语义：解析失败抛 ValueError，状态码由路由决定。

    混进 HTTPException 会让服务层调用在非 HTTP 上下文里直接炸 500。
    """
    from pathlib import Path

    import app.accounting.opening_importer as oi

    source = Path(oi.__file__).read_text(encoding="utf-8")
    assert "raise HTTPException" not in source
    assert "import HTTPException" not in source
    assert "from fastapi" not in source
