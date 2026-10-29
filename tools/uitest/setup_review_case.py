"""浏览器实测「待复核」用的造数脚本（dev 库专用，**可重复跑**）。

造的场景：**账上有同一笔，但日期差 10 天** —— 账上记的是**发票日期**，
对账单是**实际支付日期**（2026-10-29 用户报的那个问题）。

那一行在界面上应该是「待复核」，并且**不能**出现在可制证清单里
（点「生成流水凭证」就会把同一笔钱记两遍）。

**造两行，不是一行**：界面上有两颗按钮 ——「是同一笔」与「不是同一笔」。
两条路都得真点一遍才算验过，只验一条等于只验了一半。

**为什么能重复跑**：界面上点过「是同一笔」之后，那一行就变成「已入账」，
再跑一遍脚本时它不会自己回到「待复核」—— 于是第二遍脚本必然失败，
而且失败得莫名其妙（界面上就是找不到「待复核」三个字）。
所以每次跑之前先把上一轮的痕迹抹掉：凭证按摘要认、流水行回到 OPEN。

跑法::

    cd backend && .venv/bin/python ../tools/uitest/setup_review_case.py --seed
    cd backend && .venv/bin/python ../tools/uitest/setup_review_case.py --clean
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
OTHER_CODE = "5001"       # 对方科目（这本账走的是小企业会计准则的编码）
FILENAME = "uitest-待复核.csv"
TAG = "uitest-待复核"

#: （行摘要后缀，流水日期，账上凭证日期，金额）
#:
#: **两行金额必须不同** —— 金额一样的话，同额的候选不止一个，
#: 配对就变成「挑一个」而不是「认出那个」，验的就不是同一件事了。
CASES = (
    ("甲", date(2026, 12, 25), date(2026, 12, 15), Decimal("5000.00")),
    ("乙", date(2026, 12, 26), date(2026, 12, 16), Decimal("3000.00")),
)


def _csv() -> bytes:
    """一份 12 月的对账单：两行收入，日期、金额各不相同。"""
    lines = [
        "# 某某银行对账单",
        "# 账号：6222****0001",
        "# 户名：测试账户",
        "# 起始日期：20261201",
        "# 结束日期：20261231",
        "交易日期,交易时间,收入,支出,余额,交易类型,交易备注",
    ]
    balance = Decimal("0")
    for suffix, row_date, _voucher_date, amount in CASES:
        balance += amount
        lines.append(
            f"{row_date.isoformat()},10:00:00,{amount},0.0,{balance},"
            f"行内转账,{TAG}-{suffix}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cleanup(db) -> None:
    """抹掉上一轮留下的：对账单 + 带标记的凭证。

    **凭证按分录摘要认**（不是按 remark）：remark 可以被用户改，
    摘要不会被随手改 —— 按 remark 认会漏掉一部分，漏掉的后果是
    「造出两张同额的候选」，配对就乱了（界面上的候选会指向旧的那张）。

    **认的是「摘要以标记开头」**：早期那版脚本写的摘要带后缀
    （「uitest-待复核 发票日付款」），后来又改成「uitest-待复核-甲」——
    按前缀认，两种都盖得住；按「等于标记」认就会漏掉前者。
    """
    for old in db.scalars(select(Voucher).where(Voucher.book_id == BOOK_ID)).all():
        if any((e.summary or "").startswith(TAG) for e in old.entries):
            for e in list(old.entries):
                db.delete(e)
            db.delete(old)
    statement = db.scalar(select(BankStatement).where(
        BankStatement.filename == FILENAME))
    if statement is not None:
        # 原件也删掉 —— 留着就成了没人认领的垃圾文件。
        try:
            storage.delete(statement.file_path)
        except OSError:
            pass
        db.delete(statement)      # 行与对方科目由 ORM 级联删掉
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
            Subject.book_id == BOOK_ID, Subject.code == ACCOUNT_CODE, Subject.year == 2026))
        other = db.scalar(select(Subject).where(
            Subject.book_id == BOOK_ID, Subject.code == OTHER_CODE, Subject.year == 2026))
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

        for suffix, _row_date, voucher_date, amount in CASES:
            voucher = voucher_service.create_voucher(
                db,
                book_id=BOOK_ID,
                user=admin,
                voucher_date=voucher_date,
                entries=[
                    {"subject_id": account.id, "direction": "DEBIT",
                     "amount": amount, "summary": f"{TAG}-{suffix}"},
                    {"subject_id": other.id, "direction": "CREDIT",
                     "amount": amount, "summary": f"{TAG}-{suffix}"},
                ],
                remark=TAG,
                source="MANUAL",
                commit=False,
            )
            db.commit()
            print(f"造好了凭证 #{voucher.id} {voucher.voucher_no} "
                  f"{voucher_date} 金额 {amount}")

        # ---- **自己验一遍前提**：这两行真的落到「待复核」了吗？ ----
        #
        # **不验的话，界面自查就会空转即绿**：界面上找不到「待复核」，
        # 脚本只报一句「界面上没有这个状态」，而真正的原因（造数没造对）
        # 被埋在几十行之外。
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
        if bad or len(rows) != len(CASES):
            print(f"前提不成立：{bad} 行没落到待复核（造数场景没造对）")
            return 1
        print("两行都落在「待复核」，可以跑界面自查了")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
