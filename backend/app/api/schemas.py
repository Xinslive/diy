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
    year: int
    start_month: int = Field(1, ge=1, le=12)


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
    is_leaf: bool = True
    sort_no: int = 0


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
    source: Literal["MANUAL", "IMPORT", "CARRY_FORWARD"] = "MANUAL"


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


# ---- 期间 ----
class UncloseIn(BaseModel):
    revert_posting: bool = True


class YearEndIn(BaseModel):
    new_book_name: Optional[str] = None


# ---- 报表 ----
class CellIn(BaseModel):
    row_no: int
    col_no: int
    formula: str
