"""浏览器实测「可改科目」用的一次性造数脚本（dev 库专用）。

造的场景：**跨期候选**（凭证日期离流水日期 20 天）——
这种候选以前改完之后，「已入账」标记会被去重扫描（±7 天）收回去。

跑法::

    cd backend && .venv/bin/python ../tools/uitest/setup_reclassify_case.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select  # noqa: E402

from app.accounting import vouchers as voucher_service  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import Entry, Subject, SysUser, Voucher  # noqa: E402
from app.models.bankstatement import BankStatement, BankStatementRow  # noqa: E402

STATEMENT_ID = 1
ROW_SEQ = 3
ACCOUNT_CODE = "100201"      # 对账单绑定的账户（银行存款）
CASH_CODE = "1001"           # 候选凭证的现金侧（库存现金）
OFFSET_DAYS = 20             # 跨期天数：> 7 天（去重窗口），<= 30 天（候选窗口）
ROW_DATE = date(2026, 12, 5)  # 挪到未结账的期间（dev 账套 2026-09~11 都结账了）
TAG = "uitest-可改科目"


def main() -> int:
    with SessionLocal() as db:
        admin = db.scalar(select(SysUser).where(SysUser.username == "admin"))
        statement = db.get(BankStatement, STATEMENT_ID)
        account = db.scalar(select(Subject).where(
            Subject.book_id == statement.book_id,
            Subject.code == ACCOUNT_CODE, Subject.year == 2026))
        cash = db.scalar(select(Subject).where(
            Subject.book_id == statement.book_id,
            Subject.code == CASH_CODE, Subject.year == 2026))
        income = db.scalar(select(Subject).where(
            Subject.book_id == statement.book_id,
            Subject.code == "5001", Subject.year == 2026))
        row = db.scalar(select(BankStatementRow).where(
            BankStatementRow.statement_id == STATEMENT_ID,
            BankStatementRow.seq == ROW_SEQ))

        # **先清掉上一轮造的**（按摘要认，免得造出两张同额的候选）。
        for old in db.scalars(select(Voucher).where(Voucher.book_id == statement.book_id)).all():
            if any((e.summary or "") == TAG for e in old.entries):
                for e in list(old.entries):
                    db.delete(e)
                db.delete(old)
        db.commit()

        print(f"对账单 {STATEMENT_ID} 原账户 {statement.account_subject_id} "
              f"-> {ACCOUNT_CODE}({account.id})")
        statement.account_subject_id = account.id

        # 这一行回到「待确认科目」的干净状态。
        row.status = "OPEN"
        row.plan_status = "PENDING"
        row.matched_voucher_id = None
        row.summary_override = ""
        # **挪到未结账的期间** —— dev 账套 2026-09~11 都结账了，
        # 凭证落在结账期间里根本建不出来（库层会挡）。
        row.tx_date = ROW_DATE

        voucher_date = ROW_DATE + timedelta(days=OFFSET_DAYS)
        voucher = voucher_service.create_voucher(
            db, book_id=statement.book_id, user=admin,
            voucher_date=voucher_date,
            entries=[
                {"subject_id": cash.id, "direction": "DEBIT",
                 "amount": str(row.amount), "summary": TAG},
                {"subject_id": income.id, "direction": "CREDIT",
                 "amount": str(row.amount), "summary": TAG},
            ],
            source="MANUAL", commit=False)
        db.commit()
        db.refresh(voucher)
        print(f"造了候选凭证 {voucher.voucher_no}（id {voucher.id}）"
              f" 日期 {voucher_date} 现金侧 {CASH_CODE} 金额 {row.amount}")
        print(f"目标流水行 seq={ROW_SEQ} 日期 {row.tx_date} 金额 {row.amount} "
              f"-> 日期差 {abs((voucher_date - row.tx_date).days)} 天")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
