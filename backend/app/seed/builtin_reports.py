"""内置报表模板：资产负债表、利润表。

模板与账套无关 —— 公式里引用的是「科目编码」而不是 ID，
所以一份模板可以直接用于所有账套。

**资产负债表是「双栏表」**
------------------------
资产负债表按《小企业会计准则》印成左右两栏：左边是资产，右边是负债和所有者权益，
每一行同时承载两个项目。因此这里的一行 = 两个 Side（左右各一个），
而不是当年那种「一行一个项目 + 若干数值列」的竖排结构。

8 列的分工::

    1 资产 STATIC │ 2 行次 LINE_NO │ 3 期末余额 │ 4 年初余额
    5 负债和所有者权益 STATIC │ 6 行次 LINE_NO │ 7 期末余额 │ 8 年初余额

同一行的左右两半各自独立取数：数值列 C/D 属于资产侧，G/H 属于负债权益侧。
公式里的行引用因此也分两套 —— 合计行的 {c} 会按所在列展开成
``C2+C3+…`` 或 ``G2+G3+…``，互不串味。

**行次存在 style 里**
--------------------
行次（1~53）是《小企业会计准则》规定的固定编号，不是算出来的数。
它连同右栏标题、行性质（SECTION/ITEM/TOTAL）一起放在
``ReportRow.style`` 这个 JSONB 里：表结构里没有、也不该为了一张表新增列，
而 style 本来就是留给「这一行长什么样」的自由字段。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReportCell, ReportCol, ReportRow, ReportTemplate

# --------------------------------------------------------------------- 双栏定义
#
# Side: 一栏里的一个项目
#   label   —— 打印出来的项目名
#   codes   —— 取哪些科目的和（编码前加 '-' 表示减项，如累计折旧、跌价准备）
#   formula —— 自定义公式模板，{c} 会按所在列展开成列字母；为 None 表示这一格留空
#   indent  —— 缩进层级（其中：类子项目用 1）
#   bold    —— 加粗
#   kind    —— SECTION 段标题 / ITEM 普通项目 / TOTAL 合计小计
#
# **kind 必须按「侧」记，不能按行记。** 资产负债表的同一行上，左右两半常常不是
# 同一性质：第 16 行左边是「流动资产合计」（要加粗 + 底纹），右边是「递延收益」
# （一个普通项目，不该跟着加粗）。kind 只挂在行上的话，这一行要么两半都不加粗
# （合计行看起来跟正文一样），要么两半都加粗（把「递延收益」也印成合计）。
#
# Line: 打印上的一行，左右各一个 Side
#   kind       —— 整行的性质，只作为两侧的默认值
#   line_left  —— 左栏行次（None = 不显示行次，段标题与空行都属这一类）
#   line_right —— 右栏行次
#   hidden     —— 默认不显示（平衡校验行：官方表没有它，但要看得到得能翻出来）


@dataclass(frozen=True)
class Side:
    label: str = ""
    codes: tuple[str, ...] = ()
    formula: str | None = None
    indent: int = 0
    bold: bool = False
    kind: str = "ITEM"


@dataclass(frozen=True)
class Line:
    left: Side = field(default_factory=Side)
    right: Side = field(default_factory=Side)
    kind: str = "ITEM"
    line_left: int | None = None
    line_right: int | None = None
    hidden: bool = False


def _s(label, codes=(), indent=1, bold=False, formula=None, kind="ITEM") -> Side:
    """项目侧的简写。codes 为空且无公式 = 这一格留空（表里有行、账上没科目）。"""
    return Side(label=label, codes=tuple(codes), indent=indent, bold=bold,
                formula=formula, kind=kind)


def _shade(label) -> Side:
    """压灰的格。

    官方模板里一共有且只有 7 个灰格：四个段标题（流动资产：/ 流动负债：/
    非流动资产：/ 非流动负债：）加最外层的三个总计（负债合计 / 资产总计 /
    负债和所有者权益（或股东权益）总计）。**夹在中间的「流动资产合计」
    「非流动负债合计」「所有者权益（或股东权益）合计」是白底** ——
    早先一律压灰，打出来就不是那一张纸了。
    """
    return _s(label, indent=0, kind="SHADE")


def _head(label) -> Side:
    """段标题，但不压灰。

    「所有者权益（或股东权益）」就是这一类：它不缩进、不带行次，
    可官方模板偏偏没给它上底色（四个段标题里唯独它没有）。照抄。
    """
    return _s(label, indent=0, kind="HEAD")


def _gray(label, formula=None, indent=0) -> Side:
    """压灰且带公式 —— 最外层的三个总计（负债合计 / 资产总计 /
    负债和所有者权益（或股东权益）总计）。它们和段标题一样是灰底，
    区别只是要算得出数、而且有的带行次。"""
    return _s(label, formula=formula, indent=indent, kind="SHADE")


# --------------------------------------------------------------------- 资产负债表
#
# 科目编码取自 app/seed/chart_of_accounts.py 的标准科目表。表里有些行
# （应收股利、在产品、周转材料…）本项目的科目表没有对应科目，留空由财务在
# 设计模式里自己填 —— 比替他们挂一个不对口的科目诚实。
#
# 减项的符号：``科目(...)`` 配「期末余额」口径时已经按科目自然方向取正数
# （负债/权益类贷方余额取正，见 formula.py::_f_subject），所以累计折旧、
# 存货跌价准备这类备抵科目编码前加 ``-`` 就是从合计里减掉。
B_LIQUID = ("1001", "1002", "1012")            # 货币资金
B_STOCK = ("1401", "1402", "1403", "1405", "1408", "-1471")   # 存货（扣跌价准备）

#: 资产侧合计：货币资金…存货 + 其他流动资产。
#: 「其中：原材料/在产品/库存商品/周转材料」是存货的**子项**，不能重复加进来，
#: 所以这里逐行列举而不是写一个 ``合计({c}2:{c}15)`` 的连续区域。
#:
#: **必须逐个写 ``{c}``，不能只写一个列字母。** 写成 "C2+C3+…" 的话，
#: 同一串公式会原样落进年初余额那列（D），于是「流动资产合计」的年初余额
#: 变成了期末余额各项的和 —— 一张看起来正常、数值却错一列的表。
#: ``{c}`` 按所在列展开，两侧四个数值列（C/D/G/H）各自拿自己的列字母。
B_CURRENT_ASSETS = (
    "{c}2+{c}3+{c}4+{c}5+{c}6+{c}7+{c}8+{c}9+{c}10+{c}15"
)
#: 非流动资产合计：长期债券投资…其他非流动资产（跳过「固定资产原价 / 减：累计折旧」
#: 这两行 —— 它们是「固定资产账面价值」的组成部分，被 r22 = r20 - r21 收进去了）
B_NONCURRENT_ASSETS = (
    "{c}18+{c}19+{c}22+{c}23+{c}24+{c}25+{c}26+{c}27+{c}28+{c}29+{c}30"
)

# 逐行对照财政部《小企业会计准则》「会小企01表」（以财务提供的官方 Excel 模板
# 逐格核对）。**左右两半的行配对不能凭印象排** —— 所有者权益段配的是
# 「生产性生物资产」(行次 24)，不是「在建工程」(行次 21)；早先版本差了 7 行，
# 右半整块往上错，印出来根本不是那一张表。
#
# 缩进层级（官方表用空格前缀，这里换成 indent）：0 = 段标题与最外层总计，
# 1 = 普通项目（2 空格），2 = 存货的其中项（5 空格）。
# 「其中:原材料」在官方表里是 0 空格，与其它其中项不同，这里照抄不"修正"。
BALANCE_ROWS: list[Line] = [
    # -- 流动资产 / 流动负债 --
    Line(_shade("流动资产："), _shade("流动负债：")),
    Line(_s("货币资金", B_LIQUID), _s("短期借款", ("2001",)), line_left=1, line_right=31),
    Line(_s("短期投资", ("1101",)), _s("应付票据", ("2201",)), line_left=2, line_right=32),
    Line(_s("应收票据", ("1121",)), _s("应付账款", ("2202",)), line_left=3, line_right=33),
    Line(_s("应收账款", ("1122",)), _s("预收账款", ("2203",)), line_left=4, line_right=34),
    Line(_s("预付账款", ("1123",)), _s("应付职工薪酬", ("2211",)), line_left=5, line_right=35),
    Line(_s("应收股利", ("1131",)), _s("应交税费", ("2221",)), line_left=6, line_right=36),
    Line(_s("应收利息", ("1132",)), _s("应付利息", ("2231",)), line_left=7, line_right=37),
    Line(_s("其他应收款", ("1221",)), _s("应付利润", ("2232",)), line_left=8, line_right=38),
    Line(_s("存货", B_STOCK), _s("其他应付款", ("2241",)), line_left=9, line_right=39),
    # 官方表这一行是 0 空格（与下面三个其中项的 5 空格不同），照抄。
    Line(_s("其中:原材料", ("1403",), indent=0), _s("其他流动负债"),
         line_left=10, line_right=40),
    Line(_s("在产品", indent=2),
         _s("流动负债合计", formula="{c}2+{c}3+{c}4+{c}5+{c}6+{c}7+{c}8+{c}9+{c}10+{c}11"),
         line_left=11, line_right=41),
    Line(_s("库存商品", ("1405",), indent=2), _shade("非流动负债："), line_left=12),
    Line(_s("周转材料", ("1411",), indent=2), _s("长期借款", ("2501",)), line_left=13, line_right=42),
    Line(_s("其他流动资产"), _s("长期应付款", ("2701",)), line_left=14, line_right=43),
    # 左边是合计、右边是「递延收益」—— 这一行两半不同性质，
    # 靠 Side.kind 分别记账，行级 kind 在这里保持默认 ITEM。
    Line(_s("流动资产合计", formula=B_CURRENT_ASSETS), _s("递延收益", ("2401",)),
         line_left=15, line_right=44),
    # -- 非流动资产 / 非流动负债 --
    Line(_shade("非流动资产："), _s("其他非流动负债"), line_right=45),
    Line(_s("长期债券投资", ("1501",)), _s("非流动负债合计", formula="{c}14+{c}15+{c}16+{c}17"),
         line_left=16, line_right=46),
    # 负债合计是**压灰**的最外层总计（官方表行 23 E 列），
    # 而上面的非流动负债合计只是白底小计 —— 不是同一个待遇。
    Line(_s("长期股权投资", ("1511",)), _gray("负债合计", "{c}12+{c}18", indent=0),
         line_left=17, line_right=47),
    Line(_s("固定资产原价", ("1601",)), Side(), line_left=18),
    # 减项按《小企业会计准则》**显示正数**（表上就是"减：累计折旧"这一行，
    # 账面价值再由 r22 = r20 - r21 减掉）。科目("1602","期末余额") 本身
    # 已经按贷方方向取正数（evaluate.py::_orient），这里再挂一个负号
    # 就会变成「原价 + 折旧」。
    Line(_s("减：累计折旧", ("1602",)), Side(), line_left=19),
    # 固定资产账面价值是「原价 − 累计折旧」的结果，不是又一项资产，
    # 官方表给的是加粗白底。
    Line(_s("固定资产账面价值", formula="{c}20-{c}21"), Side(), line_left=20),
    Line(_s("在建工程", ("1603",)), Side(), line_left=21),
    Line(_s("工程物资", ("1605",)), Side(), line_left=22),
    Line(_s("固定资产清理", ("1606",)), Side(), line_left=23),
    # ↓ 从这里开始右半才有内容：所有者权益段配的是「生产性生物资产」(行次 24)。
    Line(_s("生产性生物资产", ("1621", "-1622")),
         _head("所有者权益（或股东权益）"), line_left=24),
    Line(_s("无形资产", ("1701", "-1702")), _s("实收资本（或股本）", ("4001",)),
         line_left=25, line_right=48),
    Line(_s("开发支出"), _s("资本公积", ("4002",)), line_left=26, line_right=49),
    Line(_s("长期待摊费用", ("1801",)), _s("盈余公积", ("4101",)), line_left=27, line_right=50),
    Line(_s("其他非流动资产"), _s("未分配利润", ("4103", "4104")), line_left=28, line_right=51),
    Line(_s("非流动资产合计", formula=B_NONCURRENT_ASSETS),
         _s("所有者权益（或股东权益）合计", formula="{c}27+{c}28+{c}29+{c}30"),
         line_left=29, line_right=52),
    # 官方表里「资产总计」是 2 空格（缩进一级），而右边同级的
    # 「负债和所有者权益（或股东权益）总计」是 0 空格 —— 两边不对称，照抄。
    Line(_gray("资产总计", "{c}16+{c}31", indent=1),
         _gray("负债和所有者权益（或股东权益）总计", "{c}19+{c}31", indent=0),
         line_left=30, line_right=53),
    # 平衡校验。会小企01表**没有这一行**（官方模板最后一格就是 53
    # 负债和所有者权益（或股东权益）总计），但它是这套系统里最有用的一行：
    # 资产 ≠ 负债+权益 时，这一格会立刻显示一个非零数。
    # 它排在 30 之后、不占任何行次，所以不破坏法定行次编号；默认**隐藏**
    # （hidden=True），打印稿与官方表逐格一致；要看得它在设计模式里取消隐藏。
    Line(_s("平衡校验（资产总计 − 负债和所有者权益（或股东权益）总计，应为 0）",
            formula="{c}32-{r}32", kind="CHECK"), Side(), hidden=True),
]

# --------------------------------------------------------------------- 利润表

INCOME_ROWS = [
    (1,  "一、营业收入", 0, False, ["6001", "6051"], "sum_amount"),
    (2,  "减：营业成本", 0, False, ["6401", "6402"], "sum_amount"),
    (3,  "    税金及附加", 0, False, ["6403"], "sum_amount"),
    (4,  "    销售费用", 0, False, ["6601"], "sum_amount"),
    (5,  "    管理费用", 0, False, ["6602"], "sum_amount"),
    (6,  "    财务费用", 0, False, ["6603"], "sum_amount"),
    (7,  "加：投资收益", 0, False, ["6111"], "sum_amount"),
    (8,  "二、营业利润", 0, True, [], "{c}1-{c}2-{c}3-{c}4-{c}5-{c}6+{c}7"),
    (9,  "加：营业外收入", 0, False, ["6301"], "sum_amount"),
    (10, "减：营业外支出", 0, False, ["6711"], "sum_amount"),
    (11, "三、利润总额", 0, True, [], "{c}8+{c}9-{c}10"),
    (12, "减：所得税费用", 0, False, ["6801"], "sum_amount"),
    (13, "四、净利润", 0, True, [], "{c}11-{c}12"),
]

# --------------------------------------------------------------------- 列定义
#
# 资产负债表 8 列：左（资产）4 列 + 右（负债和所有者权益）4 列。
# 两侧的数值列 kind 完全相同 —— 公式不认「哪一侧」，只认列号，
# 而列号已经把两侧彻底隔开了（C/D 与 G/H 之间隔着 STATIC 与 LINE_NO）。
BALANCE_COLS = [
    (1, "资  产", "STATIC", 210),   # 官方表两空格：居中后「资」「产」正好分居字身两侧
    (2, "行次", "LINE_NO", 46),
    (3, "期末余额", "BALANCE_CLOSING", 118),
    (4, "年初余额", "BALANCE_OPENING", 118),
    (5, "负债和所有者权益", "STATIC", 210),
    (6, "行次", "LINE_NO", 46),
    (7, "期末余额", "BALANCE_CLOSING", 118),
    (8, "年初余额", "BALANCE_OPENING", 118),
]

INCOME_COLS = [
    (1, "项目", "STATIC", 260),
    (2, "本期金额", "PERIOD_AMOUNT", 140),
    (3, "本年累计金额", "YTD_AMOUNT", 140),
]

TEMPLATES = [
    {
        "code": "BALANCE_SHEET",
        "name": "资产负债表",
        "type": "BALANCE_SHEET",
        "cols": BALANCE_COLS,
        "rows": BALANCE_ROWS,
    },
    {
        "code": "INCOME_SHEET",
        "name": "利润表",
        "type": "INCOME_SHEET",
        "cols": INCOME_COLS,
        "rows": INCOME_ROWS,
    },
]

# 列类型 -> (取数口径, 期间占位符)
# 占位符必须跟着列类型走：BALANCE_OPENING 是「年初余额」，要取账套启用月的期初，
# 而不是当前月的期初。早期版本这里对所有 kind 都写死 {{本期}}，
# 于是资产负债表的年初余额列显示成了 12 月的期初（11 月底余额），
# 跟上年期末根本对不上 —— 而这正是财务唯一会去核的那一列。
KIND_FOR = {
    "BALANCE_CLOSING": ("期末余额", "{{本期}}"),
    "BALANCE_OPENING": ("期初余额", "{{年初}}"),
    "PERIOD_AMOUNT": ("本期金额", "{{本期}}"),
    "YTD_AMOUNT": ("本年累计", "{{本期}}"),
    "PERIOD_DEBIT": ("本期借方", "{{本期}}"),
    "PERIOD_CREDIT": ("本期贷方", "{{本期}}"),
}

#: 不取数、也不写公式的列。STATIC 是项目标题列，LINE_NO 是行次列 ——
#: 两者的内容都存在行上（label / style），不来自科目。
#: 口径定义在 formula.py（那里是三处共用的词表，本模块从它导入）。
from app.reports.formula import NON_FORMULA_KINDS  # noqa: E402,F401  —— 重导出给播种逻辑用

#: ``Side.formula`` 的两种写法：
#:   * 这两个「模式名」——按 ``codes`` 里的科目编码逐项拼 ``科目(...)``；
#:   * 其它任何字符串都当成**显式公式**照抄（其中 ``{c}`` 按所在列展开成列字母）。
#: 分清楚是因为显式公式自带列字母（``C2+C3+…``），不依赖 codes ——
#: 早期版本把「没有科目编码」当成「这一格留空」，把整行合计公式清成了空，
#: 表现为资产负债表所有合计行全是 0。
CODE_SUM_MODES = ("sum", "sum_amount")


def _code_sum_formula(kind: str, codes: tuple[str, ...] | list[str]) -> str:
    """按科目编码拼一条 ``科目()+科目()…`` 的公式。

    科目编码前加 '-' 表示减项（累计折旧、存货跌价准备）：``科目(...)`` 配
    「期末余额」口径时已按科目自然方向取正数，所以这里的 '-' 就是从合计里减掉。
    """
    if kind not in KIND_FOR or not codes:
        return ""
    label, placeholder = KIND_FOR[kind]
    parts = []
    for c in codes:
        sign = "-" if c.startswith("-") else "+"
        parts.append(f'{sign}科目("{c.lstrip("-")}","{label}",{placeholder})')
    return parts[0].lstrip("+") + "".join(parts[1:])


def _split_columns(cols: list[ReportCol]) -> list[list[ReportCol]]:
    """按 STATIC 列把列切成「半」。

    每一半以自己的 STATIC（项目）列开头，到下一个 STATIC 列为止::

        资产 STATIC │ 行次 │ 期末 │ 年初 ┊ 负债和所有者权益 STATIC │ 行次 │ 期末 │ 年初
        <----------- 左半 ------------>  ┊ <------------ 右半 ------------>

    不这么切，``_side_formulas`` 就会给**每一半都摊上全部 8 列**，
    于是一行插进 16 个单元格、其中 8 个立刻撞 ``uq_report_cell``。
    切不出两半（单栏的利润表）时，整张表就是唯一的一半。
    """
    starts = [i for i, c in enumerate(cols) if c.kind == "STATIC"]
    if len(starts) < 2:
        return [list(cols)]
    return [
        cols[starts[n]: starts[n + 1] if n + 1 < len(starts) else len(cols)]
        for n in range(len(starts))
    ]


def _side_formulas(side: Side, cols: list[ReportCol],
                   peer: dict[str, str] | None = None) -> list[tuple[int, str]]:
    """把一侧的 Side 摊成 ``[(col_no, formula), ...]``。

    ``cols`` 必须是**这一侧自己的列**（见 :func:`_split_columns`），不是整张表。

    两个占位符：

    * ``{c}`` —— 本列的列字母。两侧共用一套模板，各自只在自己的行上生效。
    * ``{r}`` —— **另一半同一类列**的列字母（按 kind 配对：期末余额配期末余额、
      年初余额配年初余额）。只有跨栏比较用得上 —— 平衡校验那一行要比的是
      「资产总计」和「负债和所有者权益总计」，它们分居左右两栏，光有 ``{c}``
      只能拿左半的列字母，右半就永远比不到，年初余额那一列更会拿期末的数字
      去比。``peer`` 为空时 ``{r}`` 退回 ``{c}``（单栏模板没有另一半）。

    ``None`` 的行次、整行空掉的一侧，都会把这一行**另一半**也清空 —— 那半边
    本来就没有项目，留着上一行的公式才是错账。
    """
    from app.reports.formula import no_to_col

    peer = peer or {}
    out: list[tuple[int, str]] = []
    for col in cols:
        if col.kind in NON_FORMULA_KINDS or not side.label:
            formula = ""
        elif side.formula in CODE_SUM_MODES or not side.formula:
            formula = _code_sum_formula(col.kind, side.codes)
        else:
            letter = no_to_col(col.col_no)
            formula = side.formula.replace("{r}", peer.get(col.kind, letter))
            formula = formula.replace("{c}", letter)
        out.append((col.col_no, formula))
    return out


def install(db: Session, codes: tuple[str, ...] | None = None) -> None:
    """装入内置报表模板。

    ``codes`` 只装指定的几张模板（迁移里补装资产负债表时用，
    免得把用户在利润表上的改动一起冲掉）。模板已存在时是**先删后建**，
    所以重跑一次就是一次重新播种。
    """
    for spec in TEMPLATES:
        if codes is not None and spec["code"] not in codes:
            continue
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

        for col_no, label, kind, width in spec["cols"]:
            db.add(
                ReportCol(
                    template_id=tpl.id, col_no=col_no, label=label,
                    kind=kind, hidden=False, width=width,
                )
            )
        db.flush()

        cols = list(
            db.scalars(
                select(ReportCol).where(ReportCol.template_id == tpl.id).order_by(ReportCol.col_no)
            ).all()
        )

        if spec["type"] == "BALANCE_SHEET":
            _install_balance(db, tpl, cols, spec["rows"])
        else:
            _install_vertical(db, tpl, cols, spec["rows"])
        db.commit()


def _install_balance(db: Session, tpl: ReportTemplate,
                    cols: list[ReportCol], lines: list[Line]) -> None:
    """双栏（资产负债表）：一行同时落左半和右半。"""
    from app.reports.formula import no_to_col

    halves = _split_columns(cols)
    left_cols = halves[0]
    right_cols = halves[1] if len(halves) > 1 else []
    # 跨栏公式里的 {r}：另一半同一类数值列的列字母，按 kind 配对。
    peer_for_left = {c.kind: no_to_col(c.col_no) for c in right_cols
                     if c.kind not in NON_FORMULA_KINDS}
    peer_for_right = {c.kind: no_to_col(c.col_no) for c in left_cols
                      if c.kind not in NON_FORMULA_KINDS}

    cells: list[ReportCell] = []
    for row_no, line in enumerate(lines, start=1):
        style = {
            # 整行性质只作为两半的默认值；真正生效的是下面这两个 —— 同一行上
            # 「流动资产合计」（合计）与「递延收益」（普通项目）必须分别对待。
            "kind": line.kind,
            "kind_left": line.left.kind,
            "kind_right": line.right.kind,
            "line_left": line.line_left,
            "line_right": line.line_right,
            # 右栏标题不是表结构里的字段，只能随行一起存
            "label_right": line.right.label,
            "indent_right": line.right.indent,
            "bold_right": line.right.bold,
        }
        db.add(
            ReportRow(
                template_id=tpl.id, row_no=row_no, label=line.left.label,
                indent=line.left.indent, bold=line.left.bold,
                hidden=line.hidden, style=style,
            )
        )
        for side, side_cols, peer in ((line.left, left_cols, peer_for_left),
                                      (line.right, right_cols, peer_for_right)):
            for col_no, formula in _side_formulas(side, side_cols, peer):
                cells.append(ReportCell(
                    template_id=tpl.id, row_no=row_no, col_no=col_no, formula=formula,
                ))
    db.add_all(cells)
    db.flush()


def _install_vertical(db: Session, tpl: ReportTemplate, cols: list[ReportCol],
                      rows: list[tuple]) -> None:
    """单栏（利润表）：一行一个项目，右侧若干数值列。"""
    from app.reports.formula import no_to_col

    for row_no, label, indent, bold, codes, formula_tpl in rows:
        db.add(
            ReportRow(
                template_id=tpl.id, row_no=row_no, label=label,
                indent=indent, bold=bold, hidden=False, style={},
            )
        )
    db.flush()

    cells: list[ReportCell] = []
    for row_no, label, indent, bold, codes, formula_tpl in rows:
        for col in cols:
            if col.kind in NON_FORMULA_KINDS:
                formula = ""
            elif formula_tpl and formula_tpl not in CODE_SUM_MODES:
                formula = formula_tpl.replace("{c}", no_to_col(col.col_no))
            else:
                formula = _code_sum_formula(col.kind, codes)
            cells.append(ReportCell(
                template_id=tpl.id, row_no=row_no, col_no=col.col_no, formula=formula,
            ))
    db.add_all(cells)
    db.flush()