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


# ---- 账套 ----
class BookIn(BaseModel):
    company_name: str
    name: str
    year: int
    start_month: int = Field(1, ge=1, le=12)
    company_id: Optional[int] = None


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
