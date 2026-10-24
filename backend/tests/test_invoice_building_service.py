"""建筑服务发票的版式：表头没有「规格型号」那一列。

这一版式的列是「项目名称 建筑服务发生地 建筑项目名称 金额 税率/征收率 税额」。

以前解析器只认「项目名称 + 规格型号」当表头，于是这种票的明细**一行都提不
出来**：号码、日期、买卖方、价税合计全都认得出来，只有明细是空的 ——
整张票报「没有可入账的发票明细」，用户看着一头雾水（票就在眼前，全是字）。

这里盯三件事：

1. **明细提得出来**，金额与票面合计分毫不差（9206.93 / 92.07 / 9299.00）；
2. 名字区是**三个格子**：项目名称、建筑服务发生地、建筑项目名称。
   发生地是地址（对本账没用，还占地方），要剔掉；「项目名称 + 建筑项目名称」
   都得留着 —— 一个是税局归的类目，一个是「干了什么活」，摘要靠它认人；
3. 普通货物发票那张表**不能被带坏**：它的名字区仍然是「项目名称 + 规格型号」。
"""
from __future__ import annotations

from decimal import Decimal

from app.invoice.text_parser import parse_page

# 一张建筑服务发票的文字层（购销方、税号、电话与账号已脱敏，明细与合计原样）。
BUILDING_PAGE_TEXT = """电子发票（普通发票） 发票号码：
开票日期：
购
买
方
信
息
统一社会信用代码/纳税人识别号：
销
售
方
信
息
统一社会信用代码/纳税人识别号：
名称： 名称：
项目名称 建筑服务发生地 建筑项目名称 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
建筑服务
24417000000066666666
2026年10月01日
测试购买方有限公司
91420100MA1234567X
测试建筑服务公司
91310000MA2AYXC79Y
¥9206.93 ¥92.07
玖仟贰佰玖拾玖圆整 ¥9299.00
测试开票人
测试开票人
*建筑服务*其他建筑服务 福建省泉州市丰泽区 零星修缮 9206.93 1% 92.07
土地增值税项目编号:-; 跨地（市）标志:否;
购买方地址:-; 电话:0595-00000000;
购方开户银行:测试银行某支行; 银行账号:0000000000000000;
销售方地址:测试地址; 电话:13600000000;
销方开户银行:测试银行某支行; 银行账号:0000000000000000000000
"""


def test_建筑服务票的明细提得出来():
    invoice = parse_page(0, BUILDING_PAGE_TEXT)
    assert len(invoice.lines) == 1, "明细一行都提不出来 —— 整张票会被判成没有明细"
    line = invoice.lines[0]
    assert line.amount == Decimal("9206.93")
    assert line.tax == Decimal("92.07")
    assert line.tax_rate == "1%"


def test_明细之和与票面合计分毫不差():
    invoice = parse_page(0, BUILDING_PAGE_TEXT)
    assert sum((line.amount for line in invoice.lines), Decimal("0")) == invoice.meta.total_excl_tax
    assert sum((line.tax for line in invoice.lines), Decimal("0")) == invoice.meta.total_tax
    assert invoice.meta.total_incl_tax == Decimal("9299.00")


def test_发生地那一列不进名字():
    """发生地是地址，对本账没用 —— 剔掉，摘要里不该有它。"""
    invoice = parse_page(0, BUILDING_PAGE_TEXT)
    name = invoice.lines[0].name
    assert "泉州市丰泽区" not in name, name
    assert "零星修缮" in name, f"建筑项目名称要留着：{name}"
    assert name.startswith("*建筑服务*"), name


def test_摘要就是界面上的那个名字():
    """界面上显示的就是摘要 —— 用户照着它写规则关键词，得能命中。"""
    invoice = parse_page(0, BUILDING_PAGE_TEXT)
    line = invoice.lines[0]
    assert line.display_name == line.name
    assert line.display_name in line.name


def test_普通货物发票的版式没被带坏():
    """表头放宽了，但普通票的名字区仍然是「项目名称 + 规格型号」。"""
    page = """电子发票（普通发票）
发票号码：24417000000033333333
开票日期：2026年10月01日
测试购买方有限公司
91420100MA1234567X
测试销售方有限公司
91310000MA2AYXC79Y
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
*调味品*调味品 6972343076094 件 1.00 6.02 6.02 13% 0.78
合 计 ¥6.02 ¥0.78
价税合计（大写）陆圆捌角整 （小写）¥6.80
开票人：测试开票人
"""
    invoice = parse_page(0, page)
    assert len(invoice.lines) == 1
    line = invoice.lines[0]
    assert line.amount == Decimal("6.02")
    assert line.tax == Decimal("0.78")
    # 规格型号仍在名字里 —— 科目规则按 name 匹配，带条码的关键词要能命中。
    assert "6972343076094" in line.name, line.name
    assert line.display_name == "*调味品*调味品", line.display_name
