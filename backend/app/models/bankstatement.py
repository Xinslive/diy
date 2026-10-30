"""银行对账单台账、流水行、流水科目规则与格式画像。

**这个模块解决的是「银行说的」和「账上记的」之间那道缝。**

用户从网银导出的对账单上写着「9 月 3 日收到货款 12,800.00」，账上却只有
一张按发票生成的凭证。少了这一段，钱在账上就是**凭空少了一笔**，而科目余额
表永远只会显示一个差值，不告诉你是哪一笔、对不上。

所以这里存六样东西：

1. **对账单**（:class:`BankStatement`）—— 一份原始文件连同它的账户信息、
   期间与期末余额。原件必须留存：解析器会改，规则会变，解析错了要能重来。
2. **流水行**（:class:`BankStatementRow`）—— 对账单摊开的一条条流水。这是
   对账的最小单位：它知道自己有没有入账（``status``）、被哪张发票占用过
   （``matched_invoice_record_id``）、对方科目定下来了没有（``plan_status``）。
3. **流水科目规则**（:class:`BankStatementRule`）—— 摘要/用途/对方户名里的
   关键词 → 对方科目。**与发票科目规则分表**：两者的匹配对象、粒度与
   生命周期都不一样（发票规则按供应商/项目名匹配一行发票，这里按关键词
   匹配一条流水），塞进一张表会让两边都开始「半可用」。
4. **流水对方科目**（:class:`BankStatementRowSubject`）—— 一行流水的对方
   科目**可能不止一个**（一笔 1000 元的付款里 800 是房租、200 是押金）。
   所以对方科目是**子表**而不是行上的一个 id：金额要跟着科目走，
   合计必须等于流水金额。
5. **流水科目规则画像**（:class:`BankStatementProfile`）—— 记住某种对账单
   的列映射该怎么做。
6. **账套级流水配置**（:class:`BankStatementConfig`）—— 每个账套一份，
   装「流水凭证摘要模板」这类全局口径。

**为什么金额都是 Numeric(18, 2) 而不是 float。** 银行流水是钱的记录，
浮点在 0.1 + 0.2 这类地方会露出 0.30000000000000004；对账要拿金额**相等**
去判「是不是同一笔钱」，容差写成 0.00 就必须是精确相等。

**方向只有三个取值，含义与会计的借贷相反。** ``IN`` = 账户余额增加
（银行账上的「贷方发生额」）、``OUT`` = 余额减少（银行账上的「借方发生额」）、
``UNKNOWN`` = 认不出。这三个值在代码里到处用，因此在这里写死成常量，
免得每个调用点各自解释一遍。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

#: 流水方向。
#:
#: **与会计凭证的借贷是相反的**，这是本模块最容易搞错的一处：
#: 银行账上「贷方发生额」= 账户余额**增加**（收钱），而会计上银行存款增加
#: 记**借方**。所以映射恒为 ``借方 = 支出(OUT)``、``贷方 = 收入(IN)``。
#:
#: 写成常量而不是字符串字面量散落在各处：错了要改的地方只有一处。
DIRECTION_IN = "IN"
DIRECTION_OUT = "OUT"
DIRECTION_UNKNOWN = "UNKNOWN"

#: 流水行的处理状态。
STATUS_OPEN = "OPEN"          # 还没入账
STATUS_POSTED = "POSTED"      # 已经生成过凭证
STATUS_IGNORED = "IGNORED"    # 用户点名不算（误解析、手工剔除）
#: **待复核**：账上有同一笔（金额相等、方向对应），但**日期对不上** ——
#: 差得比主窗口大、比复核窗口小。
#:
#: **为什么要有这一档**（2026-10-29 用户报的问题）：银行流水记的是**实际
#: 收付日期**，会计凭证记的常常是**发票日期**，两者差十天是常事。
#: 按一档切的话，那笔钱在流水页上「没入账」（于是「生成流水凭证」又给它
#: 记一遍，**同一笔钱记两次**），在存疑页上却又「对得上」（窗口更宽）
#: —— **两页都不报，它从系统的视野里整个消失了**。
#:
#: 这一档就是给它一个落脚的地方：**报出来，让人判**。
#: 判「是同一笔」→ 挂上那张凭证（转 ``POSTED``）；
#: 判「不是同一笔」→ 放行（回 ``OPEN``，并记下否掉的是哪张凭证）。
#:
#: **它不是「已入账」**：钱到底记没记上还没定，用户还没判。
#: **它也不是「没入账」**：账上明明有一笔，只差日期。
STATUS_REVIEW = "REVIEW"

#: 对方科目的确定方式。
PLAN_NONE = "NONE"        # 还没算过
PLAN_RULE = "RULE"        # 命中了流水科目规则
PLAN_MANUAL = "MANUAL"    # 人工指定
PLAN_PENDING = "PENDING"  # 规则没命中，等人工指定

#: 解析状态。
PARSE_OK = "OK"
PARSE_FAILED = "FAILED"
PARSE_PARTIAL = "PARTIAL"


class BankStatement(Base, TimestampMixin):
    """一份对账单（原始文件 + 它的账户信息 + 解析结论）。"""

    __tablename__ = "bank_statement"
    __table_args__ = (
        Index("ix_bank_statement_book_id", "book_id", "id"),
        # **重复上传的守卫**：同一个账户、同一个起止期间只能有一份对账单。
        #
        # 为什么必须有：同一份银行流水传两遍，就会生成两遍凭证 —— 钱在账上
        # 多记一次，而用户在界面上看着「我明明只传了一次」。这不是概率问题，
        # 是「再点一次上传按钮」就会发生的事。
        UniqueConstraint(
            "book_id", "account_subject_id", "period_start", "period_end",
            name="uq_bank_statement_book_subject_period",
        ),
        Index("ix_bank_statement_book_subject", "book_id", "account_subject_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # 绑定的现金科目（1002 银行存款的某个明细）。**末级**。
    account_subject_id: Mapped[int] = mapped_column(
        ForeignKey("subject.id", ondelete="RESTRICT"), nullable=False)
    # 科目编码另存一份：科目表每个年度各有一份，id 跨年不是一回事，
    # 而「这份对账单是哪个账户的」这件事要跨年度看得懂。
    account_subject_code: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    filename: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # XLSX / XLS / HTML / CSV / PDF —— **认的是文件头，不是扩展名**。
    container_format: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    bank_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    account_no: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    account_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[Optional[date]] = mapped_column(Date)
    opening_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    closing_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    income_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    expense_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # OK / FAILED / PARTIAL。PARTIAL = 解析出来了但诊断里报了疑点。
    parse_status: Mapped[str] = mapped_column(String(16), default=PARSE_OK, nullable=False)
    parse_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 解析诊断：容器、表头行、列映射、自检结果、网格预览。
    diagnosis: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    rows: Mapped[list["BankStatementRow"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan", order_by="BankStatementRow.seq"
    )


class BankStatementRow(Base, TimestampMixin):
    """对账单里的一条流水。"""

    __tablename__ = "bank_statement_row"
    __table_args__ = (
        # 发票匹配的查询走这条索引：同账套 + 同方向 + 同金额 + 日期区间。
        Index("ix_bank_statement_row_match", "book_id", "direction", "amount", "tx_date"),
        # 制证候选走这条：同账套 + 状态。
        Index("ix_bank_statement_row_status", "book_id", "status"),
        Index("ix_bank_statement_row_statement", "statement_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("bank_statement.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tx_date: Mapped[Optional[date]] = mapped_column(Date)
    tx_time: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    direction: Mapped[str] = mapped_column(String(8), default=DIRECTION_UNKNOWN, nullable=False)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    purpose: Mapped[str] = mapped_column(Text, default="", nullable=False)
    counterparty_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    counterparty_account: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: 表头之外的列（交易流水号、凭证号、对方开户行……）原样留着。
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_OPEN, nullable=False)
    matched_voucher_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("voucher.id", ondelete="SET NULL"))
    #: **用户判过「不是同一笔」的那几张凭证**（空 = 没判过）。
    #:
    #: **为什么要有这一列**：待复核那一档是「金额对得上、日期对不上」。
    #: 用户判「不是同一笔」之后，这一行就该放行去正常制证 —— 但下次
    #: 重算时，那张同额的分录**又会被捞出来**，用户会以为没生效
    #: （判了等于没判）。记下否掉的是哪几张，重算时跳过它们。
    #:
    #: **是一组，不是一张**（迁移 ``0049`` 改的）：记判断这件事**必须累加**。
    #: 生产上（2026-10-30 用户报的死循环）同一份对账单上有两行同额流水、
    #: 账上有四张同额凭证，用户在两行上轮流否 —— **第二次否的时候把第一次
    #: 记的那张覆盖掉了**，于是第一次点过的那张凭证又冒出来，用户永远点不完。
    #: 后来的判断不许抹掉先前的。
    #:
    #: **记的是凭证而不是布尔**：否掉这一张，不代表以后冒出来的另一张
    #: 同额凭证也不是它 —— 那一张仍然该报出来让用户判。
    #:
    #: **存的是 id 而不是外键**：这张集合要跨行取用（见
    #: :func:`app.bankstatement.service._pair_rows`）—— **一份对账单里
    #: 任意一行否掉的那张凭证，这一份对账单的其它行也不再拿它来问**
    #: （否则它会从旁边那一行冒出来，用户看着像死循环）。外键做不到跨行
    #: 取用，所以这里存裸 id；凭证被删了，那个 id 指向一张不存在的凭证，
    #: 配对时自然配不上 —— 删了凭证本来就要重新判一次。
    rejected_voucher_ids: Mapped[list] = mapped_column(JSONB, default=list,
                                                       nullable=False)
    #: **这一行待复核时对着的那张凭证**（``None`` = 没指认过）。
    #:
    #: **为什么要有这一列**（迁移 ``0049`` 加的）：配对是**一对一**的贪心
    #: 分配，日期差最小的先配对。于是旁边那一行被否掉之后，重新分配时那张
    #: 凭证会落到这一行头上 —— 用户看着就是「我刚判过的那张凭证又跑到另一行
    #: 去了」。记住这一行现在对着哪张凭证，重算时优先兑现它：**一张凭证一旦
    #: 配给某行，没被否掉就不换人。**
    #:
    #: **外键是 SET NULL**：那张凭证被删了，这一行就回到「没指认过」，
    #: 重算时照常重新配一张候选 —— 删了凭证本来就要重新判一次。
    review_voucher_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("voucher.id", ondelete="SET NULL"))
    matched_invoice_record_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("invoice_record.id", ondelete="SET NULL"))
    #: 对方科目定下来了没有：NONE 还没算 / RULE 规则命中 / MANUAL 人工指定 /
    #: PENDING 规则没命中，等人工指定。
    #:
    #: **它是「这一行有没有对方科目」的摘要，不是对方科目本身。**
    #: 对方科目本体在 :class:`BankStatementRowSubject` 里（可能好几个）。
    plan_status: Mapped[str] = mapped_column(String(16), default=PLAN_NONE, nullable=False)
    #: 这一行的问题（方向未知、金额为 0、两侧都有发生额……）。
    problems: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    statement: Mapped["BankStatement"] = relationship(back_populates="rows")
    #: 对方科目（可能不止一个，见 :class:`BankStatementRowSubject`）。
    plan_subjects: Mapped[list["BankStatementRowSubject"]] = relationship(
        back_populates="row", cascade="all, delete-orphan",
        order_by="BankStatementRowSubject.sort_no")


class BankStatementRowSubject(Base, TimestampMixin):
    """一行流水的**对方科目** —— 可能不止一个。

    **为什么是子表而不是行上的一个 id。** 一笔流水可能对应好几个科目：
    一笔 1000 元的付款里 800 是房租、200 是押金；一笔收款里既有货款又有
    运费。对方科目与**金额**是一一对应的，所以金额必须跟着科目走 ——
    塞进行上的单值列里，就只能记「一个科目 + 一笔总额」，而那一笔总额
    到底记在哪个科目上就成了猜的。

    **金额的硬约束：这一行所有科目的金额合计必须等于流水金额。**
    合计对不上时凭证会不平衡（或者更糟：平了但记在错误的科目上）。
    这条校验在服务层做（:func:`app.bankstatement.service.update_row`），
    表层用 CHECK 守住「金额不能是 0 或负数」。

    **外键是 RESTRICT**：被流水计划引用的科目不能被删 —— 与旧的
    ``plan_subject_id`` 同一条语义，替掉它不能把这道闸弄丢。
    """

    __tablename__ = "bank_statement_row_subject"
    __table_args__ = (
        Index("ix_bank_statement_row_subject_row", "row_id", "sort_no"),
        # **同一个科目可以在同一行里出现多次**（2026-10-29 用户批注）。
        #
        # 原来这里有 ``UNIQUE (row_id, subject_id)``，并且应用层也报
        # 「在这一行里出现了两次 —— 要记两笔请把金额合并到一条上」。
        # **那个限制不该有**，理由有两条：
        #
        # ① **摘要是挂在这一层上的**（迁移 0043/0044 把它从行级下沉到
        #    对方科目这一层）。所以「同一个科目拆成两条、各写各的摘要」
        #    本来就是这套设计的用法 —— 把它们禁掉，那次下沉就白做了；
        # ② 账上两条分录指向同一个科目，**本来就是合法的**（金额各算
        #    各的，摘要各写各的）。禁掉它只是让用户「先把两条合并、
        #    再把其中一条的摘要丢掉」。
        #
        # **金额合计的硬约束还在**（下面那个 CHECK，以及服务层
        # ``_parse_plan_payload`` 的合计校验）—— 那个才是真的护栏。
        CheckConstraint("amount > 0", name="ck_bank_statement_row_subject_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    row_id: Mapped[int] = mapped_column(
        ForeignKey("bank_statement_row.id", ondelete="CASCADE"), nullable=False)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subject.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: RULE（规则命中）/ MANUAL（人工指定）。
    source: Mapped[str] = mapped_column(String(16), default=PLAN_MANUAL, nullable=False)
    sort_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: **人工指定的这一条对方科目的凭证摘要**。留空 = 跟着规则/模板走
    #: （见 ``app.bankstatement.service.render_summary``）。
    #:
    #: **为什么挂在对方科目上，而不是挂在流水行上。** 一张凭证有 N+1 条
    #: 分录（现金那一条 + 对方那 N 条），每条分录的摘要**本来就是各自独立
    #: 的字段**（``entry.summary``，凭证编辑页里逐条可改）。流水这一侧若只有
    #: 一个「整张凭证的摘要」，就管不了「这笔钱记到哪个科目上、那个科目该
    #: 写什么话」—— 一笔 5000 元的付款拆成「房租 800 + 押金 200」，两个
    #: 科目该写的话本来就不一样。摘要的粒度跟着**分录**走，就跟着对方科目走。
    #:
    #: **它不与规则抢真相**：留空时制证仍然按模板算；只有用户**明确改过**
    #: 才用这一列。两者的优先级由服务层那一行代码决定，不在别处另判。
    summary_override: Mapped[str] = mapped_column(Text, default="", nullable=False)

    row: Mapped["BankStatementRow"] = relationship(back_populates="plan_subjects")


class BankStatementRule(Base, TimestampMixin):
    """流水 → 对方科目 的关键词规则。

    **与发票科目规则分开两张表。** 发票规则匹配的是「供应商名 / 项目名」，
    粒度是**一张发票的一行**；这里的规则匹配的是「摘要 / 用途 / 对方户名」，
    粒度是**一条流水**。两者的关键词空间、冲突处理（这里优先级 + 关键词
    长度）、以及失效时机都不一样 —— 混在一张表里，最后一定会长出
    「这个字段在发票规则里是什么意思」这种没法回答的问题。
    """

    __tablename__ = "bank_statement_rule"
    __table_args__ = (
        Index("ix_bank_statement_rule_book_active", "book_id", "active", "priority"),
        UniqueConstraint("book_id", "match_field", "keyword", name="uq_bank_statement_rule_keyword"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # SUMMARY / PURPOSE / CP_NAME —— 拿哪一列去匹配关键词。
    match_field: Mapped[str] = mapped_column(String(16), default="SUMMARY", nullable=False)
    keyword: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subject.id", ondelete="RESTRICT"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 这条规则命中时，生成的凭证用哪个摘要模板。**空 = 用账套级默认**
    #: （:class:`BankStatementConfig`）。见 :func:`app.bankstatement.service.render_summary`。
    summary_template: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))


class BankStatementConfig(Base, TimestampMixin):
    """每个账套一份的流水对账配置。

    **现在只有一个已经下线的字段**（账套级凭证摘要模板，2026-10-28 下线）。
    摘要口径改成了「规则级模板 > 内置口径」，不再有账套级这一层。

    **表留着、列留着**，是因为删列要动迁移链，而这件事本身没有收益：
    服务层已经不再读它（``_summary_template_for`` 里没有这条分支），
    写它也会被 :func:`app.bankstatement.service.save_config` 清空 ——
    一个「留在库里但谁也不认」的字段，比「库里根本没有」更让人误解。

    留着它还有一个好处：**老数据不丢**。万一将来又要恢复账套级模板，
    那些值还在库里，不用让用户重新填一遍。
    """

    __tablename__ = "bank_statement_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=False, unique=True)
    #: 流水生成凭证时用的摘要模板。**已废弃（2026-10-28 下线）** ——
    #: 空 = 内置口径（用途优先、摘要兜底、再兜底「银行收/付 + 对方户名」）。
    #:
    #: 占位符：``{摘要}`` ``{用途}`` ``{对方户名}`` ``{日期}`` ``{金额}``
    #: ``{方向}`` ``{账户}``。模板写错时**退回内置口径**，不让整批制证失败。
    summary_template: Mapped[str] = mapped_column(String(256), default="", nullable=False)


class BankStatementProfile(Base, TimestampMixin):
    """格式画像：记住某个「看起来一样的对账单」该怎么解析。

    指纹 = 容器类型 + 归一化后的表头签名（见 ``semantics.fingerprint_of``）。
    自动映射认不出来时，用户在页面上手工指定一次各列是什么，这张表就把
    这次的映射存下来 —— 下次同一家银行、同一种导出格式的文件直接套用。

    **按账套隔离**：A 账套的手工映射不该套到 B 账套上。同一张表里既有
    指纹又有账套 id，查询时两个条件一起用。
    """

    __tablename__ = "bank_statement_profile"
    __table_args__ = (
        UniqueConstraint("book_id", "fingerprint", name="uq_bank_statement_profile_fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 列名 → IR 字段名（"DATE" / "IN" / …）的映射。
    column_mapping: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: 被套用过的次数。用来认出「这个画像已经过时了」——
    #: 银行改了导出格式时，旧画像会一直套用并一直解析错，
    #: 命中次数高 + 诊断里报错，正是该重新指定列的信号。
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
