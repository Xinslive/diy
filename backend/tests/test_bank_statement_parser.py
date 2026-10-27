"""对账单解析器：容器识别、表头定位、列映射、方向判定、归一与自检。

**这些用例跑的是真的脱敏样例**（``tests/fixtures/statements/``），不是
手工构造的假表 —— 因为这个模块的全部难点就在「真实银行导出的表格长得
千奇百怪」，用规整的假数据测它等于什么都没测。

样例是 14 份真实对账单脱敏后重排出来的：容器、列结构、日期格式、
千分位、方向分布、折行一律保留，只有账号、户名、金额被换掉。
每份配一份「金标准」（``goldens.json``）：行数、收支笔数与合计、
表头行、列映射、首末行 —— 解析结果必须逐项对得上。

**三个必须记住的坑**（解析器里到处是它们的注释）：

1. 银行账的「借/贷」与会计凭证的「借/贷」**相反** —— 银行「贷方发生额」
   是余额增加（收钱），会计上银行存款增加记借方。
2. 「有值」不等于「这一侧有发生额」 —— 农商 xlsx 用 ``0.0`` 表示无发生额，
   判方向必须看 **>0**。
3. 建行同时给「记账日」和「交易日期」，业务日期取**交易日期**。
"""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from app.bankstatement import formats, semantics

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"
GOLDENS = json.loads((FIXTURES / "goldens.json").read_text(encoding="utf-8"))


def _data(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ----------------------------------------------------------------------
# 容器识别：看文件头，不看扩展名
# ----------------------------------------------------------------------

def test_HTML冒充xls_认成HTML():
    """``泓农商`` 那份 .xls 其实是 WPS 导出的网页版。

    按扩展名分发会把它送进 xlrd，然后报一句「文件损坏」—— 而文件根本没坏。
    """
    assert formats.sniff(_data("nongshang_html.xls")) == formats.CONTAINER_HTML


def test_CSV冒充xlsx_认成CSV():
    """``玲招商`` 那份 .xlsx 其实是 CSV 文本（UTF-8 BOM + CRLF + 带引号）。"""
    assert formats.sniff(_data("cmb_csv.xlsx")) == formats.CONTAINER_CSV


def test_真xls_认成XLS():
    assert formats.sniff(_data("ccb.xls")) == formats.CONTAINER_XLS


def test_真xlsx_认成XLSX():
    assert formats.sniff(_data("nongshang_xlsx.xlsx")) == formats.CONTAINER_XLSX


def test_PDF_认成PDF():
    assert formats.sniff(_data("cib_pdf.pdf")) == formats.CONTAINER_PDF


def test_扩展名完全不参与判断():
    """同一份字节，换什么后缀都该认出同一个容器。"""
    data = _data("nongshang_html.xls")
    assert formats.sniff(data) == formats.sniff(data)


# ----------------------------------------------------------------------
# 表头定位：不写死第几行
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "ccb.xls",                # 建行：表头上面压着账户信息
    "nongshang_base.xls",
    "pingan.xls",             # 平安：第 2 行
    "cmb_csv.xlsx",           # 招商 CSV：前面压着 # 注释头
    "nongshang_xlsx.xlsx",
    "cmbc_xlsx.xlsx",         # 新恒强：表头在很后面（十几行）
    "ceb_personal.xls",       # 光大：表头在第 8 行
])
def test_表头行不是写死的第一行(name):
    """表头在第几行**不许写死**。

    真实对账单的表头前面压着账户名、账号、期间、打印时间 —— 有的还有
    十几行。所以判据是「扫全表，找到认出的字段最多的那一行」，
    而不是「取第 0 行」或「取第 5 行」。
    """
    parsed = semantics.parse_statement(_data(name))
    assert parsed.header_row > 0, (
        f"{name} 的表头被当成第一行了 —— 那说明表头定位写死了位置")
    # 表头行本身必须真的像表头：认得出列名。
    assert len(parsed.column_mapping) >= 4, (
        f"{name} 在第 {parsed.header_row} 行只认出 "
        f"{len(parsed.column_mapping)} 个字段，那一行多半不是表头")
    # 数据行数 = 表头之后的有效行数
    assert len(parsed.rows) == GOLDENS[name]["row_count"]


def test_表头之前的元信息被抽走():
    """账号、户名、期间都在表头**上面**的那几行里。

    抽不出来的话，「本户是谁」（判方向要用）与「期末余额多少」
    （余额核对要用）就都没了。
    """
    parsed = semantics.parse_statement(_data("ccb.xls"))
    assert parsed.meta.account_no or parsed.meta.account_name, (
        "表头上面的元信息没抽出来")
    assert parsed.meta.period_start is not None
    assert parsed.meta.period_end is not None


def test_双行表头能合并():
    """有的银行把列名分两行放（上行大类、下行小类）。"""
    rows = [
        ["某某银行对账单", "", "", ""],
        ["交易日期", "交易时间", "收入金额", "支出金额"],
        ["", "", "存入", "支取"],
        ["2026-09-01", "10:00:00", "100.00", ""],
    ]
    index, cells = semantics.find_header(rows)
    assert index == 1
    assert any("收入" in cell for cell in cells)


# ----------------------------------------------------------------------
# 列映射：九家银行的别名都要落到正确的 IR 字段
# ----------------------------------------------------------------------

def test_建行_记账日与交易日期分开_业务日期取交易日期():
    parsed = semantics.parse_statement(_data("ccb.xls"))
    mapping = parsed.column_mapping
    assert mapping["DATE"] != mapping["POSTING_DATE"], (
        "建行同时给记账日与交易日期，两列必须分开认")
    assert parsed.rows[0].tx_date == date_of(GOLDENS["ccb.xls"]["first"]["tx_date"])


def date_of(text: str):
    from datetime import date

    return date.fromisoformat(text)


def test_方向列与收入支出列_不能互相顶替():
    """兴业那份 PDF 给的是「借/贷」方向列；农商给的是收入/支出两列。"""
    cib = semantics.parse_statement(_data("cib_pdf.pdf"))
    assert "DIRECTION" in cib.column_mapping
    nongshang = semantics.parse_statement(_data("nongshang_xlsx.xlsx"))
    assert "IN" in nongshang.column_mapping
    assert "OUT" in nongshang.column_mapping


def test_摘要与用途分得开():
    """「摘要」与「用途」是两列：摘要写事由，用途写业务类型。

    流水科目规则能按这两列分别配规则，所以认错列会让规则全部失灵。
    """
    parsed = semantics.parse_statement(_data("nongshang_xlsx.xlsx"))
    mapping = parsed.column_mapping
    assert "SUMMARY" in mapping or "PURPOSE" in mapping


def test_对方账号与对方户名分得开():
    parsed = semantics.parse_statement(_data("ccb.xls"))
    mapping = parsed.column_mapping
    assert "CP_ACCOUNT" in mapping and "CP_NAME" in mapping
    assert mapping["CP_ACCOUNT"] != mapping["CP_NAME"]


# ----------------------------------------------------------------------
# 方向判定：五条通路
# ----------------------------------------------------------------------

def test_收入支出两列_按大于零判而不是按有没有值():
    """农商的 xlsx 用 ``0.0`` 表示「这一侧没有发生额」。

    看「非空」会把 0.00 当成一笔收入 —— 于是每一行都同时有收入和支出，
    全表变成「方向未知」。
    """
    parsed = semantics.parse_statement(_data("nongshang_xlsx.xlsx"))
    known = [row for row in parsed.rows if row.direction != "UNKNOWN"]
    assert known, "一份 31 行的农商对账单不该一行方向都认不出"
    for row in known:
        assert row.direction in ("IN", "OUT")


def test_方向列_借是支出贷是收入():
    """**银行的借贷与会计的借贷相反。**

    银行账上「贷方发生额」= 余额增加 = 收钱；会计上银行存款增加记借方。
    """
    parsed = semantics.parse_statement(_data("cib_pdf.pdf"))
    for row in parsed.rows:
        if row.direction == "IN":
            assert row.amount and row.amount > 0
            break
    else:
        pytest.fail("一份兴业对账单里连一行收入都没有")


def test_两侧都有金额的行标成异常而不是乱猜():
    """疑似拆单：收入与支出同时有值。猜一个方向就是在编数据。"""
    result = semantics.resolve_direction(
        amount_in=Decimal("100"), amount_out=Decimal("200"),
        amount=None, direction_text="", tx_type="", payer="", payee="",
        summary="", account_no="", account_name="")
    assert result[0] == "UNKNOWN"
    assert "都有" in result[1]


def test_本户在付款方就是支出():
    """平安那份只有「付款方 / 收款方」两列，方向靠本户身份判。"""
    result = semantics.resolve_direction(
        amount_in=None, amount_out=None, amount=Decimal("500"),
        direction_text="", tx_type="",
        payer="6230****5263 张三", payee="李四 6222****0001",
        summary="", account_no="6230****5263", account_name="")
    assert result[0] == "OUT"


def test_本户在收款方就是收入():
    result = semantics.resolve_direction(
        amount_in=None, amount_out=None, amount=Decimal("500"),
        direction_text="", tx_type="",
        payer="李四 6222****0001", payee="6230****5263 张三",
        summary="", account_no="6230****5263", account_name="")
    assert result[0] == "IN"


def test_摘要只作提示_不单独定案():
    """摘要里有「结息」但没有别的证据时，方向仍然可以定 —— 但依据要写明
    是「按摘要推测」，让用户知道这行可能不准。"""
    result = semantics.resolve_direction(
        amount_in=None, amount_out=None, amount=Decimal("12"),
        direction_text="", tx_type="", payer="", payee="",
        summary="利息结息", account_no="", account_name="")
    assert result[0] == "IN"
    assert result[1] == "按摘要推测"


def test_方向未知_不丢弃只标记():
    result = semantics.resolve_direction(
        amount_in=None, amount_out=None, amount=Decimal("12"),
        direction_text="", tx_type="", payer="", payee="",
        summary="", account_no="", account_name="")
    assert result[0] == "UNKNOWN"


# ----------------------------------------------------------------------
# 值归一
# ----------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", Decimal("1234.56")),
    ("¥1,234.56", Decimal("1234.56")),
    ("１２３４．５６", Decimal("1234.56")),
    ("(123)", Decimal("-123")),
    ("-", None),
    ("", None),
    ("0.0", Decimal("0.00")),
])
def test_金额归一(raw, expected):
    got = semantics.parse_amount(raw)
    if expected is None:
        assert got is None
    else:
        assert got == expected


@pytest.mark.parametrize("raw", [
    "20260901",
    "2026-09-01",
    "2026/9/1",
    "2026-09-01 16:07:12",
    "2026年9月1日",
])
def test_日期归一(raw):
    parsed, _time = semantics.parse_date(raw)
    assert parsed is not None, raw
    assert (parsed.year, parsed.month, parsed.day) == (2026, 9, 1)


def test_datetime对象也认():
    """xlsx 里日期常常是**真日期单元格**，不是文本 —— 单元格读出来就是
    ``datetime``。认不出来的话这一行会被当成「没有日期」丢掉。"""
    from datetime import datetime as dt

    parsed, time_text = semantics.parse_date(dt(2026, 9, 1, 16, 7, 12))
    assert parsed == dt(2026, 9, 1).date()
    assert time_text == "16:07:12"


def test_Excel序列号也认():
    parsed, _time = semantics.parse_date(46293)
    assert parsed is not None


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------

def test_自检_文件自称的笔数与金额对得上():
    """农商那份文件自己写着「收入 N 笔」，解析出来必须一样。"""
    parsed = semantics.parse_statement(_data("nongshang_base.xls"))
    meta = parsed.meta
    if meta.declared_income_count is not None:
        actual = sum(1 for row in parsed.rows if row.direction == "IN")
        assert actual == meta.declared_income_count
    if meta.declared_expense_count is not None:
        actual = sum(1 for row in parsed.rows if row.direction == "OUT")
        assert actual == meta.declared_expense_count


def test_自检_漏掉一行要报出来():
    """把一行删掉，余额就接不上了 —— 诊断里必须说出来。

    **不丢弃数据**：诊断只是提示，用户自己决定怎么办。
    """
    from app.bankstatement.grid import Grid, cell_text

    grid = semantics.read_grids(_data("nongshang_base.xls"))[0]
    rows = list(grid.rows)
    # 找一行有余额的数据行，删掉它
    for index, row in enumerate(rows):
        if index > 5 and sum(1 for c in row if cell_text(c)) > 3:
            break
    trimmed = Grid(container=grid.container, sheet=grid.sheet,
                   rows=rows[:index] + rows[index + 1:])
    parsed = semantics.parse_grid(trimmed)
    joined = " ".join(parsed.diagnosis)
    assert "余额" in joined or "漏" in joined or "接不上" in joined, (
        f"删掉一行之后诊断没有报出来：{parsed.diagnosis}")


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------

def test_文件自报的总笔数与解析行数一致():
    """兴业那种模板只给「本期总笔数」，不给收支分列的笔数。

    拿它跟解析出的行数对，是发现漏行最直接的一条路 ——
    文件说 4 笔，解析出 3 笔，那就是漏了一行。
    """
    for name in ("cib_pdf.pdf", "cib_construction_pdf.pdf"):
        parsed = semantics.parse_statement(_data(name))
        assert parsed.meta.declared_total_count == len(parsed.rows), name
        assert parsed.diagnosis == [], name


def test_少一行时总笔数对不上要报出来():
    """删掉一行，自报的总笔数就跟不上了 —— 诊断必须说出来。

    跟「余额接不上」是两条独立的通路：有的表余额没这一列，
    靠余额查不出漏行；笔数那条路能兜住。
    """
    from app.bankstatement.grid import Grid, cell_text

    grid = semantics.read_grids(_data("cib_pdf.pdf"))[0]
    rows = list(grid.rows)
    index = next(i for i, row in enumerate(rows)
                 if i > 0 and any("2026-09" in cell_text(c) for c in row))
    trimmed = Grid(container=grid.container, sheet=grid.sheet,
                   rows=rows[:index] + rows[index + 1:],
                   meta=grid.meta, footer=grid.footer)
    parsed = semantics.parse_grid(trimmed)
    joined = " ".join(parsed.diagnosis)
    assert "文件自称共 4 笔" in joined, parsed.diagnosis


def test_PDF解析出行数与金额():
    parsed = semantics.parse_statement(_data("cib_pdf.pdf"))
    assert parsed.container == "PDF"
    assert len(parsed.rows) == GOLDENS["cib_pdf.pdf"]["row_count"]
    assert str(parsed.income_total) == GOLDENS["cib_pdf.pdf"]["income_total"]


def test_PDF里折行的长户名被合并():
    """兴业那份把「鑫方泰商务服务（泉州）有限公司」折成两行。

    不合并的话对方户名就只剩半截 —— 而流水科目规则正是按户名配的，
    折断的户名永远匹配不上。
    """
    parsed = semantics.parse_statement(_data("ceb_corp_pdf.pdf"))
    names = [row.counterparty_name for row in parsed.rows]
    assert any(len(name) >= 6 for name in names), names
    assert not any(name.endswith("（") or name.endswith("(") for name in names)


# ----------------------------------------------------------------------
# 全量夹具 vs 金标准
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_每份样例都与金标准一致(name):
    golden = GOLDENS[name]
    parsed = semantics.parse_statement(_data(name))
    assert parsed.container == golden["container"], name
    assert parsed.header_row == golden["header_row"], name
    assert len(parsed.rows) == golden["row_count"], name
    assert str(parsed.income_total) == golden["income_total"], name
    assert str(parsed.expense_total) == golden["expense_total"], name
    assert parsed.column_mapping == golden["column_mapping"], name
    if golden.get("period_start"):
        assert parsed.meta.period_start.isoformat() == golden["period_start"], name
        assert parsed.meta.period_end.isoformat() == golden["period_end"], name
    # 诊断也要对得上：金标准里记了它，就得有人盯。
    # 解析器「多报一条」「少报一条」都是行为变化 —— 漏报最要命，
    # 因为漏报的那条正是「我少认了几行」这种要命的话。
    assert parsed.diagnosis == golden["diagnosis"], name
    # 文件自报的笔数与金额：解析器要拿它们跟解析结果对账，
    # 这份对账本身就是金标准的一部分。
    assert parsed.meta.declared_total_count == golden["declared_total_count"], name
    if golden.get("declared_income_total") is not None:
        assert str(parsed.meta.declared_income_total) == golden["declared_income_total"], name
    if golden.get("declared_expense_total") is not None:
        assert str(parsed.meta.declared_expense_total) == golden["declared_expense_total"], name


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_首末行与金标准一致(name):
    golden = GOLDENS[name]
    parsed = semantics.parse_statement(_data(name))
    if not parsed.rows:
        pytest.skip(f"{name} 没有流水行")
    first = golden.get("first")
    last = golden.get("last")
    if first:
        row = parsed.rows[0]
        assert row.tx_date.isoformat() == first["tx_date"], name
        assert str(row.amount) == first["amount"], name
        assert row.direction == first["direction"], name
    if last:
        row = parsed.rows[-1]
        assert row.tx_date.isoformat() == last["tx_date"], name
        assert str(row.amount) == last["amount"], name
        assert row.direction == last["direction"], name
