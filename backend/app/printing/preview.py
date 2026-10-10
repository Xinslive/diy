"""把账簿/凭证数据适配成打印模板能消费的「行」结构。

模板里的列 key 是稳定的语义名（date / subject / debit ...），
这里负责把各查询结果翻译成这些 key。以后新增账簿，只要在这里补一个
``to_rows_xxx``，模板引擎和前端设计器都不用改。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import Book, Period, Subject, SysCompany

_CATEGORY_CN = {"ASSET": "资产", "LIABILITY": "负债", "EQUITY": "所有者权益",
                "COST": "成本", "PROFIT": "损益"}


def context(db: Session, book: Book, period: Period | None = None, /, **extra: Any) -> dict:
    """页眉上下文。公司名/账套名/期间这些每个模板都要用。

    ``period`` 刻意做成仅位置参数，这样调用方既可以
    ``context(db, book, p)``，也可以 ``context(db, book, p, period="2026-01 至 2026-12")``
    覆盖副标题而不撞名。
    """
    company = db.get(SysCompany, book.company_id)
    ctx = {
        "company": company.name if company else "",
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
    meta = payload.get("subject") or {}
    for r in payload.get("rows", []):
        if r.get("kind") in ("OPENING", "TOTAL"):
            out.append(
                {
                    "date": "",
                    "voucher_no": "",
                    "summary": r.get("voucher_no") or r.get("summary", ""),
                    "debit": "",
                    "credit": "",
                    "direction": r.get("direction", ""),
                    "balance": r.get("balance", ""),
                    "__kind": "TOTAL",
                }
            )
            continue
        out.append(
            {
                "date": str(r.get("voucher_date") or ""),
                "voucher_no": r.get("voucher_no", ""),
                "summary": r.get("summary", ""),
                "debit": r.get("debit", ""),
                "credit": r.get("credit", ""),
                "direction": r.get("direction", ""),
                "balance": r.get("balance", ""),
                "__kind": r.get("kind", "") if r.get("kind") in ("TOTAL", "SUBTOTAL") else "",
            }
        )
    return out


def report_rows(payload: dict) -> list[dict]:
    """报表 -> 打印行。列按 payload['cols'] 展开成 value0..valueN。"""
    out = []
    cols = payload.get("cols", [])
    for i, row in enumerate(payload.get("rows", [])):
        item = {
            "label": row.get("label", ""),
            "indent": row.get("indent", 0),
            "bold": row.get("bold", False),
            "hidden": row.get("hidden", False),
        }
        for j, cell in enumerate(row.get("cells", [])):
            item[f"value{j}"] = cell.get("value", "")
            item[f"_err{j}"] = cell.get("error", "")
        out.append(item)
    return out
