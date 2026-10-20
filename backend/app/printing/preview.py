"""把账簿/凭证数据适配成打印模板能消费的「行」结构。

模板里的列 key 是稳定的语义名（date / subject / debit ...），
这里负责把各查询结果翻译成这些 key。以后新增账簿，只要在这里补一个
``to_rows_xxx``，模板引擎和前端设计器都不用改。

**科目余额表的合计行也在这里算**（``balance_totals`` / ``balance_verdict``）：
Excel 导出与 HTML 打印是两条通道，但它们印的是**同一张纸** ——
合计与试算必须一份实现、一套数字。以前 Excel 通道自己算、HTML 通道压根
没有合计行，于是「打出来的表」与「导出来的表」对不上。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import Book, Period, Subject
from app.printing.template import REPORT_LINE_NO, REPORT_STATIC, report_column_sides

_CATEGORY_CN = {"ASSET": "资产", "LIABILITY": "负债", "EQUITY": "所有者权益",
                "COST": "成本", "PROFIT": "损益"}

ZERO = Decimal("0")
CENT = Decimal("0.01")

#: 合计行用到的八个「按方向归过边」的数。键名与 ``queries.balance_sheet_of_accounts``
#: 的 ``check`` 一一对应（``check`` 里的键多一个 ``total_`` 前缀），别再另立一套叫法。
BALANCE_TOTAL_KEYS = ("opening_debit", "opening_credit", "period_debit", "period_credit",
                      "ytd_debit", "ytd_credit", "closing_debit", "closing_credit")

#: ``check`` 的键 -> 合计行的键。两边一一对应。
_CHECK_TO_TOTAL = {
    "total_opening_debit": "opening_debit",
    "total_opening_credit": "opening_credit",
    "total_debit": "period_debit",
    "total_credit": "period_credit",
    "total_ytd_debit": "ytd_debit",
    "total_ytd_credit": "ytd_credit",
    "total_closing_debit": "closing_debit",
    "total_closing_credit": "closing_credit",
}


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


def _counted_rows(rows: list[dict]) -> list[dict]:
    """这批行里**每棵子树只算一次**的那些行。

    判据是「在这批行里不是别的行的下级」：父级行本身就是下级的上卷，
    两边都加进去就是同一笔钱数两遍。

    * 「仅一级」档位：表体里只有一级行，它们互不为下级 → 全部参与；
    * 「全部」档位：父级行被丢掉 → 每棵子树只留根上那一行；
    * 「仅明细」档位：全是末级 → 全部参与。

    **不能按 ``is_leaf`` 筛**：「仅一级」档位下，带下级的一级科目
    ``is_leaf=False``，按 is_leaf 筛会把**整行丢掉** —— 那笔钱从合计里
    彻底消失，而同一行的「期末」来自 check、还在纸面上，于是合计行内部
    自相矛盾（审计 P1-4 / 工程 P1-1）。
    """
    if rows and all(r.get("subject_id") is not None for r in rows):
        parents = {r.get("parent_id") for r in rows if r.get("parent_id") is not None}
        return [r for r in rows if r.get("subject_id") not in parents]
    # 没有科目 id（老调用方手工构的行）：退回按 is_leaf 判定
    return [r for r in rows if r.get("is_leaf", True)]


def balance_totals(rows: list[dict], check: dict | None = None) -> dict[str, Decimal]:
    """科目余额表合计行 = **试算合计**，不是纸上那几行的算术和。

    以前这里是 ``for row in rows: totals[idx] += _d(row.get(key))``，两个错叠在
    一起，都落在同一张交给客户和审计的纸面上：

    1. **方向被吃掉了。** 行里的 ``opening`` / ``closing`` 来自
       ``display_side()``，是**绝对值** —— 贷方余额同样存正数（它靠旁边的
       「方向」列区分）。借方余额合计 1000 + 贷方余额合计 1000 直接相加，
       合计「期初」写成 2000，而下面两行试算还打着「期末余额平衡 ✓」：
       同一张纸上两处结论互相打脸，而 2000 不等于任何一边的合计。
       归边口径必须与 ``app/ledger/queries.py::balance_sheet_of_accounts``
       一致 —— 那边按 ``o_signed`` / ``c_signed`` 的符号拆借贷、只累计叶子，
       行里的「方向」列就是那次拆分的结果，所以这里按方向列归边即同一个口径。
    2. **重复计数。** 行是**过滤之后**的（``filter_balance_rows``：仅一级 /
       仅明细 / 全部）。默认「全部」时父子同在一张表上，父级行本身就是下级的
       上卷，逐行相加等于把同一笔钱数了两遍。

    **有 check 就用 check。** 它是叶子级、按符号拆好边的八个数，是这套系统里
    唯一一份试算口径的实现；本函数退化成「同样的算法」只为了在没有 check 的
    旧调用方（以及直接调本函数的测试）下仍然给出正确的数。

    **Excel 导出与 HTML 打印共用这一份**（以前 Excel 通道自己算、HTML 通道
    压根没有合计行，于是打出来的表与导出来的表对不上）。
    """
    totals = dict.fromkeys(BALANCE_TOTAL_KEYS, ZERO)
    for row in _counted_rows(rows):
        # Decimal 累加，不退回 float（见 excel._num 的说明）。逐格 round 也不能
        # 替代：round 是「四舍五入到分」，累加是「先加后舍」，两者在长表上不等价。
        if row.get("opening_direction") == "借":
            totals["opening_debit"] += _d(row.get("opening"))
        else:
            totals["opening_credit"] += _d(row.get("opening"))
        totals["period_debit"] += _d(row.get("period_debit"))
        totals["period_credit"] += _d(row.get("period_credit"))
        totals["ytd_debit"] += _d(row.get("ytd_debit"))
        totals["ytd_credit"] += _d(row.get("ytd_credit"))
        if row.get("direction") == "借":
            totals["closing_debit"] += _d(row.get("closing"))
        else:
            totals["closing_credit"] += _d(row.get("closing"))

    for check_key, total_key in _CHECK_TO_TOTAL.items():
        value = (check or {}).get(check_key)
        if value is not None:
            totals[total_key] = _d(value)
    return totals


def balance_verdict(check: dict, key: str, label: str,
                    debit: Decimal, credit: Decimal) -> str:
    """试算行：「平衡 ✓ / 不平衡 ✗」+ 两边的数。

    只打 ✓ 不打数，等于让读纸面的人自己回头把几百行加一遍 —— 而这份表
    的用途正是**免去**加法（交给审计方逐格核对）。不平衡时两边的差额一眼可见。
    """
    def money(v: Decimal) -> str:
        return f"{v.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"

    mark = "平衡 ✓" if (check or {}).get(key) else "不平衡 ✗"
    return f"{label}{mark}（借 {money(debit)} / 贷 {money(credit)}）"


def balance_rows(rows: list[dict], check: dict | None = None) -> list[dict]:
    """科目余额表 -> 打印行。带「上期末」「本期发生」两类分栏。

    **末尾带合计行与两行试算**（口径与 Excel 导出同一份实现，见
    ``balance_totals``）。以前 HTML 打印通道只有明细行、没有合计 —— 于是
    「打出来的那张」与「导出来的那张」对不上，而余额表正是交给客户和审计的
    纸面数（审计 P3-3）。
    """
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
    totals = balance_totals(rows, check)
    out.append({
        "code": "", "name": "合计", "level": "",
        "opening_direction": "", "opening": "",
        "opening_debit": totals["opening_debit"],
        "opening_credit": totals["opening_credit"],
        "period_debit": totals["period_debit"],
        "period_credit": totals["period_credit"],
        "closing_direction": "", "closing": "",
        "closing_debit": totals["closing_debit"],
        "closing_credit": totals["closing_credit"],
        "__kind": "TOTAL",
    })
    # 试算两行。文案放在**名称列**（左对齐、可折行）而不是金额列 ——
    # 金额列是右对齐且不折行（``td.num`` 的 white-space:nowrap），长句会横着
    # 压到旁边的格子上。
    out.append({
        "code": "", "name": "试算平衡：" + balance_verdict(
            check or {}, "period_balanced", "本期发生额",
            totals["period_debit"], totals["period_credit"]),
        "level": "", "opening_debit": "", "opening_credit": "",
        "period_debit": "", "period_credit": "",
        "closing_debit": "", "closing_credit": "",
        "__kind": "CHECK",
    })
    out.append({
        "code": "", "name": "试算平衡：" + balance_verdict(
            check or {}, "closing_balanced", "期末余额",
            totals["closing_debit"], totals["closing_credit"]),
        "level": "", "opening_debit": "", "opening_credit": "",
        "period_debit": "", "period_credit": "",
        "closing_debit": "", "closing_credit": "",
        "__kind": "CHECK",
    })
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
#:
#: 常量本身住在 ``app.printing.template`` —— **Excel 导出与 HTML 打印共用**，
#: 「列属于哪一半」的判据也只有那一份（``report_column_sides``）。


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
    # 每一列属于左半还是右半：**共享实现**（``app.printing.template``）。
    #
    # 判据是「STATIC 列开启的分块」：STATIC 是每一半的首列，从它到下一个
    # STATIC 之间的所有列（含行次列与金额列）都属于同一半。
    #
    # 只给 STATIC/LINE_NO 编号是不够的 —— 金额列取不到 side，就会一律落到左半，
    # 于是「非流动资产合计」那一行右半的两个空金额格也印成合计（加粗）。
    # 单栏表只有一个 STATIC，全表都落在左半，同理不需要分支。
    #
    # Excel 导出那条通道也调它 —— 以前两边各写一套（Excel 侧是两个独立
    # 计数器），两套实现迟早分叉（审计 P3-2）。
    side_of: dict[Any, int] = report_column_sides(cols)

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
                # **公式出错的格子印出错误文案**，不是一个空格。
                #
                # 空格子在纸上与「这一项真的是 0」长得一模一样，而后者是账、
                # 是结论。一格公式写坏了（期间字面量畸形、科目编码打错…），
                # 印出来却是一个空格子，读者只会以为那项没有数。
                item[key] = cell.get("error") or cell.get("value", "")
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
