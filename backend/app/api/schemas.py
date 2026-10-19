from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field

Direction = Literal["DEBIT", "CREDIT"]


# ---- 认证 ----
class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int
    user: dict


class RegisterIn(BaseModel):
    # 自助注册只允许字母数字下划线，避免带空格/中文的口令在日志和口令提示里混淆
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    # 比管理员建号更严：管理员建号 6 位，自助注册 8 位
    password: str = Field(min_length=8, max_length=128)
    real_name: str = Field(default="", max_length=64)


# ---- 账套 ----
class BookIn(BaseModel):
    # 公司名只是账套的一个文本属性，没有独立的「公司」实体
    company_name: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    # 账套的起始会计年度。一个账套跨多个年度，这里填「从哪一年开始记」
    start_year: int
    start_month: int = Field(1, ge=1, le=12)
    # 科目编码规则，如「4-2-2-2」；决定新增科目自动接几位号
    subject_code_rule: str = Field("4-2-2-2", pattern=r"^\d+(-\d+)*$")


class BookEditIn(BaseModel):
    """改账套的属性：只允许动名字和科目编码规则，不动账上的任何数字。"""

    company_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    # 改了规则只影响**今后**新增科目的自动接号，已有科目的编码一律不动
    subject_code_rule: Optional[str] = Field(default=None, pattern=r"^\d+(-\d+)*$")

    def changes(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class BookOrderIn(BaseModel):
    """账套磁贴的**个人**排序：按用户想要的先后给一份账套 id。

    传的是**相对顺序**，不是「第 2 本写在第 1 位」这样的绝对序号 ——
    拖动只改变谁在谁前面，写死序号的话每次拖动都要重算全部，
    而且中间新建的账套会把后面的序号全顶掉。
    """

    # 上限不是业务规则，是防御：真有人一次几千个账套时，
    # 那不是「排个序」而是别的东西，不该由这个接口默默接下
    book_ids: list[int] = Field(min_length=0, max_length=500)


class OpeningIn(BaseModel):
    subject_id: int
    amount: Decimal = Decimal("0")


# ---- 科目 ----
class SubjectIn(BaseModel):
    code: str
    name: str
    category: Literal["ASSET", "LIABILITY", "EQUITY", "COST", "PROFIT"]
    direction: Direction = "DEBIT"
    parent_id: Optional[int] = None
    carry_forward: bool = False
    # **不接受客户端控制（审计 P1-01）。**
    #
    # 「是不是末级」完全由「有没有下级」决定。凭证校验和期初写入都拿
    # ``Subject.is_leaf`` 当权威判据，一旦它能被 PATCH 改写，就等于普通写入者
    # 可以把汇总科目标成末级、进而往汇总级上挂分录。
    #
    # 字段保留是因为前端已经在提交它（新增表单固定传 true），
    # 删掉会让存量前端直接 422。服务端读都不读它。
    is_leaf: bool = True
    sort_no: int = 0
    qty_calc: bool = False
    aux_calc: bool = False
    fx_calc: bool = False
    # 只有「改」才用得上：停用科目。留空表示不动。
    # 新增一律启用，不走这个字段 —— 刚建的科目没人会要它是停用的。
    active: Optional[bool] = None


class SubjectEditIn(BaseModel):
    """科目更新使用部分字段，避免仅改名称/编码时覆盖其他属性。"""
    code: Optional[str] = Field(default=None, min_length=1, max_length=32)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    category: Optional[Literal["ASSET", "LIABILITY", "EQUITY", "COST", "PROFIT"]] = None
    direction: Optional[Direction] = None
    parent_id: Optional[int] = None
    carry_forward: Optional[bool] = None
    is_leaf: Optional[bool] = None
    sort_no: Optional[int] = None
    qty_calc: Optional[bool] = None
    aux_calc: Optional[bool] = None
    fx_calc: Optional[bool] = None
    active: Optional[bool] = None


# ---- 凭证 ----
class EntryIn(BaseModel):
    subject_id: int
    direction: Direction
    amount: Decimal
    summary: str = ""
    aux_ref: dict = Field(default_factory=dict)


class VoucherIn(BaseModel):
    voucher_date: date
    entries: list[EntryIn]
    remark: str = ""
    attachments: int = 0
    voucher_no: Optional[str] = None
    # **客户端不能指定 CARRY_FORWARD（审计 P1-02）。**
    #
    # 它原来和 MANUAL/IMPORT/INVOICE 一起写在 Literal 里，于是任何有账套写权限
    # 的人都能直接调接口把一张自己的凭证标成「系统结转」。这个标记不是装饰：
    #
    #   * ``create_voucher`` 过去按 ``source in SYSTEM_SOURCES`` 放开「停用科目
    #     也能记账」这道闸（系统结转搬的是**已经存在**的余额，不该被拦）；
    #   * ``periods._drop_carry_forward`` 删本期全部 CARRY_FORWARD 凭证时**不看
    #     状态**，而正常删除路径（``vouchers.delete_voucher``）是拒绝删已记账凭证的。
    #
    # 两条叠起来就是：自己造一张 CARRY_FORWARD、记账、再点一次「重新结转损益」，
    # 这张**已记账的**凭证被悄悄删掉，库里还不留任何提示（凭证号可复用）。
    #
    # 所以它从客户端可选值里拿掉，成为服务端内部值（``create_voucher`` 的
    # ``internal=True`` 是唯一的正规入口）。前端只用它显示标签
    # （``VoucherOrganize.vue`` 的 sourceLabel），从不提交这个值，不受影响。
    source: Literal["MANUAL", "IMPORT", "INVOICE"] = "MANUAL"
    # 「插入凭证」告诉后端插在谁的后面：新凭证拿这个号，后面的号整体顺位。
    # 留空就是普通的新增凭证（取期间内最大号 +1）。
    insert_after: Optional[int] = None
    # 插在参照凭证的前面还是后面（用户 2026-10-19 定的口径）：
    #   before —— 「插入凭证」= 向前插入，占参照自己的号，参照及以后顺位；
    #   after  —— 「增加凭证」= 向后插入，拿参照号 +1，只有更大的号顺位。
    # 只在 ``insert_after`` 有值时才有意义。
    insert_position: Literal["before", "after"] = "before"


class VoucherOut(BaseModel):
    id: int
    voucher_no: str
    voucher_date: date
    status: str
    source: str
    remark: str
    attachments: int
    created_by: Optional[str]
    posted_by: Optional[str]
    posted_at: Optional[str]
    total_debit: Decimal
    total_credit: Decimal
    entries: list[dict]


class BatchIds(BaseModel):
    ids: list[int]


# ---- 发票导入 ----
class InvoiceConfigIn(BaseModel):
    """发票自动制证的账套级配置。

    没有「进项税额」科目：小微企业不做进项抵扣，进项发票的税额直接计入成本
    费用（并入第一条明细行），不需要单独一个应交税费明细科目。
    """

    taxpayer_id: str = Field(default="", max_length=32)
    output_tax_subject_id: Optional[int] = None
    input_credit_subject_id: Optional[int] = None
    output_debit_subject_id: Optional[int] = None


class InvoiceRuleIn(BaseModel):
    match_type: Literal["SELLER", "ITEM"]
    invoice_type: Literal["INPUT", "OUTPUT", "BOTH"] = "BOTH"
    keyword: str = Field(min_length=1, max_length=128)
    subject_id: int
    priority: int = Field(default=0, ge=-1000, le=1000)
    active: bool = True


class InvoiceRulePatch(BaseModel):
    match_type: Optional[Literal["SELLER", "ITEM"]] = None
    invoice_type: Optional[Literal["INPUT", "OUTPUT", "BOTH"]] = None
    keyword: Optional[str] = Field(default=None, min_length=1, max_length=128)
    subject_id: Optional[int] = None
    priority: Optional[int] = Field(default=None, ge=-1000, le=1000)
    active: Optional[bool] = None


class InvoicePreviewIn(BaseModel):
    """批次预览的人工覆盖项。

    动态字典保持前端可逐张修正，不把发票明细硬编码成固定列数。
    key 为 InvoiceRecord.id，value 允许包含 invoice_type、line_subjects、
    tax_subject_id、settlement_subject_id。
    """

    overrides: dict[str, dict] = Field(default_factory=dict)


# ---- 期间 ----
class CarryForwardIn(BaseModel):
    """结转损益的入参。

    结转永远是「有旧凭证就删掉重算」，没有第二种模式。

    ``replace`` 保留只是为了兼容旧前端调用（它还在发这个字段），**完全被忽略** ——
    传 false 拿到的仍是「旧凭证已删除并重算」的结果。P2-3 修掉了服务层那个
    从不生效的同名形参（那才是真正在骗人的地方：它对外承诺了一个这个函数
    从不兑现的开关）；这里留着字段只是为了让旧调用不报 422。
    """
    replace: bool = True


class CloseIn(BaseModel):
    """结账的入参。

    整个 body 可选（``Optional[CloseIn]``）是为了兼容还没升级的前端：
    不传 = 不带版本 = 不比对，与乐观锁上线前的行为完全一致。
    """
    # 乐观锁的期望版本（审计 P1-19）。值来自 ``GET /periods`` 返回的
    # ``version``；留空表示「老调用方，不比对」。
    version: Optional[int] = None
    # **没有 allow_empty**：空月份一律不许结账，不留「我知道、我确认」的旁路。
    # 一张凭证都没有的月份能穿过整理体检、自动记账和损益结平三道闸门，
    # 「这个月忘录了账」于是被静默封进历史，而补录就得反结账、反结账会连带把
    # 后面已结的月份全部退回。确认弹窗挡不住它 —— 人会点「确定」。


class UncloseIn(BaseModel):
    revert_posting: bool = True
    # 与 CloseIn 同一条约定，见那里的注释。反结账尤其需要：
    # 它会把区间内的凭证批量退回未记账，陈旧屏幕上再点一次等于抹掉别人的操作。
    version: Optional[int] = None


class YearEndIn(BaseModel):
    # 年度结转不再新建账套，只能指定「紧接的下一个年度」
    new_year: Optional[int] = None
    # 页面上正在看的那一年：结转就从这一年起算，不然后端只能靠猜
    from_year: Optional[int] = None


# ---- 报表 ----
class CellIn(BaseModel):
    row_no: int
    col_no: int
    formula: str
