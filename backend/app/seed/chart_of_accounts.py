"""内置标准科目表（企业会计准则一级科目 + 常用明细科目）。

每个账套独立持有一份副本。导入时如果账套已存在同编码科目则跳过。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Subject

A, L, E, P = "ASSET", "LIABILITY", "EQUITY", "PROFIT"
DR, CR = "DEBIT", "CREDIT"

# (code, name, category, direction, carry_forward, [明细科目...])
CHART: list[tuple] = [
    ("1001", "库存现金", A, DR, False, []),
    ("1002", "银行存款", A, DR, False, [
        ("100201", "银行存款-工商银行", A, DR, False, []),
        ("100202", "银行存款-建设银行", A, DR, False, []),
        ("100203", "银行存款-微信支付", A, DR, False, []),
    ]),
    ("1012", "其他货币资金", A, DR, False, []),
    ("1101", "交易性金融资产", A, DR, False, []),
    ("1121", "应收票据", A, DR, False, []),
    ("1122", "应收账款", A, DR, False, []),
    ("1123", "预付款项", A, DR, False, []),
    ("1221", "其他应收款", A, DR, False, []),
    ("1401", "材料采购", A, DR, False, []),
    ("1402", "在途物资", A, DR, False, []),
    ("1403", "原材料", A, DR, False, []),
    ("1405", "库存商品", A, DR, False, []),
    ("1408", "委托加工物资", A, DR, False, []),
    ("1471", "存货跌价准备", A, CR, False, []),
    ("1511", "长期股权投资", A, DR, False, []),
    ("1601", "固定资产", A, DR, False, []),
    ("1602", "累计折旧", A, CR, False, []),
    ("1603", "在建工程", A, DR, False, []),
    ("1606", "固定资产清理", A, DR, False, []),
    ("1701", "无形资产", A, DR, False, []),
    ("1702", "累计摊销", A, CR, False, []),
    ("1801", "长期待摊费用", A, DR, False, []),
    ("2001", "短期借款", L, CR, False, []),
    ("2201", "应付票据", L, CR, False, []),
    ("2202", "应付账款", L, CR, False, []),
    ("2203", "预收账款", L, CR, False, []),
    ("2211", "应付职工薪酬", L, CR, False, []),
    ("2221", "应交税费", L, CR, False, []),
    ("2241", "其他应付款", L, CR, False, []),
    ("2501", "长期借款", L, CR, False, []),
    ("4001", "实收资本", E, CR, False, []),
    ("4002", "资本公积", E, CR, False, []),
    ("4101", "盈余公积", E, CR, False, []),
    ("4103", "本年利润", E, CR, False, []),
    ("4104", "利润分配-未分配利润", E, CR, False, []),
    ("6001", "主营业务收入", P, CR, True, [
        ("600101", "主营业务收入-租金", P, CR, True, []),
        ("600102", "主营业务收入-管理费", P, CR, True, []),
    ]),
    ("6051", "其他业务收入", P, CR, True, []),
    ("6111", "投资收益", P, CR, True, []),
    ("6301", "营业外收入", P, CR, True, []),
    ("6401", "主营业务成本", P, DR, True, [
        ("640101", "主营业务成本-租金", P, DR, True, []),
    ]),
    ("6402", "其他业务成本", P, DR, True, []),
    ("6403", "税金及附加", P, DR, True, []),
    ("6601", "销售费用", P, DR, True, []),
    ("6602", "管理费用", P, DR, True, []),
    ("6603", "财务费用", P, DR, True, []),
    ("6711", "营业外支出", P, DR, True, []),
    ("6801", "所得税费用", P, DR, True, []),
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
