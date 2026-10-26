#!/usr/bin/env python3
"""造一份**用友格式**的科目余额表 .xls（真 OLE 二进制，不是改名的 xlsx）。

    .venv/bin/python scripts/make_ufida_sample_xls.py [输出路径]

用途：演示「期初余额可以直接导进用友导出的余额表」。

这份文件是**照着用友 U8 的版式**排的，不是随便拼个表：

* 表头上方带标题行、制表单位行（用友导出就是这样，所以表头不能写死第 1 行）
* 表头横跨两行：第一行写「期初 / 本期 / 本年 / 期末」，第二行才写「借方 / 贷方」
* 借贷**分列**（用友余额表就是分列的，不靠正负猜方向）
* 科目名称用前导空格表示级次（用友的老规矩）
* 明细行之间夹着「资产小计 / 负债小计 / 合计」这类汇总行
* 大量全零的行（真实导出里占绝大多数）

数据取自账上：期初余额、1 月发生额、期末余额都是从余额表查出来的真数，
所以这份文件导入回去，得到的就是这套账本身。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import xlwt  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.ledger import queries  # noqa: E402
from app.models import Book, Period, Subject  # noqa: E402

BOOK_NAME = "宏发商贸"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "output" / "demo-data" / "科目余额表.xls"

#: 用友余额表里那几种汇总行。整行跳过（它是加总过的数，当成科目会翻倍）。
SUMMARY_ROWS = [
    ("资产小计", 1), ("负债小计", 1), ("权益小计", 1), ("损益小计", 1),
]


def build(out_path: Path) -> Path:
    db = SessionLocal()
    try:
        book = db.scalar(select(Book).where(Book.name == BOOK_NAME))
        if book is None:
            raise SystemExit(f"找不到账套《{BOOK_NAME}》，先跑 seed_trade_demo.py")

        period = db.scalar(select(Period).where(
            Period.book_id == book.id, Period.year == 2026, Period.month == 1))
        subjects = list(db.scalars(select(Subject).where(
            Subject.book_id == book.id, Subject.year == 2026)).all())
        tree = queries.subject_tree(db, book.id, 2026)
        rows, _check = queries.balance_sheet_of_accounts(db, book.id, tree, period, False)

        wb = xlwt.Workbook(encoding="utf-8")
        ws = wb.add_sheet("科目余额表")

        # ---- 表头上方的标题行与制表单位行（用友导出就是长这样）----
        title = xlwt.easyxf("font: bold on, height 320;")
        sub = xlwt.easyxf("align: horiz center;")
        ws.write(0, 0, "科目余额表", title)
        ws.write(1, 0, f"编制单位：{book.company_name}", sub)
        ws.write(1, 6, "会计期间：2026 年 01 月", sub)
        ws.write(1, 9, "单位：元", sub)

        # ---- 表头：横跨两行（第一行口径、第二行方向）----
        head1 = xlwt.easyxf("font: bold on; align: horiz center, vert center; borders: left thin, right thin, top thin, bottom thin")
        head2 = xlwt.easyxf("font: bold on; align: horiz center, vert center; borders: left thin, right thin, top thin, bottom thin")
        top = ["科目编码", "科目名称", "方向", "期初", "", "本期", "", "本年", "", "期末", ""]
        bottom = ["", "", "", "借方", "贷方", "借方", "贷方", "借方", "贷方", "借方", "贷方"]
        for c, v in enumerate(top):
            ws.write(3, c, v, head1)
        for c, v in enumerate(bottom):
            ws.write(4, c, v, head2)

        body = xlwt.easyxf("borders: left thin, right thin, top thin, bottom thin")
        body_b = xlwt.easyxf("font: bold on; borders: left thin, right thin, top thin, bottom thin")

        r = 5
        leaves = [x for x in rows if x["is_leaf"]]
        leaves.sort(key=lambda x: x["code"])

        def write_row(code, name, direction, o_dr, o_cr, p_dr, p_cr,
                      y_dr, y_cr, c_dr, c_cr, bold=False):
            nonlocal r
            fx = body_b if bold else body
            ws.write(r, 0, code, fx)
            ws.write(r, 1, name, fx)
            ws.write(r, 2, direction, fx)
            for c, v in enumerate([o_dr, o_cr, p_dr, p_cr, y_dr, y_cr, c_dr, c_cr]):
                ws.write(r, 3 + c, v, fx)
            r += 1

        # 用友的老规矩：科目名称用前导空格表示级次
        def spaced(level: int, name: str) -> str:
            return "    " * (level - 1) + name

        last_cat = None
        for row in leaves:
            cat = row["category"]
            if last_cat is not None and cat != last_cat:
                label = {"ASSET": "资产小计", "LIABILITY": "负债小计",
                         "EQUITY": "权益小计", "PROFIT": "损益小计"}.get(cat, "小计")
                write_row("", label, "", 0, 0, 0, 0, 0, 0, 0, 0, bold=True)
            last_cat = cat

            o_side, o_amt = row["opening_direction"], row["opening"]
            c_side, c_amt = row["direction"], row["closing"]
            o_dr = o_amt if o_side == "借" else 0
            o_cr = o_amt if o_side == "贷" else 0
            c_dr = c_amt if c_side == "借" else 0
            c_cr = c_amt if c_side == "贷" else 0
            write_row(
                row["code"], spaced(row["level"], row["name"]),
                c_side if c_side != "平" else "平",
                o_dr, o_cr,
                float(row["period_debit"]), float(row["period_credit"]),
                float(row["ytd_debit"]), float(row["ytd_credit"]),
                c_dr, c_cr,
            )

        write_row("", "合计", "", 0, 0, 0, 0, 0, 0, 0, 0, bold=True)

        # 列宽：用友导出时人看着舒服
        for c, w in enumerate([3200, 5200, 1200, 3000, 3000, 3000, 3000,
                               3000, 3000, 3000, 3000]):
            ws.col(c).width = w

        out_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(out_path))
        print(f"已生成：{out_path}")
        print(f"明细行 {len(leaves)} 条（其中有余额的 "
              f"{sum(1 for x in leaves if x['opening'])} 条），表头第 4~5 行")
        return out_path
    finally:
        db.close()


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    build(out)
