"""导入路径的三处护栏（审查修复）。

* **「类型」列与编码首位对不上就拒**（``subject_importer``）
  —— 科目表导入按外部表的「类型」列自动置 ``carry_forward``，那张表的
  「类型」写错一个词，资产科目就被标成结转科目，随后任何一次月末结转
  都会把它的余额整体搬进本年利润 —— 钱凭空消失，而**借贷仍然平衡，
  系统自检抓不到**。
  读侧按 ``category`` 过滤**不是独立防线**（category 本身就是从同一列
  推导出来的），所以导入路径上要补一道**独立于「类型」列**的证据：
  **科目编码的首位（大类码）**。

* **科目树深度上限**（``subject_importer``）
  —— 账簿引擎 ``_roll_up_openings`` 有个 16 层保险丝，超过就静默截断
  子树合计。导入是科目树唯一的批量入口，超限时要在入口就报错。

* **凭证导入的表头扫描**（``importer``）
  —— 凭证导入以前写死「第 1 行就是表头」，而外部系统导出常在表头上方
  带标题、制表日期 —— 整份表读不进来。期初余额导入早就有同款扫描。
"""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.accounting.importer import parse_workbook
from app.accounting.subject_importer import parse_subject_workbook


def _xlsx(rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _chart(rows: list[list]) -> bytes:
    """一张外部科目表：类型 / 级次 / 科目编码 / 科目名称 / 余额方向。"""
    return _xlsx([
        ["类型", "级次", "科目编码", "科目名称", "余额方向"],
        *rows,
    ])


# --------------------------------------------------------------------- 1. 类型 vs 编码首位


def test_类型与编码首位不一致被拒():
    """把 1002 标成「损益」——编码首位 1 在本账套是**资产类**，对不上就拒。

    后果不是显示难看：损益类会被自动置上 ``carry_forward``，月末结转把
    它的余额整体搬进本年利润 —— 钱凭空消失，而借贷仍然平衡。
    """
    data = _chart([
        ["资产", 1, "1001", "库存现金", "借"],
        ["损益", 1, "1002", "银行存款", "借"],      # ← 写错了
        ["负债", 1, "2202", "应付账款", "贷"],
        ["损益", 1, "5001", "主营业务收入", "贷"],
    ])
    result = parse_subject_workbook(data, "会计科目.xlsx")

    assert result["ok"] is False, "编码首位与「类型」不一致的科目表被照单全收了"
    messages = [e["message"] for e in result["errors"]]
    hit = [m for m in messages if "1002" in m]
    assert hit, f"报错要点名是哪个科目：{messages}"
    assert any("首位" in m for m in hit), hit


def test_类型与编码首位一致时照常导入():
    """闸门不能顺手把正常表也拦了。"""
    data = _chart([
        ["资产", 1, "1001", "库存现金", "借"],
        ["资产", 1, "1002", "银行存款", "借"],
        ["负债", 1, "2202", "应付账款", "贷"],
        ["损益", 1, "5001", "主营业务收入", "贷"],
    ])
    result = parse_subject_workbook(data, "会计科目.xlsx")

    assert result["ok"] is True, result["errors"]
    assert result["total"] == 4


def test_外部表用另一套编码体系时不误伤():
    """外部系统的编码规则可能与本账套不同 —— 拿不准时**宁可不做校验**。

    存量账里还有上一版《企业会计准则》编号：4001 实收资本是所有者权益类、
    6001 主营业务收入是损益类。本账套用友 4-2-2 里 4 是成本、压根没有 6 ——
    拿本账套的规则去判它就是误伤，那张表会**根本导不进来**。
    """
    data = _chart([
        ["资产", 1, "1001", "库存现金", "借"],
        ["负债", 1, "2202", "应付账款", "贷"],
        ["所有者权益", 1, "4001", "实收资本", "贷"],
        ["损益", 1, "6001", "主营业务收入", "贷"],
    ])
    result = parse_subject_workbook(data, "会计科目.xlsx")

    assert result["ok"] is True, result["errors"]
    assert result["total"] == 4


# --------------------------------------------------------------------- 2. 层级深度


def test_科目层级深度超限被拒():
    """17 级的科目树 → 明确报错（引擎的期初上卷只处理到 16 层）。"""
    rows = [["资产", 1, "1001", "库存现金", "借"]]
    for level in range(2, 18):
        code = "1001" + "0" * (level - 1)
        rows.append(["资产", level, code, f"第 {level} 级", "借"])
    result = parse_subject_workbook(_chart(rows), "会计科目.xlsx")

    assert result["ok"] is False
    messages = [e["message"] for e in result["errors"]]
    assert any("深度" in m and "16" in m for m in messages), messages


def test_标准三级科目表不受影响():
    """闸门不能把正常的科目表拦了（标准科目表只有 3 级）。"""
    data = _chart([
        ["资产", 1, "1001", "库存现金", "借"],
        ["资产", 2, "100101", "备用金", "借"],
        ["资产", 3, "10010101, ".replace(", ", ""), "备用金-工行", "借"],
    ])
    result = parse_subject_workbook(data, "会计科目.xlsx")

    assert result["ok"] is True, result["errors"]


# --------------------------------------------------------------------- 3. 凭证导入的表头扫描


def _voucher_xlsx(rows: list[list]) -> bytes:
    return _xlsx(rows)


def test_凭证导入认得出表头上方的标题行(db, book):
    """表头不在第 1 行时也要读得进来。

    修好之前：``header = next(rows_iter)`` 写死「第 1 行就是表头」，
    外部导出常在表头上方带标题/制表日期 —— 整份表**根本导不进来**。
    """
    data = _voucher_xlsx([
        ["序时账", "", "", "", "", ""],
        ["2026 年 10 月", "", "", "", "", ""],
        ["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"],
        ["2026-01-05", "记-1", "收租金", "1001", 1000, 0],
        [None, None, "收租金", "5001", 0, 1000],
    ])
    result = parse_workbook(data, book.id, db)

    assert len(result["vouchers"]) == 1, result["vouchers"]
    voucher = result["vouchers"][0]
    assert len(voucher["entries"]) == 2, voucher
    assert voucher["voucher_no"] == "记-1"
    assert voucher["errors"] == [], voucher["errors"]


def test_凭证导入的行号是文件里的真实行号(db, book):
    """表头不在第 1 行时，报错里的「第 N 行」必须是 Excel 里那一行。

    否则用户在 Excel 里按行号找会找错地方 —— 而「哪一行有问题」正是
    导入报错里最有用的一句话。
    """
    data = _voucher_xlsx([
        ["序时账", "", "", "", "", ""],
        ["2026 年 10 月", "", "", "", "", ""],
        ["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"],
        ["2026-01-05", "记-1", "收租金", "1001", 1000, 0],
        [None, None, "收租金", "9999", 0, 1000],     # ← Excel 的第 5 行
    ])
    result = parse_workbook(data, book.id, db)

    voucher = result["vouchers"][0]
    assert voucher["line"] == 4, f"凭证起始行应当是 4，实际 {voucher['line']}"
    assert any("第 5 行" in e for e in voucher["errors"]), voucher["errors"]


def test_凭证导入认得出双行合并表头(db, book):
    """用友某些版本把列名分两行放（上面一行标题、下面一行「借方/贷方」）。"""
    data = _voucher_xlsx([
        ["日期", "凭证号", "摘要", "科目编码", "", ""],
        ["", "", "", "", "借方", "贷方"],
        ["2026-01-05", "记-1", "收租金", "1001", 1000, 0],
        [None, None, "收租金", "5001", 0, 1000],
    ])
    result = parse_workbook(data, book.id, db)

    assert len(result["vouchers"]) == 1, result["vouchers"]
    voucher = result["vouchers"][0]
    assert len(voucher["entries"]) == 2, voucher
    assert voucher["errors"] == [], voucher["errors"]
    # 行号基准也跟着走：数据从第 3 行开始
    assert voucher["line"] == 3, f"凭证起始行应当是 3，实际 {voucher['line']}"


def test_没有表头的文件仍然报缺表头(db, book):
    """扫描不能把「根本没有表头」这件事变成别的错。"""
    data = _voucher_xlsx([
        ["2026-01-05", "记-1", "收租金", "1001", 1000, 0],
    ])
    with pytest.raises(ValueError, match="缺少必需的表头列"):
        parse_workbook(data, book.id, db)
