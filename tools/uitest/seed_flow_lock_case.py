#!/usr/bin/env python3
"""给「已入账的行不许再指定科目」的界面自查造数据（以及清理它）。

**为什么要有这个脚本**：这个缺陷**只发生在「已入账」的行上**，而自然数据里
这种行很少 —— dev 库里一条都没有。没有它，界面自查就会「空转即绿」：整个
检查一步都不跑，却显示通过。

所以这里**造**出来两条带 ``__uitest_lock__`` 标记的流水：

- 一条 **OPEN** —— 用来验「忽略」之后「恢复」点得动；
- 一条 **POSTED**（带凭证、对方科目已定）—— 用来验「对方科目」按钮是灰的，
  且旁边写着为什么。

**它只动自己造的东西**：所有东西都带 ``__uitest_lock__`` 标记，清理按标记
精确删。造之前先清一遍，所以反复跑不会攒下垃圾。

用法::

    python tools/uitest/seed_flow_lock_case.py --seed     # 造
    python tools/uitest/seed_flow_lock_case.py --clean    # 清

（需要在 dev 环境下跑：后端 venv + dev 库。）
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from sqlalchemy import select, text  # noqa: E402

from app.accounting import vouchers as voucher_service  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import BankStatement, Subject, SysUser  # noqa: E402
from app.models.bankstatement import (  # noqa: E402
    BankStatementRow,
    BankStatementRowSubject,
)

MARK = "__uitest_lock__"
BOOK_ID = 18          # dev 库里的「宏发商贸」
YEAR = 2026
STATEMENT_ID = 1      # dev 库里的 cmb_csv.xlsx
TX_DATE = date(2026, 12, 20)      # 12 月那个期间没结账，才能新增凭证
AMOUNT = "1207.00"
#: 那条 OPEN 流水的金额与日期：**必须与已入账那条区分开**。
#:
#: ``plan_vouchers`` 每次都扫一遍「这个账户上有没有**同额同向**、日期在 ±7 天内
#: 的分录」，扫到就把那行标成已入账 —— 两条长得一模一样，它们会被当成**同一笔**
#: （这正是去重该做的事）。于是 OPEN 那条也会被标成已入账，「忽略」按钮随之
#: 变灰 —— 测的就不是「忽略 ↔ 恢复」了。
OPEN_AMOUNT = "1307.00"
OPEN_DATE = date(2026, 12, 19)
OTHER_CODE = "560101"    # 商品维修费：当对方科目（借方那一侧）


def _clean(db) -> int:
    """按标记删干净自己造的东西。"""
    n = db.execute(text(
        "select count(*) from bank_statement_row where summary like :m"),
        {"m": f"%{MARK}%"}).scalar()
    db.execute(text("""
        delete from entry where voucher_id in (
          select v.id from voucher v
           join entry e on e.voucher_id = v.id
          where e.summary like :m)
    """), {"m": f"%{MARK}%"})
    db.execute(text("delete from voucher where remark like :m"), {"m": f"%{MARK}%"})
    # 子表跟着流水行一起删（外键是 ON DELETE CASCADE），这里仍显式清一遍：
    # 万一哪天级联被去掉，这里不会留下孤儿行。
    db.execute(text("""
        delete from bank_statement_row_subject where row_id in (
          select id from bank_statement_row where summary like :m)
    """), {"m": f"%{MARK}%"})
    db.execute(text("delete from bank_statement_row where summary like :m"),
               {"m": f"%{MARK}%"})
    return int(n)


def seed(db) -> None:
    user = db.scalars(select(SysUser).where(SysUser.is_admin.is_(True))).first()
    if user is None:
        raise SystemExit("dev 库里没有管理员 —— 先跑起来 dev 环境")
    other = db.scalar(select(Subject).where(
        Subject.book_id == BOOK_ID, Subject.year == YEAR, Subject.code == OTHER_CODE))
    if other is None:
        raise SystemExit(f"dev 库里没有 {OTHER_CODE} 科目")

    # **现金侧必须是这份对账单绑定的账户科目**（1001 库存现金）。
    #
    # 这不是讲究，是**硬要求**：``plan_vouchers`` 每次都会重新扫一遍
    # 「这个账户上有没有同额同向的分录」，**扫不到就把「已入账」的标记收回**
    # （行退回 OPEN、matched_voucher_id 清空）。凭证的现金侧记在别的科目上，
    # 标记当场就被收走了 —— 界面上那一行会显示「可制证」，而我们要验的
    # 恰恰是「已入账」。
    statement = db.get(BankStatement, STATEMENT_ID)
    if statement is None:
        raise SystemExit("dev 库里没有 statement 1")
    cash = db.get(Subject, statement.account_subject_id)
    if cash is None:
        raise SystemExit("这份对账单绑的账户科目不存在")

    # ---- 一条 OPEN 的：验「忽略 → 恢复」 --------------------------------
    open_row = BankStatementRow(
        statement_id=STATEMENT_ID, book_id=BOOK_ID, seq=98, tx_date=OPEN_DATE,
        direction="OUT", amount=OPEN_AMOUNT, balance=0,
        summary=f"{MARK} 可忽略的流水", purpose="", counterparty_name="测试对手方",
        counterparty_account="", extra={}, status="OPEN", plan_status="PENDING",
        problems=[])
    db.add(open_row)
    db.flush()
    print(f"造好 OPEN 流水：id={open_row.id} seq={open_row.seq}")

    # ---- 一条 POSTED 的：验「对方科目」按钮是灰的 ----------------------
    posted_row = BankStatementRow(
        statement_id=STATEMENT_ID, book_id=BOOK_ID, seq=99, tx_date=TX_DATE,
        direction="OUT", amount=AMOUNT, balance=0,
        summary=f"{MARK} 已入账的流水", purpose="", counterparty_name="测试对手方",
        counterparty_account="", extra={}, status="OPEN", plan_status="MANUAL",
        problems=[])
    db.add(posted_row)
    db.flush()
    db.add(BankStatementRowSubject(
        row_id=posted_row.id, subject_id=other.id, amount=AMOUNT,
        source="MANUAL", sort_no=1))
    db.flush()

    # 支出（OUT）⇒ 现金减少 ⇒ 现金侧是**贷方**（与银行账的方向相反）。
    voucher = voucher_service.create_voucher(
        db, book_id=BOOK_ID, user=user, voucher_date=TX_DATE,
        entries=[
            {"subject_id": other.id, "direction": "DEBIT",
             "amount": AMOUNT, "summary": f"{MARK} 已入账的流水"},
            {"subject_id": cash.id, "direction": "CREDIT",
             "amount": AMOUNT, "summary": f"{MARK} 已入账的流水"},
        ],
        source="MANUAL", remark=f"{MARK} 已入账的流水", commit=False)
    posted_row.status = "POSTED"
    posted_row.matched_voucher_id = voucher.id
    db.flush()
    print(f"造好 POSTED 流水：id={posted_row.id} seq={posted_row.seq} "
          f"凭证 id={voucher.id}（现金侧 {cash.code} {cash.name}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    if not args.seed and not args.clean:
        ap.error("要么 --seed，要么 --clean")

    with SessionLocal() as db:
        removed = _clean(db)
        if removed:
            print(f"先清掉上次留下的 {removed} 行流水")
        if args.seed:
            seed(db)
        db.commit()
    print("好了" if args.seed else "清干净了")


if __name__ == "__main__":
    main()
