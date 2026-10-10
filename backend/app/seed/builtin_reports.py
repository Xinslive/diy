"""内置报表模板：资产负债表、利润表。

模板与账套无关 —— 公式里引用的是「科目编码」而不是 ID，
所以一份模板可以直接用于所有账套。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReportCell, ReportCol, ReportRow, ReportTemplate

# 报表行定义：(row_no, 标题, 缩进, 加粗, 科目编码列表, 自定义公式模板)
# 公式模板里 {c} 会被替换成列字母
BALANCE_ROWS = [
    (1,  "流动资产", 0, True, [], None),
    (2,  "货币资金", 1, False, ["1001", "1002", "1012"], "sum"),
    (3,  "交易性金融资产", 1, False, ["1101"], "sum"),
    (4,  "应收票据", 1, False, ["1121"], "sum"),
    (5,  "应收账款", 1, False, ["1122"], "sum"),
    (6,  "预付款项", 1, False, ["1123"], "sum"),
    (7,  "其他应收款", 1, False, ["1221"], "sum"),
    (8,  "存货（扣跌价准备）", 1, False, ["1401", "1402", "1403", "1405", "1408", "-1471"], "sum"),
    (9,  "流动资产合计", 0, True, [], "合计({c}2:{c}8)"),
    (10, "非流动资产", 0, True, [], None),
    (11, "长期股权投资", 1, False, ["1511"], "sum"),
    (12, "固定资产（扣累计折旧）", 1, False, ["1601", "1606", "-1602"], "sum"),
    (13, "在建工程", 1, False, ["1603"], "sum"),
    (14, "无形资产（扣累计摊销）", 1, False, ["1701", "-1702"], "sum"),
    (15, "长期待摊费用", 1, False, ["1801"], "sum"),
    (16, "非流动资产合计", 0, True, [], "合计({c}11:{c}15)"),
    (17, "资产总计", 0, True, [], "{c}9+{c}16"),
    (18, "流动负债", 0, True, [], None),
    (19, "短期借款", 1, False, ["2001"], "sum"),
    (20, "应付票据", 1, False, ["2201"], "sum"),
    (21, "应付账款", 1, False, ["2202"], "sum"),
    (22, "预收账款", 1, False, ["2203"], "sum"),
    (23, "应付职工薪酬", 1, False, ["2211"], "sum"),
    (24, "应交税费", 1, False, ["2221"], "sum"),
    (25, "其他应付款", 1, False, ["2241"], "sum"),
    (26, "流动负债合计", 0, True, [], "合计({c}19:{c}25)"),
    (27, "非流动负债", 0, True, [], None),
    (28, "长期借款", 1, False, ["2501"], "sum"),
    (29, "非流动负债合计", 0, True, [], "{c}28"),
    (30, "负债合计", 0, True, [], "{c}26+{c}29"),
    (31, "所有者权益", 0, True, [], None),
    (32, "实收资本", 1, False, ["4001"], "sum"),
    (33, "资本公积", 1, False, ["4002"], "sum"),
    (34, "盈余公积", 1, False, ["4101"], "sum"),
    (35, "本年利润", 1, False, ["4103"], "sum"),
    (36, "未分配利润", 1, False, ["4104"], "sum"),
    (37, "所有者权益合计", 0, True, [], "合计({c}32:{c}36)"),
    (38, "负债和所有者权益总计", 0, True, [], "{c}30+{c}37"),
    (39, "平衡校验（应为 0）", 0, False, [], "{c}17-{c}38"),
]

INCOME_ROWS = [
    (1,  "一、营业收入", 0, False, ["6001", "6051"], "sum_amount"),
    (2,  "减：营业成本", 0, False, ["6401", "6402"], "sum_amount"),
    (3, "    税金及附加", 0, False, ["6403"], "sum_amount"),
    (4, "    销售费用", 0, False, ["6601"], "sum_amount"),
    (5, "    管理费用", 0, False, ["6602"], "sum_amount"),
    (6, "    财务费用", 0, False, ["6603"], "sum_amount"),
    (7, "加：投资收益", 0, False, ["6111"], "sum_amount"),
    (8, "二、营业利润", 0, True, [], "{c}1-{c}2-{c}3-{c}4-{c}5-{c}6+{c}7"),
    (9, "加：营业外收入", 0, False, ["6301"], "sum_amount"),
    (10, "减：营业外支出", 0, False, ["6711"], "sum_amount"),
    (11, "三、利润总额", 0, True, [], "{c}8+{c}9-{c}10"),
    (12, "减：所得税费用", 0, False, ["6801"], "sum_amount"),
    (13, "四、净利润", 0, True, [], "{c}11-{c}12"),
]

BALANCE_COLS = [
    (1, "项目", "STATIC", False, 260),
    (2, "期末余额", "BALANCE_CLOSING", False, 140),
    (3, "年初余额", "BALANCE_OPENING", False, 140),
]

INCOME_COLS = [
    (1, "项目", "STATIC", False, 260),
    (2, "本期金额", "PERIOD_AMOUNT", False, 140),
    (3, "本年累计金额", "YTD_AMOUNT", False, 140),
]

TEMPLATES = [
    {
        "code": "BALANCE_SHEET",
        "name": "资产负债表",
        "type": "BALANCE_SHEET",
        "cols": BALANCE_COLS,
        "rows": BALANCE_ROWS,
        "kind_of": "期末" if False else None,
    },
    {
        "code": "INCOME_SHEET",
        "name": "利润表",
        "type": "INCOME_SHEET",
        "cols": INCOME_COLS,
        "rows": INCOME_ROWS,
    },
]

KIND_FOR = {
    "BALANCE_CLOSING": "期末余额",
    "BALANCE_OPENING": "期初余额",
    "PERIOD_AMOUNT": "本期金额",
    "YTD_AMOUNT": "本年累计",
    "PERIOD_DEBIT": "本期借方",
    "PERIOD_CREDIT": "本期贷方",
}


def _formula(kind: str, codes: list[str], template: str | None) -> str:
    """科目编码前加 '-' 表示减项（如累计折旧、跌价准备）。"""
    if template in ("sum", "sum_amount") and codes:
        parts = []
        for c in codes:
            sign = "-" if c.startswith("-") else "+"
            parts.append(f'{sign}科目("{c.lstrip("-")}","{KIND_FOR[kind]}",{{{{本期}}}})')
        return parts[0].lstrip("+") + "".join(parts[1:])
    if template:
        from app.reports.formula import no_to_col

        return template.replace("{c}", no_to_col(2))
    return ""


def install(db: Session) -> None:
    for spec in TEMPLATES:
        tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == spec["code"]))
        if tpl is None:
            tpl = ReportTemplate(
                code=spec["code"], name=spec["name"], type=spec["type"], is_builtin=True
            )
            db.add(tpl)
            db.flush()
        else:
            for c in list(db.scalars(select(ReportCol).where(ReportCol.template_id == tpl.id))):
                db.delete(c)
            for r in list(db.scalars(select(ReportRow).where(ReportRow.template_id == tpl.id))):
                db.delete(r)
            for c in list(db.scalars(select(ReportCell).where(ReportCell.template_id == tpl.id))):
                db.delete(c)
            db.flush()

        for col_no, label, kind, hidden, width in spec["cols"]:
            db.add(
                ReportCol(
                    template_id=tpl.id, col_no=col_no, label=label,
                    kind=kind, hidden=hidden, width=width,
                )
            )
        db.flush()

        cols = list(
            db.scalars(
                select(ReportCol).where(ReportCol.template_id == tpl.id).order_by(ReportCol.col_no)
            ).all()
        )

        for row_no, label, indent, bold, codes, formula_tpl in spec["rows"]:
            db.add(
                ReportRow(
                    template_id=tpl.id, row_no=row_no, label=label,
                    indent=indent, bold=bold, hidden=False, style={},
                )
            )
        db.flush()

        from app.reports.formula import no_to_col

        for row_no, label, indent, bold, codes, formula_tpl in spec["rows"]:
            for col in cols:
                if col.kind == "STATIC":
                    formula = ""
                elif formula_tpl and "{c}" in formula_tpl:
                    formula = formula_tpl.replace("{c}", no_to_col(col.col_no))
                else:
                    formula = _formula(col.kind, codes, formula_tpl)
                db.add(
                    ReportCell(
                        template_id=tpl.id, row_no=row_no, col_no=col.col_no, formula=formula
                    )
                )
        db.flush()
