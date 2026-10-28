#!/usr/bin/env python3
"""给「跨期候选」的界面自查造数据（以及清理它）。

**为什么要有这个脚本**：跨期提示是**界面上才看得见**的东西 —— 后端用例能证明
「候选带 days_diff / in_window」，但证明不了「那句话真的显示在弹窗里」。而这个
场景在真实数据里很罕见（账上要有一笔记在别的现金科目上、金额相同、日期差个
十来天的分录），靠自然数据撞出来的概率极低。

所以这里**造**出来：dev 库里加一行带标记的流水 + 两张凭证 ——
- 一张差 3 天（主窗口内）→ 界面上**不该**有跨期提示
- 一张差 10 天（跨期）→ 界面上**该**有「日期相差 10 天，请确认是不是同一笔」

两条摆在一起对比，才看得出「提示只出现在该出现的地方」。

**它只动自己造的东西**：所有东西都带 ``__uitest_cross__`` 标记，清理按标记
精确删。造之前先清一遍，所以反复跑不会攒下垃圾。

用法::

    python tools/uitest/seed_cross_period_case.py --seed     # 造
    python tools/uitest/seed_cross_period_case.py --clean    # 清

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
from app.models import Subject, SysUser  # noqa: E402
from app.models.bankstatement import BankStatementRow  # noqa: E402

MARK = "__uitest_cross__"
BOOK_ID = 18          # dev 库里的「宏发商贸」
YEAR = 2026
TX_DATE = date(2026, 12, 20)      # 12 月那个期间没结账，才能新增凭证
AMOUNT = "1141.00"
CASH_CODE = "100202"   # 另一个银行账户：对账单绑的是 100201，所以它算「别的账户」
#                    （2026-10-28 口径变了：这一条线只认 1002 银行存款，
#                      1001 库存现金与 1012 第三方支付备付金都不算 ——
#                      支付宝那种账户根本对不上网银流水）
OTHER_CODE = "5001"


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
    db.execute(text("delete from bank_statement_row where summary like :m"),
               {"m": f"%{MARK}%"})
    return int(n)


def seed(db) -> None:
    user = db.scalars(select(SysUser).where(SysUser.is_admin.is_(True))).first()
    if user is None:
        raise SystemExit("dev 库里没有管理员 —— 先跑起来 dev 环境")
    cash = db.scalar(select(Subject).where(
        Subject.book_id == BOOK_ID, Subject.year == YEAR, Subject.code == CASH_CODE))
    other = db.scalar(select(Subject).where(
        Subject.book_id == BOOK_ID, Subject.year == YEAR, Subject.code == OTHER_CODE))
    if cash is None or other is None:
        raise SystemExit(f"dev 库里没有 {CASH_CODE} / {OTHER_CODE} 科目")

    row = BankStatementRow(
        statement_id=1, book_id=BOOK_ID, seq=99, tx_date=TX_DATE, direction="IN",
        amount=AMOUNT, balance=0, summary=f"{MARK} 测试流水", purpose="",
        counterparty_name="测试对手方", counterparty_account="", extra={},
        status="OPEN", plan_status="PENDING", problems=[])
    db.add(row)
    db.flush()
    print(f"造好测试流水：id={row.id} seq={row.seq} {TX_DATE} {AMOUNT} IN")

    for label, when in (("窗口内(差3天)", date(2026, 12, 17)),
                        ("跨期(差10天)", date(2026, 12, 10))):
        v = voucher_service.create_voucher(
            db, book_id=BOOK_ID, user=user, voucher_date=when,
            entries=[
                {"subject_id": cash.id, "direction": "DEBIT",
                 "amount": AMOUNT, "summary": f"{MARK} {label}"},
                {"subject_id": other.id, "direction": "CREDIT",
                 "amount": AMOUNT, "summary": f"{MARK} {label}"},
            ],
            source="MANUAL", remark=f"{MARK} {label}", commit=False)
        print(f"造好 {label}：凭证 id={v.id} 日期 {when}")


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
