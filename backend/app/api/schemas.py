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
    source: Literal["MANUAL", "IMPORT", "INVOICE", "CARRY_FORWARD"] = "MANUAL"
    # 「插入凭证」告诉后端插在谁的后面：新凭证拿这个号，后面的号整体顺位。
    # 留空就是普通的新增凭证（取期间内最大号 +1）。
    insert_after: Optional[int] = None


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

    结转永远是「有旧凭证就删掉重算」，这个字段保留只是为了兼容旧前端调用，
    不再影响行为。
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
