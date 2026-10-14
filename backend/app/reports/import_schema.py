"""模板导入的有界输入结构。"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_no: int = Field(ge=1, le=500)
    label: str = Field(max_length=128)
    indent: int = Field(default=0, ge=0, le=4)
    bold: bool = False
    hidden: bool = False
    style: dict[str, Any] = Field(default_factory=dict)

    @field_validator("style")
    @classmethod
    def allowed_style(cls, value):
        allowed = {"line_left", "line_right", "kind", "kind_left", "kind_right", "label_right", "indent_right", "bold_right", "source_row_no"}
        if set(value) - allowed:
            raise ValueError("模板包含不支持的行样式")
        if len(str(value)) > 1000:
            raise ValueError("行样式过长")
        if len(str(value.get("label_right", ""))) > 128:
            raise ValueError("右侧项目名称最多 128 字")
        return value


class ImportCol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    col_no: int = Field(ge=1, le=32)
    label: str = Field(max_length=64)
    kind: Literal["STATIC", "LINE_NO", "BALANCE_CLOSING", "BALANCE_OPENING", "PERIOD_AMOUNT", "YTD_AMOUNT", "FORMULA"]
    width: int = Field(default=140, ge=46, le=600)
    hidden: bool = False


class ImportCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_no: int = Field(ge=1, le=500)
    col_no: int = Field(ge=1, le=32)
    formula: str = Field(max_length=4000)


class ImportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(max_length=255)
    format: str = Field(max_length=80)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{24}$")


class ImportIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_no: int = Field(ge=1, le=500)
    col_no: int = Field(ge=1, le=32)
    source_address: str = Field(max_length=20)
    source_formula: str = Field(max_length=4000)
    reason: str = Field(max_length=500)
    label: str = Field(max_length=128)


class ImportTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    type: Literal["BALANCE_SHEET", "INCOME_SHEET"]
    source: ImportSource
    rows: list[ImportRow] = Field(min_length=1, max_length=500)
    cols: list[ImportCol] = Field(min_length=1, max_length=32)
    cells: list[ImportCell] = Field(max_length=16000)
    issues: list[ImportIssue] = Field(default_factory=list, max_length=16000)
    subject_mapping: dict[str, str] = Field(default_factory=dict, max_length=500)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("模板名称不能为空")
        return value.strip()


class ImportTemplates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    templates: list[ImportTemplate] = Field(min_length=1, max_length=10)
    accept_warnings: bool = False
