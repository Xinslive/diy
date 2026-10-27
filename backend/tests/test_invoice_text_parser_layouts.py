"""发票的四种版式：免税、通行费、单位名、汉字间距。

这些版式都来自**同一份真实票**（35 页的增值税电子普通发票，35 页里四种
版式都撞见了）。原来的解析器在其中三种上会**静默出错**——不是报错，
是把错的东西写进凭证。

这里盯三件事：

1. **明细提得出来**。免税那一版式（税率那一栏印的是星号）原来整行都匹配
   不上，一条明细都提不出来，票被判成「没有可入账的发票明细」。
2. **买卖方别认错**。通行费那一版式的表头是「项目名称 车牌号 车辆类型 …」，
   里面的「行」字命中了「公司名后缀表」，于是**整行表头被当成了购方公司名**，
   真正的销方（高速公路公司）被挤掉，**销方变成了用户自己公司**——
   金额全对，凭证上的对方单位写成了自己，账错得悄无声息。
3. **别把不相干的列当数据**。通行日期（20260419 这种 8 位数）被当成了
   数量与单价；「车」「平方米」「季度」这些单位漏进了项目名。
"""
from __future__ import annotations

from decimal import Decimal

from app.invoice.text_parser import _looks_like_company, parse_page

#: 免税票（电信服务）：**税率与税额两栏都印的是星号**。
#:
#: 票面那一行是「*电信服务*电信服务 无 项 1 199.5 199.50 * *」——
#: 规格型号印的是「无」，单位是「项」，税率与税额都是「*」。
#: 金额 199.50、价税合计 199.50 ⇒ 税额 0。
#: （购销方、税号、账号、姓名已脱敏；版式、列序、金额原样。）
TAX_EXEMPT_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
26357000000144463158
2026年05月08日
测试购买方有限公司
91420100MA1234567X
测试电信服务有限公司
9135050075736499XL
¥199.50 *
壹佰玖拾玖圆伍角整 ¥199.50
张三
张三
*电信服务*电信服务 无 项 1 199.5 199.50 * *
销方开户银行：测试建设银行某支行 销方银行账号：35001652490050011449 销方地址电话：测试地址
电信大楼 10000
所属帐期：202604 客户号码：17750078087
"""

#: 通行费票：**版式不一样**，名字区后面是「车牌号 车辆类型 通行日期起
#: 通行日期止」四列，压根没有「单位 数量 单价」那几列。
#: 票面上还印着「通行费」这个票种标签（在「开票人：」后面那一行）。
TOLL_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 车牌号 车辆类型 通行日期起 通行日期止 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
通行费
26357910650500009426
2026年05月02日
测试购买方有限公司
91420100MA1234567X
测试高速公路集团有限公司
91350000158165606D
¥11.05
壹拾壹圆零伍分 ¥11.05
张三
张三
*经营租赁*代收通行费 闽C889BJ 客车 20260419 20260419 11.05 不征税
"""

#: 通行费票的另一张：PDF 把汉字**拆成单字**（「通 行 费」「通 行 费」），
#: 名字区那几个字会被拆成三个 token。
TOLL_SPACED_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 车牌号 车辆类型 通行日期起 通行日期止 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
通 行 费
26357912470700004436
2026年07月03日
测试购买方有限公司
91420100MA1234567X
测试高速公路有限责任公司
91350500MA347W001R
¥21.18 ¥0.64
贰拾 壹 圆捌角贰分 ¥21.82
张三
张三
*生产生活服务 *通 行 费 闽C889BJ 客车 20260608 20260608 21.18 3% 0.64
"""

# 不动产租赁票在双方信息之前还会出现「不动产经营租赁服务」字样；
# 该短语含「经营」，会被宽松的公司名特征误认成购方。
RENTAL_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 产权证书/不动产权证号 面积单位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
不动产经营租赁服务
26352000002132208046
2026年09月14日
测试租户有限公司
91420100MA1234567X
测试出租方有限公司
91350000158165606D
¥100.00 ¥3.00
壹佰零叁圆整 ¥103.00
张三
张三
*生产生活服务*房屋租赁
平方米 1 100 100.00 3% 3.00
"""

#: 货物/服务票：单位是「车」「平方米」「季度」——三种都**不在**原来的单位
#: 白名单里，于是它们会漏进项目名（「垃圾清运费车」「防火门平方米」）。
UNIT_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
26352000001158847831
2026年05月12日
测试购买方有限公司
91420100MA1234567X
测试园林绿化有限公司
91350502MA320YDJ5J
¥3168.32 ¥31.68
叁仟贰佰圆整 ¥3200.00
张三
张三
*生活服务*垃圾清运费 车 8 396.039603960396 3168.32 1% 31.68
"""

#: 同一版式，单位是「平方米」。
AREA_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
26352000001772287921
2026年07月25日
测试购买方有限公司
91420100MA1234567X
测试铝业有限公司
913505830665945425
¥33280.20 ¥332.80
叁万叁仟陆佰壹拾叁圆整 ¥33613.00
张三
张三
*金属制品*防火门 平方米 93.37 356.433522756795 33280.20 1% 332.80
"""

#: 电梯保养：单位是「季度」。原来的白名单里只有「季」和「度」两个单字，
#: 于是「季度」被砍成 unit=「度」、项目名只剩「…保养季」。
QUARTER_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
26942000000687356296
2026年05月31日
测试购买方有限公司
91420100MA1234567X
测试电梯有限公司厦门分公司
913502007692801796
¥12877.36 ¥772.64
壹万叁仟陆佰伍拾圆整 ¥13650.00
张三
张三
*生产生活服务*电梯保养 季度 1.00000000 12877.358490566037 12877.36 6% 772.64
"""

#: 预付卡：税率那一栏印的是「不征税」，合计行只有一个 ¥ 数字。
PREPAID_PAGE_TEXT = """电子发票（普通发票） 发票号码：
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
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
26357000000149514933
2026年05月25日
测试购买方有限公司
91420100MA1234567X
测试石油有限公司泉州分公司
91350500662837379M
¥1000.00
壹仟圆整 ¥1000.00
张三
张三
*预付卡销售*加油IC卡充
值款
元 1 1000 1000.00 不征税
"""


# ------------------------------------------------------------------ 免税票


def test_免税票的明细提得出来():
    """税率印成星号的那一版式，原来整行都匹配不上 —— 一条明细都没有。"""
    invoice = parse_page(0, TAX_EXEMPT_PAGE_TEXT)
    assert len(invoice.lines) == 1, "明细一行都提不出来 —— 整张票会被判成没有明细"
    line = invoice.lines[0]
    assert line.amount == Decimal("199.50")
    assert line.tax == Decimal("0"), "免税的票没有税额"
    assert line.tax_rate == "免税"
    assert line.quantity == Decimal("1")
    assert line.unit_price == Decimal("199.5")


def test_免税票的项目名干净():
    """规格型号那一格印的是「无」—— 那是「没有」，不是名字的一部分。"""
    line = parse_page(0, TAX_EXEMPT_PAGE_TEXT).lines[0]
    assert line.name == "*电信服务*电信服务", line.name
    assert line.display_name == "*电信服务*电信服务", line.display_name


def test_免税票的合计三件套齐全():
    """免税的票只有**两个** ¥ 数字（不含税与价税合计），凑三个是凑不出来的。"""
    meta = parse_page(0, TAX_EXEMPT_PAGE_TEXT).meta
    assert meta.total_excl_tax == Decimal("199.50"), meta.total_excl_tax
    assert meta.total_tax == Decimal("0"), meta.total_tax
    assert meta.total_incl_tax == Decimal("199.50"), meta.total_incl_tax


def test_免税票的金额校验不报警():
    invoice = parse_page(0, TAX_EXEMPT_PAGE_TEXT)
    # **要有明细**，校验才真的跑过 —— 没有明细时压根不校验，也就没有警告，
    # 那条「不报警」就成了空转。
    assert invoice.lines, "没有明细 —— 金额校验压根没跑，这条用例是空转"
    assert invoice.parse_warning == "", invoice.parse_warning


# ------------------------------------------------------------------ 通行费


def test_通行费票的表头没被当成购方公司名():
    """表头里「通**行**日期」的「行」命中了公司名后缀表 —— 整行表头被
    当成购方公司名，真正的销方（高速公路公司）被挤掉。"""
    meta = parse_page(0, TOLL_PAGE_TEXT).meta
    assert meta.buyer_name == "测试购买方有限公司", meta.buyer_name
    assert meta.seller_name == "测试高速公路集团有限公司", meta.seller_name


def test_通行费票的销方不是用户自己公司():
    """认错的后果比「名字写错」更糟：销方成了**自己公司**，
    凭证上的对方单位就写成了自己 —— 金额全对，账却是错的。

    （税号一直是认对的 —— 错的只是名字。所以这条钉的是**名字**。）
    """
    meta = parse_page(0, TOLL_PAGE_TEXT).meta
    assert meta.seller_name != "测试购买方有限公司", (
        f"销方认成了自己公司：{meta.seller_name}"
    )
    assert meta.seller_name == "测试高速公路集团有限公司", meta.seller_name


def test_通行费票的开票人不是票种标签():
    """「开票人：」后面那行是票种标签「通行费」，不是人名。"""
    meta = parse_page(0, TOLL_PAGE_TEXT).meta
    assert meta.issuer == "张三", meta.issuer


def test_通行日期没被当成数量与单价():
    """这一版式没有「数量 单价」两列，那两个 8 位数是通行日期。"""
    line = parse_page(0, TOLL_PAGE_TEXT).lines[0]
    assert line.quantity is None, line.quantity
    assert line.unit_price is None, line.unit_price
    assert line.amount == Decimal("11.05")
    assert line.tax_rate == "不征税"


def test_通行费的项目名带车牌号且不漏字():
    """PDF 把汉字拆成单字时（「通 行 费」），项目名不能被砍成「通行」。"""
    line = parse_page(0, TOLL_SPACED_PAGE_TEXT).lines[0]
    assert line.name == "*生产生活服务*通行费闽C889BJ", line.name
    assert "通行费" in line.name, line.name
    assert line.amount == Decimal("21.18")
    assert line.tax == Decimal("0.64")


def test_拆成单字的通行费票的买卖方也没认错():
    meta = parse_page(0, TOLL_SPACED_PAGE_TEXT).meta
    assert meta.buyer_name == "测试购买方有限公司", meta.buyer_name
    assert meta.seller_name == "测试高速公路有限责任公司", meta.seller_name
    assert meta.issuer == "张三", meta.issuer


def test_不动产租赁票按税号位置对应买卖方():
    meta = parse_page(0, RENTAL_PAGE_TEXT).meta
    assert meta.buyer_name == "测试租户有限公司", meta.buyer_name
    assert meta.seller_name == "测试出租方有限公司", meta.seller_name
    assert meta.buyer_tax_id == "91420100MA1234567X"
    assert meta.seller_tax_id == "91350000158165606D"


def test_表头行一眼就不是公司名():
    """这是「表头被当公司名」那条路的根因，单独钉住。"""
    header = "项目名称 车牌号 车辆类型 通行日期起 通行日期止 金 额 税率/征收率 税 额"
    assert not _looks_like_company(header)
    # 真的公司名照样认得出来
    assert _looks_like_company("测试高速公路集团有限公司")
    assert _looks_like_company("兴业银行股份有限公司泉州津淮支行")


# ------------------------------------------------------------------ 单位


def test_单位不漏进项目名_车():
    line = parse_page(0, UNIT_PAGE_TEXT).lines[0]
    assert line.name == "*生活服务*垃圾清运费", line.name
    assert line.display_name == "*生活服务*垃圾清运费", line.display_name
    assert line.unit == "车", line.unit


def test_单位不漏进项目名_平方米():
    line = parse_page(0, AREA_PAGE_TEXT).lines[0]
    assert line.name == "*金属制品*防火门", line.name
    assert line.display_name == "*金属制品*防火门", line.display_name
    assert line.unit == "平方米", line.unit


def test_单位不漏进项目名_季度():
    """「季度」原来被砍成 unit=「度」、项目名只剩「…保养季」。"""
    line = parse_page(0, QUARTER_PAGE_TEXT).lines[0]
    assert line.name == "*生产生活服务*电梯保养", line.name
    assert line.display_name == "*生产生活服务*电梯保养", line.display_name
    assert line.unit == "季度", line.unit
    assert line.amount == Decimal("12877.36")
    assert line.tax == Decimal("772.64")


# ------------------------------------------------------------------ 不征税


def test_不征税的预付卡():
    line = parse_page(0, PREPAID_PAGE_TEXT).lines[0]
    assert line.amount == Decimal("1000.00")
    assert line.tax_rate == "不征税"
    assert line.tax == Decimal("0")
    assert line.name == "*预付卡销售*加油IC卡充值款", line.name
    meta = parse_page(0, PREPAID_PAGE_TEXT).meta
    assert meta.total_excl_tax == Decimal("1000.00"), meta.total_excl_tax
    assert meta.total_tax == Decimal("0"), meta.total_tax
    assert meta.total_incl_tax == Decimal("1000.00"), meta.total_incl_tax


# ------------------------------------------------------------------ 汉字间距


def test_汉字之间的空格被去掉():
    """PDF 把一个汉字单独放一格（「福 建 省 泉 州紫欣工贸有限公司」）——
    那是排版产物，不是名字里的空格。"""
    page = TAX_EXEMPT_PAGE_TEXT.replace(
        "测试购买方有限公司", "福 建 省 泉 州紫欣工贸有限公司"
    )
    meta = parse_page(0, page).meta
    assert meta.buyer_name == "福建省泉州紫欣工贸有限公司", meta.buyer_name
