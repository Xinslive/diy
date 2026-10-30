"""浏览器实测「判「不是同一笔」判不完」那个死循环用的造数脚本（dev 库专用）。

造的场景就是**生产上那一幕**（2026-10-30 用户报的）：

* 对账单上**两行同额流水**（18.05，高速通行费，同一天）；
* 账上**四张同额凭证**（同一个月的高速通行费，日期各不相同）。

同额的几行与同额的凭证之间**分不清谁是谁** —— 这正是「待复核」这一档
存在的原因。用户要在两行上点「不是同一笔」，把账上那几张一张一张否掉。

**这个脚本造出来的场景必须验两件事**（跑 :mod:`tools.uitest.ui_flow_review`
那一套浏览器自查时用）：

1. **点下去要真的停下来** —— 连点若干次之后不该再有「待复核」；
2. **判过的那张不再冒出来** —— 无论是回到原来那一行，还是挪到旁边那一行。

**为什么能重复跑**：跑之前先把上一轮的痕迹抹掉（按摘要认凭证、按文件名
认对账单），造之前先清一遍，所以反复跑不会攒下垃圾。

跑法::

    cd backend && .venv/bin/python ../tools/uitest/setup_review_loop_case.py --seed
    cd backend && .venv/bin/python ../tools/uitest/setup_review_loop_case.py --clean
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select  # noqa: E402

from app.accounting import vouchers as voucher_service  # noqa: E402
from app.bankstatement import service as statement_service  # noqa: E402
from app.bankstatement import storage  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import Subject, SysUser, Voucher  # noqa: E402
from app.models.bankstatement import BankStatement, BankStatementRow  # noqa: E402

BOOK_ID = 18
ACCOUNT_CODE = "100201"   # 对账单绑定的账户（银行存款-基本户）
OTHER_CODE = "5001"       # 对方科目
FILENAME = "uitest-判否死循环.csv"
TAG = "uitest-判否死循环"

AMOUNT = Decimal("18.05")
ROW_DATE = date(2026, 12, 25)
#: 账上那四张凭证的日期 —— **都在复核窗口内（≤30 天）但都在主窗口外
#: （>7 天）**，所以它们是「待复核」的候选而不是「已入账」。
VOUCHER_DATES = (date(2026, 12, 5), date(2026, 12, 5),
                 date(2026, 12, 6), date(2026, 12, 7))


def _csv() -> bytes:
    """一份 12 月的对账单：**两行同额同日的支出**。"""
    lines = [
        "# 某某银行对账单",
        "# 账号：6222****0001",
        "# 户名：测试账户",
        "# 起始日期：20261201",
        "# 结束日期：20261231",
        "交易日期,交易时间,收入,支出,余额,交易类型,交易备注",
    ]
    balance = Decimal("0")
    for index in range(2):
        balance -= AMOUNT
        lines.append(
            f"{ROW_DATE.isoformat()},10:00:00,0.0,{AMOUNT},{balance},"
            f"行内转账,{TAG}-{index + 1}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cleanup(db) -> None:
    """抹掉上一轮留下的：对账单 + 带标记的凭证。"""
    for old in db.scalars(select(Voucher).where(Voucher.book_id == BOOK_ID)).all():
        if any((e.summary or "").startswith(TAG) for e in old.entries):
            for e in list(old.entries):
                db.delete(e)
            db.delete(old)
    statement = db.scalar(select(BankStatement).where(
        BankStatement.filename == FILENAME))
    if statement is not None:
        try:
            storage.delete(statement.file_path)
        except OSError:
            pass
        db.delete(statement)
    db.commit()


def main() -> int:
    clean_only = "--clean" in sys.argv
    with SessionLocal() as db:
        admin = db.scalar(select(SysUser).where(SysUser.username == "admin"))
        if admin is None:
            print("没有 admin 用户")
            return 1

        _cleanup(db)
        if clean_only:
            print("清掉了")
            return 0

        account = db.scalar(select(Subject).where(
            Subject.book_id == BOOK_ID, Subject.code == ACCOUNT_CODE,
            Subject.year == 2026))
        other = db.scalar(select(Subject).where(
            Subject.book_id == BOOK_ID, Subject.code == OTHER_CODE,
            Subject.year == 2026))
        if account is None or other is None:
            print("造数前置条件不满足（科目不在）")
            return 1

        settings.statement_storage_dir = str(
            Path(settings.statement_storage_dir).parent / "statements")
        statement = statement_service.upload_statement(
            db, BOOK_ID, admin, FILENAME, _csv(), account.id)
        db.commit()
        print(f"造好了对账单 #{statement['id']} {FILENAME} "
              f"（{statement.get('period_start')} ~ {statement.get('period_end')}）")

        for index, voucher_date in enumerate(VOUCHER_DATES):
            voucher = voucher_service.create_voucher(
                db,
                book_id=BOOK_ID,
                user=admin,
                voucher_date=voucher_date,
                entries=[
                    # **支出**：现金侧是贷方（银行账方向与会计方向相反）。
                    {"subject_id": account.id, "direction": "CREDIT",
                     "amount": AMOUNT, "summary": f"{TAG}-{index + 1}"},
                    {"subject_id": other.id, "direction": "DEBIT",
                     "amount": AMOUNT, "summary": f"{TAG}-{index + 1}"},
                ],
                remark=TAG,
                source="MANUAL",
                commit=False,
            )
            db.commit()
            print(f"造好了凭证 #{voucher.id} {voucher.voucher_no} "
                  f"{voucher_date} 金额 {AMOUNT}")

        # ---- **自己验一遍前提**：两行都落在「待复核」了吗？ ----
        plan = statement_service.plan_vouchers(db, BOOK_ID, statement["id"])
        db.commit()
        review_ids = {int(item["id"]) for item in plan.get("review", [])}
        rows = db.scalars(select(BankStatementRow).where(
            BankStatementRow.statement_id == statement["id"]).order_by(
            BankStatementRow.seq)).all()
        bad = 0
        for row in rows:
            ok = int(row.id) in review_ids
            bad += 0 if ok else 1
            print(f"   行 #{row.id} seq={row.seq} {row.tx_date} {row.amount} "
                  f"状态={row.status} {'✓ 待复核' if ok else '✗ 不是待复核'}")
        if bad or len(rows) != 2:
            print(f"前提不成立：{bad} 行没落到待复核（造数场景没造对）")
            return 1
        print("两行都落在「待复核」，可以跑界面自查了")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
