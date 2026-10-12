"""把账簿/凭证数据适配成打印模板能消费的「行」结构。

模板里的列 key 是稳定的语义名（date / subject / debit ...），
这里负责把各查询结果翻译成这些 key。以后新增账簿，只要在这里补一个
``to_rows_xxx``，模板引擎和前端设计器都不用改。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import Book, Period, Subject

_CATEGORY_CN = {"ASSET": "资产", "LIABILITY": "负债", "EQUITY": "所有者权益",
                "COST": "成本", "PROFIT": "损益"}


def context(db: Session, book: Book, period: Period | None = None, /, **extra: Any) -> dict:
    """页眉上下文。公司名/账套名/期间这些每个模板都要用。

    公司名直接取账套上的 company_name，不再回查公司表。

    ``period`` 刻意做成仅位置参数，这样调用方既可以
    ``context(db, book, p)``，也可以 ``context(db, book, p, period="2026-01 至 2026-12")``
    覆盖副标题而不撞名。
    """
    ctx = {
        "company": book.company_name,
        "book": book.name,
        "period": period.code if period is not None else "",
    }
    ctx.update({k: v for k, v in extra.items() if v not in (None, "")})
    return ctx


def _d(v: Any) -> Decimal:
    if v in (None, ""):
        return Decimal("0")
    return v if isinstance(v, Decimal) else Decimal(str(v))


# --------------------------------------------------------------------- 各来源


def voucher_rows(db: Session, book: Book, vouchers: list) -> list[dict]:
    from app.api.vouchers import _serialize

    return [_serialize(db, v) for v in vouchers]


def balance_rows(rows: list[dict]) -> list[dict]:
    """科目余额表 -> 打印行。带「上期末」「本期发生」两类分栏。"""
    out = []
    for r in rows:
        out.append(
            {
                "code": r["code"],
                "name": ("　" * (r["level"] - 1)) + r["name"],
                "level": r["level"],
                "opening_direction": r["opening_direction"],
                "opening": r["opening"],
                "opening_debit": r["opening"] if r["opening_direction"] == "借" else "",
                "opening_credit": r["opening"] if r["opening_direction"] == "贷" else "",
                "period_debit": r["period_debit"],
                "period_credit": r["period_credit"],
                "closing_direction": r["direction"],
                "closing": r["closing"],
                "closing_debit": r["closing"] if r["direction"] == "借" else "",
                "closing_credit": r["closing"] if r["direction"] == "贷" else "",
                "__kind": "SUBTOTAL" if not r["is_leaf"] else "",
            }
        )
    return out


def general_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append(
            {
                "code": r["code"],
                "name": ("　" * (r["level"] - 1)) + r["name"],
                "level": r["level"],
                "opening_direction": r["opening_direction"],
                "opening": r["opening"],
                "period_debit": r["period_debit"],
                "period_credit": r["period_credit"],
                "ytd_debit": r["ytd_debit"],
                "ytd_credit": r["ytd_credit"],
                "direction": r["direction"],
                "closing": r["closing"],
                "__kind": "SUBTOTAL" if not r["is_leaf"] else "",
            }
        )
    return out


def detail_rows(payload: dict) -> list[dict]:
    """明细账 -> 打印行。payload 来自 queries.detail_ledger。"""
    out: list[dict] = []
    for r in payload.get("rows", []):
        voucher_date = str(r.get("voucher_date") or "")
        period = str(r.get("period") or "")
        date_value = voucher_date or (period[-2:] if r.get("kind") in ("MONTH_TOTAL", "YTD_TOTAL") else "")
        base = {
            "date": voucher_date or date_value,
            "month": date_value[5:7] if len(date_value) >= 7 else date_value,
            "day": date_value[8:10] if len(date_value) >= 10 else "",
            "code": r.get("subject_code", ""),
            "name": ("　" * max(0, int(r.get("subject_level", 1)) - 1)) + r.get("subject_name", ""),
            "level": r.get("subject_level", ""),
            "voucher_no": r.get("voucher_no", ""),
            "summary": r.get("summary", ""),
            "debit": r.get("debit", ""),
            "credit": r.get("credit", ""),
            "direction": r.get("direction", ""),
            "balance": r.get("balance", ""),
        }
        if r.get("kind") in ("MONTH_TOTAL", "YTD_TOTAL"):
            base["__kind"] = "TOTAL" if r.get("kind") == "MONTH_TOTAL" else "SUBTOTAL"
            out.append(base)
            continue
        if r.get("kind") in ("OPENING", "TOTAL"):
            base.update({"date": "", "month": "", "day": "", "voucher_no": "",
                         "summary": r.get("voucher_no") or r.get("summary", ""),
                         "debit": "", "credit": "", "__kind": "TOTAL"})
            out.append(base)
            continue
        out.append(base)
    return out


def visible_report_payload(payload: dict) -> dict:
    """剔除隐藏行/列，并把每行的 cells 对齐到可见列上。

    报表模板的 hidden 是给设计者用的开关，打印和导出都该尊重它。这里统一在
    入口归一化，下游（HTML 打印 / Excel 导出）就不用各自处理一遍 ——
    两处各处理一遍的话，隐藏中间一列时单元格按坐标对齐就会整列错位。
    """
    cols = [c for c in payload.get("cols", []) if not c.get("hidden")]
    col_nos = [c.get("col_no") for c in cols]
    rows = []
    for row in payload.get("rows", []):
        if row.get("hidden"):
            continue
        by_no = {c.get("col_no"): c for c in row.get("cells", [])}
        rows.append({**row, "hidden": False,
                     "cells": [by_no.get(no) or {"col_no": no, "value": None,
                                                 "formula": "", "error": ""}
                               for no in col_nos]})
    return {**payload, "cols": cols, "rows": rows}


def report_rows(payload: dict) -> list[dict]:
    """报表 -> 打印行。列按 payload['cols'] 展开成 value0..valueN。

    隐藏行/列已由 :func:`visible_report_payload` 剔除，这里只做形状转换。
    """
    out = []
    cols = payload.get("cols", [])
    visible_col_nos = [c.get("col_no") for c in cols]
    for row in payload.get("rows", []):
        item = {
            "label": row.get("label", ""),
            "indent": row.get("indent", 0),
            "bold": row.get("bold", False),
            "hidden": False,
        }
        # 按可见列的 col_no 对齐，而不是照抄 cells 的下标：隐藏掉中间一列之后
        # 下标就和列对不上了，数字会整列错位。
        by_no = {c.get("col_no"): c for c in row.get("cells", [])}
        for j, col_no in enumerate(visible_col_nos):
            cell = by_no.get(col_no) or {}
            item[f"value{j}"] = cell.get("value", "")
            item[f"_err{j}"] = cell.get("error", "")
        out.append(item)
    return out
