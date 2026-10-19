"""内置标准科目表 —— 用友 4-2-2 编码（来自《基线科目表.xls》）。

**为什么换掉上一版。** 上一版是照 2007 版《企业会计准则》编号手工列的
（6001 主营业务收入、4001 实收资本、6401 主营业务成本、6601 销售费用……）。
但从用友导入的真实账套本来就是另一套编号：1101 短期投资、5001 主营业务收入、
3001 实收资本，压根没有 4001 / 4101 / 6001。于是默认科目和真实账对不上，
而内置报表（会小企01表资产负债表、利润表）是**按科目编码**取数的，换过去之后：

* 17 个编码在新表里不存在（4002/4103/4104/6001/6051/6111/6301/6401/6402/
  6403/6601/6602/6603/6711/6801/1603/1471），那些行会直接空掉；
* 更糟的是 4 个**同码不同义**——4001 从「实收资本」变成「生产成本」、
  4101 从「盈余公积」变成「制造费用」、1101 从「交易性金融资产」变成
  「短期投资」、1123 从「预付款项」变成「预付账款」。这种不会报错，
  报表会**静默取到另一个科目的数**，比空掉更难发现。

所以换表的同时，内置报表的取数编码一起按科目名迁到新编号
（见 ``builtin_reports.py`` 里那张对照表）。

**只影响新建账套，已有账套一律不动。** 启明物业 209 张凭证、星悦荟商管
557 张凭证 + 178 行期初、压测账套 15000 张凭证，都指着各自账套里现有的编码；
改编码就是改历史账。``install_chart`` 本身就是「已存在同编码就跳过」，
不会去覆盖任何已有科目。

**两处这份表里没有的东西，是有意留空的。**

* 1471 存货跌价准备：《基线科目表.xls》里没有，会小企01表「存货」行本来要
  减掉它。科目都不存在，谈不上减值准备，所以那一项去掉了 —— 这跟
  《小企业会计准则》「不存在的交易可以不设置科目」是一回事。
* 成本类（4001 生产成本、4101 制造费用、4301 研发支出、4401 工程施工、
  4403 机械作业）**不参与结转损益**：成本月末是滚进主营业务成本，
  不是直接结进损益，所以 ``carry_forward`` 只给损益类开。

编码规则 4-2-2（4 位大类 + 2 位 + 2 位），最深 3 级。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Subject

A, L, E, C, P = "ASSET", "LIABILITY", "EQUITY", "COST", "PROFIT"
DR, CR = "DEBIT", "CREDIT"

# (code, name, category, direction, carry_forward, [明细科目...])
CHART: list[tuple] = [
    ("1001", "库存现金", A, DR, False, []),
    ("1002", "银行存款", A, DR, False, []),
    ("1012", "其他货币资金", A, DR, False, []),
    ("1101", "短期投资", A, DR, False, [
        ("110101", "股票", A, DR, False, []),
        ("110102", "债券", A, DR, False, []),
        ("110103", "基金", A, DR, False, []),
        ("110110", "其他", A, DR, False, []),
    ]),
    ("1121", "应收票据", A, DR, False, []),
    ("1122", "应收账款", A, DR, False, []),
    ("1123", "预付账款", A, DR, False, []),
    ("1131", "应收股利", A, DR, False, []),
    ("1132", "应收利息", A, DR, False, []),
    ("1221", "其他应收款", A, DR, False, []),
    ("1401", "材料采购", A, DR, False, []),
    ("1402", "在途物资", A, DR, False, []),
    ("1403", "原材料", A, DR, False, []),
    ("1404", "材料成本差异", A, DR, False, []),
    ("1405", "库存商品", A, DR, False, []),
    ("1407", "商品进销差价", A, DR, False, []),
    ("1408", "委托加工物资", A, DR, False, []),
    ("1411", "周转材料", A, DR, False, []),
    ("1421", "消耗性生物资产", A, DR, False, []),
    ("1501", "长期债券投资", A, DR, False, []),
    ("1511", "长期股权投资", A, DR, False, []),
    ("1601", "固定资产", A, DR, False, []),
    ("1602", "累计折旧", A, CR, False, []),
    ("1604", "在建工程", A, DR, False, []),
    ("1605", "工程物资", A, DR, False, []),
    ("1606", "固定资产清理", A, DR, False, []),
    ("1621", "生产性生物资产", A, DR, False, []),
    ("1622", "生产性生物资产累计折旧", A, CR, False, []),
    ("1701", "无形资产", A, DR, False, []),
    ("1702", "累计摊销", A, CR, False, []),
    ("1801", "长期待摊费用", A, DR, False, []),
    ("1901", "待处理财产损溢", A, DR, False, []),
    ("2001", "短期借款", L, CR, False, []),
    ("2201", "应付票据", L, CR, False, []),
    ("2202", "应付账款", L, CR, False, []),
    ("2203", "预收账款", L, CR, False, []),
    ("2211", "应付职工薪酬", L, CR, False, [
        ("221101", "应付职工工资", L, CR, False, []),
        ("221102", "应付奖金、津贴和补贴", L, CR, False, []),
        ("221103", "应付福利费", L, CR, False, []),
        ("221104", "应付社会保险费", L, CR, False, []),
        ("221105", "应付住房公积金", L, CR, False, []),
        ("221106", "应付工会经费", L, CR, False, []),
        ("221107", "应付教育经费", L, CR, False, []),
        ("221108", "非货币性福利", L, CR, False, []),
        ("221109", "辞退福利", L, CR, False, []),
        ("221110", "其他应付职工薪酬", L, CR, False, []),
    ]),
    ("2221", "应交税费", L, CR, False, [
        ("222101", "应交增值税", L, CR, False, [
            ("22210101", "进项税额", L, CR, False, []),
            ("22210106", "销项税额", L, CR, False, []),
            ("22210107", "进项税额转出", L, CR, False, []),
        ]),
        ("222102", "未交增值税", L, CR, False, []),
        ("222103", "应交营业税", L, CR, False, []),
        ("222104", "应交消费税", L, CR, False, []),
        ("222105", "应交资源税", L, CR, False, []),
        ("222106", "应交所得税", L, CR, False, []),
        ("222107", "应交土地增值税", L, CR, False, []),
        ("222108", "应交城市维护建设税", L, CR, False, []),
        ("222109", "应交房产税", L, CR, False, []),
        ("222110", "应交城镇土地使用税", L, CR, False, []),
        ("222111", "应交车船使用税", L, CR, False, []),
        ("222112", "应交个人所得税", L, CR, False, []),
        ("222113", "教育费附加", L, CR, False, []),
        ("222114", "矿产资源补偿费", L, CR, False, []),
        ("222115", "排污费", L, CR, False, []),
        ("222116", "增值税留抵税额", L, CR, False, []),
        ("222117", "减免税款", L, CR, False, []),
    ]),
    ("2231", "应付利息", L, CR, False, []),
    ("2232", "应付利润", L, CR, False, []),
    ("2241", "其他应付款", L, CR, False, []),
    ("2401", "递延收益", L, CR, False, []),
    ("2501", "长期借款", L, CR, False, []),
    ("2701", "长期应付款", L, CR, False, []),
    ("3001", "实收资本", E, CR, False, []),
    ("3002", "资本公积", E, CR, False, []),
    ("3101", "盈余公积", E, CR, False, [
        ("310101", "法定盈余公积", E, CR, False, []),
        ("310102", "任意盈余公积", E, CR, False, []),
    ]),
    ("3103", "本年利润", E, CR, False, []),
    ("3104", "利润分配", E, CR, False, [
        ("310401", "其他转入", E, CR, False, []),
        ("310402", "提取法定盈余公积", E, CR, False, []),
        ("310403", "提取法定公益金", E, CR, False, []),
        ("310404", "提取职工奖励及福利基金", E, CR, False, []),
        ("310409", "提取任意盈余公积", E, CR, False, []),
        ("310410", "应付利润", E, CR, False, []),
        ("310415", "未分配利润", E, CR, False, []),
    ]),
    ("4001", "生产成本", C, DR, False, []),
    ("4101", "制造费用", C, DR, False, []),
    ("4301", "研发支出", C, DR, False, []),
    ("4401", "工程施工", C, DR, False, []),
    ("4403", "机械作业", C, DR, False, []),
    ("5001", "主营业务收入", P, CR, True, []),
    ("5051", "其他业务收入", P, CR, True, []),
    ("5111", "投资收益", P, CR, True, []),
    ("5301", "营业外收入", P, CR, True, [
        ("530101", "政府补助", P, CR, True, []),
        ("530102", "收回坏账损失", P, CR, True, []),
        ("530103", "汇兑收益", P, CR, True, []),
        ("530104", "非流动资产处置净收益", P, CR, True, []),
    ]),
    ("5401", "主营业务成本", P, DR, True, []),
    ("5402", "其他业务成本", P, DR, True, []),
    ("5403", "营业税金及附加", P, DR, True, []),
    ("5601", "销售费用", P, DR, True, [
        ("560101", "商品维修费", P, DR, True, []),
        ("560102", "广告费", P, DR, True, []),
        ("560103", "业务宣传费", P, DR, True, []),
        ("560104", "交通费", P, DR, True, []),
        ("560105", "通讯费", P, DR, True, []),
        ("560106", "业务招待费", P, DR, True, []),
        ("560107", "员工工资", P, DR, True, []),
    ]),
    ("5602", "管理费用", P, DR, True, [
        ("560201", "开办费", P, DR, True, []),
        ("560202", "业务招待费", P, DR, True, []),
        ("560203", "研究费用", P, DR, True, []),
        ("560204", "交通费", P, DR, True, []),
        ("560205", "通讯费", P, DR, True, []),
        ("560206", "水电费", P, DR, True, []),
        ("560207", "房屋租赁费", P, DR, True, []),
        ("560208", "员工活动费", P, DR, True, []),
        ("560209", "员工工资", P, DR, True, []),
        ("560210", "折旧", P, DR, True, []),
        ("560211", "无形资产摊销", P, DR, True, []),
    ]),
    ("5603", "财务费用", P, DR, True, [
        ("560301", "利息费用", P, DR, True, []),
        ("560302", "手续费用", P, DR, True, []),
        ("560303", "现金折扣", P, DR, True, []),
        ("560304", "汇兑损失", P, DR, True, []),
    ]),
    ("5711", "营业外支出", P, DR, True, [
        ("571101", "坏账损失", P, DR, True, []),
        ("571102", "无法收回的长期债券投资损失", P, DR, True, []),
        ("571103", "无法收回的长期股权投资损失", P, DR, True, []),
        ("571104", "自然灾害等不可抗力因素造成的损失", P, DR, True, []),
        ("571105", "税收滞纳金", P, DR, True, []),
    ]),
    ("5801", "所得税费用", P, DR, True, []),
]


def install_chart(db: Session, book_id: int, overwrite: bool = False) -> int:
    """把标准科目表装入账套，返回新增科目数。"""
    if overwrite:
        for s in db.scalars(select(Subject).where(Subject.book_id == book_id)).all():
            db.delete(s)
        db.flush()

    existing = set(db.scalars(select(Subject.code).where(Subject.book_id == book_id)).all())
    count = 0

    def add(code, name, category, direction, carry, children, level, parent_id, sort_no):
        nonlocal count
        if code in existing:
            return None
        s = Subject(
            book_id=book_id, code=code, name=name, category=category, direction=direction,
            carry_forward=carry, level=level, parent_id=parent_id, is_leaf=not children,
            sort_no=sort_no, active=True, ext_attrs={},
        )
        db.add(s)
        db.flush()
        count += 1
        for i, (cc, cn, ccat, cdir, ccarry, gchildren) in enumerate(children, start=1):
            add(cc, cn, ccat, cdir, ccarry, gchildren, level + 1, s.id, i * 10)
        return s

    for i, (code, name, cat, dirn, cf, children) in enumerate(CHART, start=1):
        add(code, name, cat, dirn, cf, children, 1, None, i * 10)

    db.flush()
    return count
