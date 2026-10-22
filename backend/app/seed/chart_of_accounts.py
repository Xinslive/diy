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

from sqlalchemy import insert, select, update
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


def install_chart(db: Session, book_id: int, year: int | None = None,
                  overwrite: bool = False) -> int:
    """把标准科目表装入**某个年度**，返回新增科目数。

    **科目表每个年度各有一份**（迁移 0032），所以这里必须说清楚装到哪一年。
    ``year`` 不传时装到**账套的启用年度**（``book.start_year``）——
    那是最常见的场景：新账套的第一张科目表。

    装到别的年度（年度结转时给新年度铺一份）走
    ``accounting.years.copy_chart``，它是从已有科目表**复制**而不是重装，
    差别在于复制会带上用户自己的改动（停用、改名、子科目）。
    """
    if year is None:
        from app.models import Book

        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"账套不存在: {book_id}")
        year = book.start_year
    else:
        from app.models import Book

        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"账套不存在: {book_id}")

    # **年度账套行必须先于科目存在**：subject 上有一条指向
    # ``book_year(book_id, year)`` 的组合外键（迁移 0032），
    # 年度没登记就插科目会得到一条「违反外键约束」的外键错误，
    # 而它真正的原因是「这个年度还没登记」。
    from app.accounting.years import ensure_book_year

    ensure_book_year(db, book, year)

    if overwrite:
        # **先把 parent_id 全置空，再删。**
        #
        # ``subject.parent_id`` 是指向自己的外键，而且**没有 ON DELETE CASCADE**。
        # 原来这里逐个 ``db.delete(s)`` 之后 flush，ORM 按 id 顺序发 DELETE，
        # 父科目先被删、子科目还指着它 —— 于是撞 ``subject_parent_id_fkey``，
        # 整条 ``overwrite=True`` 路径是**坏的**（全仓无人调用，所以一直没暴露）。
        #
        # 先置空 parent_id 就没有先后顺序问题了：树在应用层被拆成
        # 「一堆 parent_id IS NULL 的行」，再逐个删，外键不再指向任何被删的行。
        db.execute(
            update(Subject)
            .where(Subject.book_id == book_id, Subject.year == year)
            .values(parent_id=None)
        )
        for s in db.scalars(select(Subject).where(
                Subject.book_id == book_id, Subject.year == year)).all():
            db.delete(s)
        db.flush()

    existing = set(db.scalars(select(Subject.code).where(
        Subject.book_id == book_id, Subject.year == year)).all())

    # ---- 先把整棵树摊平成一条条记录，再按层级批量插 ----------------------
    #
    # **为什么不再逐条 flush。** 原来这里是 ``db.add(s); db.flush()`` 一条一次：
    # 140 个科目 = 140 次 INSERT 往返 PostgreSQL。**而这条路径是热路径** ——
    # ① 新建账套要装一次标准科目表；② 每一条会计用例的 ``book`` 夹具都要装
    # 一次（夹具 = 「12 个期间 + 标准科目表」）。于是「每条用例多花 ~0.15 秒
    # 全花在 140 次往返上」，全量 2000+ 条用例就是几百秒的纯网络往返。
    #
    # 改成**按层级批量**：先插第 1 层（一级科目），拿回它们的 id，再插第 2 层
    # （子科目带上刚拿到的 parent_id），如此往下。140 条科目从 140 次往返降到
    # 「层数次 INSERT + 层数次 SELECT 读回 id」，本表 3 层 = 6 次往返。
    #
    # **语义保持不变**（这几点是刻意对齐原实现的，别当成可以省的细节）：
    #   · 已存在的编码要**连同整棵子树一起跳过** —— 原来 ``add()`` 命中
    #     ``existing`` 就 ``return None``，根本不往下递归。父科目在、子孙不在的
    #     情形下，老实现不会去补那些孤儿；
    #   · ``existing`` 是**插之前**查出来的快照，不往里加新插进去的编码 ——
    #     与老实现一致（老实现也没往里加）；
    #   · ``sort_no`` 按**同级**序号 ×10，``is_leaf`` = 没有子科目；
    #   · ``parent_id`` 指向**刚插进去**的父科目，不是同名同编码的历史行。
    #
    # 库层的护栏照样生效：``trg_subject_leaf_guard`` 是 BEFORE INSERT FOR EACH ROW，
    # 批量插入逐行触发；``trg_subject_leaf_parent_guard`` 是 DEFERRABLE 的
    # 约束触发器，提交时判「父科目不能是末级」—— 父级先插且 ``is_leaf=False``，
    # 两条都过。
    flat: list[tuple] = []

    def walk(rows, level, parent_code):
        for i, (code, name, cat, dirn, cf, children) in enumerate(rows, start=1):
            if code in existing:
                # 父科目已经有了 → 它的子孙老实现也不补，整棵子树跳过。
                continue
            flat.append((code, name, cat, dirn, cf, level, parent_code, i * 10, not children))
            walk(children, level + 1, code)

    walk(CHART, 1, None)
    count = len(flat)
    if not count:
        return 0

    id_by_code: dict[str, int] = {}
    for level in sorted({row[5] for row in flat}):
        rows = [r for r in flat if r[5] == level]
        payload = [
            {
                "book_id": book_id, "year": year, "code": code, "name": name,
                "category": category, "direction": direction,
                "carry_forward": carry, "level": lv, "parent_id": id_by_code.get(parent_code),
                "is_leaf": is_leaf, "sort_no": sort_no, "active": True, "ext_attrs": {},
            }
            for (code, name, category, direction, carry, lv, parent_code, sort_no, is_leaf) in rows
        ]
        db.execute(insert(Subject), payload)
        # 读回这一层的 id，给下一层当 parent_id。
        # （用 code 反查而不是依赖 RETURNING 的顺序：批量 INSERT 的行序
        #   不该成为「父子关系对不对」的前提。）
        for code, sid in db.execute(
            select(Subject.code, Subject.id).where(
                Subject.book_id == book_id, Subject.year == year,
                Subject.code.in_([r[0] for r in rows])),
        ):
            id_by_code[code] = sid

    db.flush()
    return count
