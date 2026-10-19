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
    from app.api.vouchers import _book_subjects, _serialize

    # 科目一次取好，逐张共用（审计 P3-12 的同一条理由，审计 2026-10-14 复发）。
    # 以前这里每张凭证都走 `_serialize` 的兜底分支，各查一次全账套科目：
    # 序列化一张凭证只显示科目编码与名称，而这个查询与凭证无关。
    # SQLAlchemy 的 identity map 只省对象重建、**不省 SQL 发射**，
    # N 张凭证就是 N 条一模一样的 SELECT —— 列表接口（vouchers.py:113）
    # 早就修好了，这里还留着。
    subs = _book_subjects(db, book.id)
    return [_serialize(db, v, subs) for v in vouchers]


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


#: 报表列 kind -> 该列的内容来自「行」而不是公式。
#:
#: STATIC 是项目标题列、LINE_NO 是行次列（见 ``seed/builtin_reports.py`` 的
#: ``NON_FORMULA_KINDS``）。这两列在 evaluate() 里恒为 ``value: None``，
#: 所以打印时必须按 kind 改从行上取数 —— 照抄 cell.value 只会印出一片空白。
REPORT_STATIC = "STATIC"
REPORT_LINE_NO = "LINE_NO"


def report_rows(payload: dict) -> list[dict]:
    """报表 -> 打印行。**一个报表列一个 key**（``c1``…``cN``）。

    为什么不能继续用「一个 label + value0..valueN」：资产负债表（会小企01表）
    是 8 列双栏表，一行同时承载左边的「资产」和右边的「负债和所有者权益」，
    外加各自的「行次」。一个 label 表达不了两个项目名，N 个 value 也表达不了
    行次 —— 以前那套形状只能印出左半边，右半边整片空白。
    摊成「key -> 值」的普通字典之后，通用表格渲染器不需要知道报表有双栏，
    利润表（3 列、1 个 STATIC）也走同一段代码，不需要开特例。

    各列取行上的什么，由列的 kind 与「这是第几个该 kind 的列」决定::

        STATIC  第 1 个 -> row.label / row.indent / row.bold（左半）
                第 2 个 -> style.label_right / indent_right / bold_right（右半）
        LINE_NO 第 1 个 -> style.line_left，第 2 个 -> style.line_right
                None 时留空（段标题与空行本来就不印行次）
        其它           -> cell.value（数值）

    ``label`` / ``indent`` / ``bold`` 继续原样保留（它们就是左半那一列的值），
    旧的消费方不必跟着改。

    隐藏行/列已由 :func:`visible_report_payload` 剔除，这里只做形状转换。
    """
    out = []
    cols = payload.get("cols", [])
    # 每一列属于左半还是右半，由**STATIC 列开启的分块**决定：STATIC 是每一半的
    # 首列，从它到下一个 STATIC 之间的所有列（含行次列与金额列）都属于同一半。
    #
    # 只给 STATIC/LINE_NO 编号是不够的 —— 金额列取不到 side，就会一律落到左半，
    # 于是「非流动资产合计」那一行右半的两个空金额格也印成合计（加粗）。
    # 单栏表只有一个 STATIC，全表都落在左半，同理不需要分支。
    side_of: dict[Any, int] = {}
    side = -1
    for c in cols:
        if c.get("kind") == REPORT_STATIC:
            side += 1
        side_of[c.get("col_no")] = max(side, 0)

    for row in payload.get("rows", []):
        style = row.get("style") or {}
        if not isinstance(style, dict):        # 老数据的 style 可能不是 dict
            style = {}
        item = {
            "label": row.get("label", ""),
            "indent": row.get("indent", 0),
            "bold": row.get("bold", False),
            "indent_right": style.get("indent_right", 0),
            "bold_right": bool(style.get("bold_right", False)),
            "hidden": False,
        }
        # 逐列的缩进/加粗。渲染器只认行级的 indent/bold，而双栏表两侧经常
        # 不同（「流动资产合计」左半加粗、右半却是个普通项目），必须分别指定。
        # 缺失时渲染器仍走行级值，LEDGER/DETAIL_LEDGER/BALANCE 一行都不用改。
        col_indent: dict[str, Any] = {}
        col_bold: dict[str, bool] = {}
        # 逐列的行性质。同理：kind 也不能只挂行上，否则第 16 行要么两半都不
        # 加粗（合计淹没在正文里），要么把「递延收益」也印成合计。
        col_kind: dict[str, str] = {}
        # 按可见列的 col_no 对齐，而不是照抄 cells 的下标：隐藏掉中间一列之后
        # 下标就和列对不上了，数字会整列错位。
        by_no = {c.get("col_no"): c for c in row.get("cells", [])}
        # 整行是空的半边不参与行性质：资产负债表右半到「负债和所有者权益总计」
        # 就结束了，底下几行右半全是空格子 —— 但它们所在行的左半是合计，
        # 不挡一下就会给右半也盖上合计的加粗与灰底。
        side_filled = {
            0: bool(row.get("label")) or style.get("line_left") is not None,
            1: bool(style.get("label_right")) or style.get("line_right") is not None,
        }
        for j, col in enumerate(cols):
            col_no = col.get("col_no")
            key = f"c{col_no}"
            side = side_of.get(col_no)
            kind_key = "kind_right" if side else "kind_left"
            if col.get("kind") == REPORT_STATIC:
                if side:                       # 右半：项目名随行存在 style 里
                    item[key] = style.get("label_right") or ""
                    col_indent[key] = style.get("indent_right", 0)
                    col_bold[key] = bool(style.get("bold_right", False))
                else:                          # 左半：就是 row 自己的三个字段
                    item[key] = row.get("label", "")
                    col_indent[key] = row.get("indent", 0)
                    col_bold[key] = bool(row.get("bold", False))
            elif col.get("kind") == REPORT_LINE_NO:
                no = style.get("line_right" if side else "line_left")
                item[key] = "" if no is None else str(no)
            else:
                cell = by_no.get(col_no) or {}
                item[key] = cell.get("value", "")
            # 单栏表（利润表）没有 kind_left/kind_right，回落到行级 kind。
            col_kind[key] = _print_kind(
                style.get(kind_key) if side_filled.get(side, True) else None,
                style.get("kind") if side_filled.get(side, True) else None,
            )
            item[f"_err{j}"] = (by_no.get(col_no) or {}).get("error", "")
        # 行性质：段标题 / 合计 / 平衡校验 —— 渲染器靠它决定加粗与底纹。
        item["__kind"] = _print_kind(style.get("kind"), "")
        item["__indent"] = col_indent
        item["__bold"] = col_bold
        item["__kind_by_col"] = col_kind
        out.append(item)
    return out


def _print_kind(side_kind: Any, fallback: Any) -> str:
    """行性质 -> 渲染器认的名字。

    ``side_kind`` 优先于 ``fallback``：分侧的值是新的，老数据（``style`` 里
    只有行级 ``kind``）走 fallback，所以这里认两种输入。
    """
    raw = str(side_kind or fallback or "")
    return {
        # SHADE = 官方表里那 7 个灰格（四个段标题 + 负债合计 / 资产总计 /
        # 负债和所有者权益（或股东权益）总计）。渲染上就是「压一层浅灰」，
        # **不加粗** —— 官方表通篇没有粗体，只有大标题是粗的。
        "SHADE": "SHADE",
        # HEAD（所有者权益（或股东权益））在官方模板里唯独没有底色，
        # 与普通项一样，不给任何类。
        "CHECK": "CHECK",
    }.get(raw, "")
