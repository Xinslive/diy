#!/usr/bin/env python
"""生成一套可以直接上手看的演示账套。

    .venv/bin/python scripts/seed_demo.py            # 生成
    .venv/bin/python scripts/seed_demo.py --reset    # 先删掉旧的演示账套再生成
    .venv/bin/python scripts/seed_demo.py --clean    # 只清理历史测试残留

场景：启明物业管理有限公司 2026 年度。
    1~11 月  —— 已结转损益、已结账（状态完整，可以看结转凭证、反结账效果）
    12 月    —— 业务已录入但**未结转未结账**，正好可以上手试「结转损益 → 结账 → 年度结转」

金额全部写死（不用随机），所以每次生成的数据完全一致，便于对着核。
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.accounting.periods import (  # noqa: E402
    carry_forward_profit, close_period, create_periods, ensure_periods,
)
from app.accounting.vouchers import create_voucher, post_vouchers  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.ledger import queries  # noqa: E402
from app.ledger.engine import LedgerEngine  # noqa: E402
from app.models import (  # noqa: E402
    Book, Entry, Period, PeriodOpening, ReportTemplate, Subject, SysUser,
    UserBook, Voucher,
)
from app.seed.chart_of_accounts import install_chart  # noqa: E402

D = Decimal
BOOK_NAME = "启明物业"
YEAR = 2026

# ---------------------------------------------------------------- 期初余额
# 借方合计 4,770,000 = 贷方合计 4,770,000（贷方类按借正贷负存负数）
OPENINGS = [
    ("1001", "20000"), ("100201", "1850000"), ("1122", "320000"),
    ("1405", "180000"), ("1601", "2400000"),
    ("1602", "-480000"), ("2001", "-1000000"), ("2202", "-150000"),
    ("2211", "-86000"), ("2221", "-42000"), ("4001", "-2000000"),
    ("4104", "-1012000"),
]

# ---------------------------------------------------------------- 12 个月业务
# (租金不含税, 管理费不含税, 保洁绿化成本, 水电成本, 工资, 办公差旅, 代收水电)
MONTHS = [
    # 租金     管理费    保洁外包  水电能耗  工资    办公   代收水电
    (148000,  96000,  66000,  22000,  86000,  12000,  45000),
    (152000,  98000,  68000,  24000,  86000,  13500,  48000),
    (156000, 100000,  70000,  21000,  86000,  11200,  52000),
    (160000, 102000,  72000,  23000,  88000,  15800,  47000),
    (158000, 101000,  71000,  25000,  88000,  12600,  54000),
    (162000, 104000,  73000,  27000,  88000,  14100,  58000),
    (165000, 106000,  74000,  26000,  90000,  11900,  61000),
    (168000, 108000,  76000,  25000,  90000,  16500,  55000),
    (172000, 110000,  77000,  23000,  90000,  12300,  49000),
    (175000, 112000,  79000,  22000,  92000,  14900,  46000),
    (178000, 115000,  80000,  21000,  92000,  11700,  44000),
    (182000, 118000,  82000,  24000,  92000,  17300,  51000),
]
DEP = "20000"        # 月折旧：240 万固定资产按 10 年
CASH_FLOAT = "15000" # 每月提取备用金          # 月折旧：240 万固定资产按 10 年
VAT_RENT = D("0.09")   # 租金 9%
VAT_MF = D("0.06")     # 管理费 6%
LAST_OPEN = 12         # 这一月留着不结账，给上手演示用


# ---------------------------------------------------------------- 工具


def money(v) -> Decimal:
    return D(str(v))


def wipe_book(db, book_id: int) -> None:
    """删掉一个账套的全部数据。

    两道闸门都得**按规矩过**，不能关掉触发器硬删：
      1. ``trg_voucher_delete_guard`` 拦已结账期间的删除 → 先解锁期间
      2. ``trg_posted_voucher_guard`` 拦已记账凭证的删除     → 先反记账

    反记账（POSTED → UNPOSTED）是触发器明确放行的唯一状态跳转，
    走的是业务上的那条路，而不是抄近道。分录随凭证级联删除，不用单独处理。
    """
    db.execute(Period.__table__.update()
               .where(Period.book_id == book_id).values(locked=False))
    db.execute(Voucher.__table__.update()
               .where(Voucher.book_id == book_id, Voucher.status == "POSTED")
               .values(status="UNPOSTED"))
    db.execute(Voucher.__table__.delete().where(Voucher.book_id == book_id))
    db.execute(PeriodOpening.__table__.delete().where(PeriodOpening.book_id == book_id))
    db.execute(Period.__table__.delete().where(Period.book_id == book_id))
    db.execute(Subject.__table__.delete().where(Subject.book_id == book_id))
    db.execute(UserBook.__table__.delete().where(UserBook.book_id == book_id))
    db.execute(Book.__table__.delete().where(Book.id == book_id))
    db.commit()


def clean_test_books(db) -> tuple[int, int]:
    """清掉历史测试脚本留下的账套，以及因此变成空壳的公司。

    返回 (账套数, 空壳公司数)。名字必须匹配已知模式才动手，避免误删真实账套。
    """
    import re

    # 开户测试/设计器E2E 是浏览器端 e2e 建的账套与报表模板，同样属于测试残留
    pat = re.compile(r"^(租金账套|管理费账套|模块测试账套|开户测试)")
    ids = [b.id for b in db.scalars(select(Book)).all() if pat.match(b.name)]
    for bid in ids:
        wipe_book(db, bid)
    db.flush()

    # e2e 每跑一次就留下一张「设计器E2Exxxx」报表模板，同样清掉
    junk_tpl = db.scalars(select(ReportTemplate).where(
        ReportTemplate.name.like("设计器E2E%"))).all()
    for t in junk_tpl:
        db.delete(t)          # 模板下的行/列/单元格随外键 CASCADE 一起走
    db.flush()
    print(f"已清理 {len(junk_tpl)} 张 e2e 报表模板")

    db.commit()
    return len(ids)


def drop_demo(db) -> None:
    for b in db.scalars(select(Book).where(Book.name.like("启明物业%"))).all():
        wipe_book(db, b.id)


def build(db, user: SysUser) -> Book:
    # start_year 是这套账的**起始**年度（年度结转只往后追加，不新建账套）
    book = Book(company_name="启明物业管理有限公司", name=BOOK_NAME,
                start_year=YEAR, start_month=1)
    db.add(book)
    db.flush()
    create_periods(db, book)
    install_chart(db, book.id)
    db.add(UserBook(user_id=user.id, book_id=book.id, role="OWNER"))
    db.commit()

    S = {s.code: s.id for s in db.scalars(select(Subject).where(Subject.book_id == book.id)).all()}
    periods = {p.month: p for p in
               db.scalars(select(Period).where(Period.book_id == book.id)).all()}

    for code, amt in OPENINGS:
        db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                             subject_id=S[code], amount=money(amt)))
    db.commit()

    def voucher(d: date, entries: list[tuple[str, str, str, str]], remark: str, src="MANUAL"):
        """entries: (科目编码, 借贷, 金额, 摘要)"""
        payload = [
            {"subject_id": S[c], "direction": dirn, "amount": amt, "summary": s}
            for c, dirn, amt, s in entries
        ]
        v = create_voucher(db, book_id=book.id, user=user, voucher_date=d,
                           entries=payload, remark=remark, source=src)
        post_vouchers(db, [v.id], user)
        return v

    for idx, (rent, mgmt, clean, util, salary, office, collect) in enumerate(MONTHS, start=1):
        m = idx
        day = lambda dd: date(YEAR, m, dd)

        rent_vat = money(rent) * VAT_RENT
        mgmt_vat = money(mgmt) * VAT_MF
        # 1. 收租金：85% 走银行，15% 挂应收（次月收回应收账款）
        cash_rent = money(rent) * D("0.85") + rent_vat * D("0.85")
        ac_rent = money(rent) * D("0.15") + rent_vat * D("0.15")
        voucher(day(3), [
            ("100201", "DEBIT", str(cash_rent), f"收到{m}月租金"),
            ("600101", "CREDIT", str(money(rent) * D("0.85")), f"{m}月租金收入"),
            ("2221", "CREDIT", str(rent_vat * D("0.85")), "销项税额"),
            ("1122", "DEBIT", str(ac_rent), f"应收{m}月租金"),
            ("600101", "CREDIT", str(money(rent) * D("0.15")), f"{m}月租金收入"),
            ("2221", "CREDIT", str(rent_vat * D("0.15")), "销项税额"),
        ], f"{m}月 租金收入")

        # 2. 收回上月挂账的应收账款（第 1 个月没有上月可收）
        if idx >= 2:
            prev_rent = MONTHS[idx - 2][0]
            prev_vat = money(prev_rent) * VAT_RENT * D("0.15")
            recv = money(prev_rent) * D("0.15") + prev_vat
            voucher(day(5), [
                ("100201", "DEBIT", str(recv), "收回应收租金"),
                ("1122", "CREDIT", str(recv), "收回应收租金"),
            ], "收回应收租金")

        # 3. 物业管理费收入
        voucher(day(6), [
            ("100201", "DEBIT", str(money(mgmt) + mgmt_vat), f"收到{m}月物业管理费"),
            ("600102", "CREDIT", str(money(mgmt)), f"{m}月物业管理费收入"),
            ("2221", "CREDIT", str(mgmt_vat), "销项税额"),
        ], f"{m}月 物业管理费收入")

        # 4. 代收业主水电（代收代付，不进损益）
        voucher(day(8), [
            ("100201", "DEBIT", str(money(collect) * D("1.06")), f"代收{m}月水电费"),
            ("2241", "CREDIT", str(money(collect) * D("1.06")), "代收水电费"),
        ], f"{m}月 代收水电费")

        # 5. 结清代收水电
        voucher(day(9), [
            ("2241", "DEBIT", str(money(collect) * D("1.06")), "支付代收水电费"),
            ("100201", "CREDIT", str(money(collect) * D("1.06")), "支付代收水电费"),
        ], f"{m}月 结清代收水电")

        # 12. 缴纳上期增值税及附加
        prev_vat2 = (money(MONTHS[idx - 2][0]) * VAT_RENT * D("1.0")
                     + money(MONTHS[idx - 2][1]) * VAT_MF) if idx >= 2 else money(rent) * VAT_RENT
        surcharge = money(prev_vat2) * D("0.12")
        if idx >= 2:
            voucher(day(10), [
                ("2221", "DEBIT", str(prev_vat2), f"缴纳{m - 1}月增值税"),
                ("100201", "CREDIT", str(prev_vat2), f"缴纳{m - 1}月增值税"),
                ("6403", "DEBIT", str(surcharge), f"{m - 1}月城建税及附加"),
                ("2221", "CREDIT", str(surcharge), f"{m - 1}月城建税及附加"),
            ], "缴纳税费")

        # 6. 保洁绿化外包
        voucher(day(12), [
            ("640101", "DEBIT", str(clean), f"{m}月保洁绿化支出"),
            ("100201", "CREDIT", str(clean), f"{m}月保洁绿化支出"),
        ], f"{m}月 保洁绿化")

        # 7. 小区水电能耗成本
        voucher(day(13), [
            ("640101", "DEBIT", str(util), f"{m}月水电能耗"),
            ("2202", "CREDIT", str(util), f"{m}月水电能耗"),
        ], f"{m}月 水电能耗")

        # 10.6 支付上月挂账的水电能耗（形成应付账款 → 次月付款的滚动）
        if idx >= 2:
            prev_util = MONTHS[idx - 2][3]
            voucher(day(14), [
                ("2202", "DEBIT", str(prev_util), f"支付{m - 1}月水电能耗"),
                ("100201", "CREDIT", str(prev_util), f"支付{m - 1}月水电能耗"),
            ], f"支付{m - 1}月水电能耗")

        # 8. 计提工资
        voucher(day(15), [
            ("6602", "DEBIT", str(salary), f"{m}月职工薪酬"),
            ("2211", "CREDIT", str(salary), f"{m}月职工薪酬"),
        ], f"{m}月 计提工资")

        # 10. 办公及差旅（现金支付，所以要先有备用金，见 10.5）
        voucher(day(18), [
            ("6602", "DEBIT", str(office), f"{m}月办公差旅"),
            ("1001", "CREDIT", str(office), f"{m}月办公差旅"),
        ], f"{m}月 办公差旅")

        # 10.5 银行提取备用金（否则库存现金会被办公费支成负数）
        voucher(day(18), [
            ("1001", "DEBIT", CASH_FLOAT, "提取备用金"),
            ("100201", "CREDIT", str(CASH_FLOAT), "提取备用金"),
        ], f"{m}月 提取备用金")

        # 9. 发放工资
        voucher(day(20), [
            ("2211", "DEBIT", str(salary), f"{m}月发放工资"),
            ("100201", "CREDIT", str(salary), f"{m}月发放工资"),
        ], f"{m}月 发放工资")

        # 13. 银行手续费与利息
        voucher(day(21), [
            ("6603", "DEBIT", "186", "银行手续费"),
            ("100201", "CREDIT", "186", "银行手续费"),
            ("100201", "DEBIT", "980", "银行存款利息"),
            ("6111", "CREDIT", "980", "银行存款利息"),
        ], "银行结算")

        print(f"  {YEAR}-{m:02d} 业务凭证录入完成")
        # 11. 计提折旧
        voucher(day(25), [
            ("6602", "DEBIT", DEP, f"{m}月计提折旧"),
            ("1602", "CREDIT", DEP, f"{m}月计提折旧"),
        ], f"{m}月 计提折旧")


        # 月末：结转损益 + 结账（12 月故意留着）
        if m != LAST_OPEN:
            r = carry_forward_profit(db, book.id, periods[m], user)
            post_vouchers(db, [
                v.id for v in db.scalars(
                    select(Voucher).where(Voucher.book_id == book.id,
                                          Voucher.source == "CARRY_FORWARD",
                                          Voucher.period_id == periods[m].id)).all()
            ], user)
            close_period(db, periods[m], user)
            print(f"  {YEAR}-{m:02d} 结转损益 {len(r['vouchers'])} 张 → 已结账")

    # 下一年也开好 12 个空期间。一套真在用的账，跨年前就会把下一年准备好，
    # 人第二天上班直接就能在新年里记账，不必先去设置里点一次「新增年度」。
    #
    # 走 ensure_periods 而不是 year_end_carry：年度结转要求全年都结账，
    # 而 12 月是特意留着不结账的（留给上手试结转/结账）。
    # 这也不影响账 —— 期初是**推导值**（期初 P = 期末 P-1），
    # 2027 的期初会自动等于 2026 的期末，不存在快照要搬。
    ensure_periods(db, book, YEAR + 1)
    db.commit()

    db.refresh(book)
    return book


# ---------------------------------------------------------------- 自检


def verify(db, book: Book) -> bool:
    eng = LedgerEngine(db, book.id)
    # **只取本年**：账套现在跨了两年（2026 的账 + 2027 的空期间），
    # 还按 p.month 建字典的话 2027-01 会把 2026-01 顶掉，自检就成了对着空账本念经
    periods = {p.month: p for p in
               db.scalars(select(Period).where(Period.book_id == book.id,
                                                Period.year == YEAR)).all()}
    tree = queries.subject_tree(db, book.id)
    ok = True

    print("\n=== 自检 ===")
    for m in (1, 6, 12):
        rows, check = queries.balance_sheet_of_accounts(db, book.id, tree, periods[m], False)
        good = check["period_balanced"] and check["closing_balanced"] and check["opening_balanced"]
        ok &= good
        print(f"  {YEAR}-{m:02d} 试算平衡 {'✓' if good else '✗'}  "
              f"借={check['total_debit']:,} 贷={check['total_credit']:,}")

    # 资产负债表勾稽：资产 = 负债 + 权益 + 本年利润 + 期末未结转损益
    # 注意 12 月故意没结转，损益类科目（category=P）还有余额，
    # 它不属于资产/负债/权益任何一类，必须单独算进去，否则差一个月的损益。
    dec = periods[12]
    S = {s.code: s for s in db.scalars(select(Subject).where(Subject.book_id == book.id)).all()}
    cl = eng.compute([s.id for s in S.values()], dec.ord)
    leaves = [s for s in S.values() if s.is_leaf]

    def total(cat):
        return sum((cl[s.id].closing for s in leaves if s.category == cat), D(0))

    assets, liab, equity, pl = total("ASSET"), total("LIABILITY"), total("EQUITY"), total("PROFIT")
    profit_acc = cl[S["4103"].id].closing      # 1~11 月已结转到本年利润
    tie = assets + liab + equity + pl == 0
    ok &= tie

    print(f"  {'资产总计':<24}{assets:>16,}")
    print(f"  {'负债合计':<24}{-liab:>16,}")
    print(f"  {'所有者权益合计':<22}{-equity:>16,}")
    print(f"  {'  其中 本年利润(1~11月已结转)':<22}{-profit_acc:>16,}")
    print(f"  {'加 期末未结转损益(12月)':<24}{-pl:>16,}")
    rhs = -liab + -equity + -pl
    print(f"  勾稽 资产({assets:,}) = 负债+权益+期末损益({rhs:,})  "
          f"{'✓' if assets == rhs else '✗ 差额 ' + str(assets - rhs)}")

    n_v = db.scalar(select(func.count()).select_from(Voucher).where(Voucher.book_id == book.id))
    n_cf = db.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD"))
    n_lock = db.scalar(select(func.count()).select_from(Period).where(
        Period.book_id == book.id, Period.locked.is_(True)))
    print(f"  凭证 {n_v} 张（其中结转 {n_cf} 张），已结账 {n_lock} / 12 期")
    return ok


def main() -> int:
    db = SessionLocal()
    try:
        user = db.scalar(select(SysUser).where(SysUser.username == "admin"))
        if user is None:
            print("找不到 admin 用户，先跑一次 init_database")
            return 1

        if "--clean" in sys.argv:
            n_book = clean_test_books(db)
            print(f"已清理 {n_book} 个历史测试账套")
            return 0

        if "--reset" in sys.argv or db.scalar(select(Book).where(Book.name == BOOK_NAME)):
            drop_demo(db)
            print("已删除旧演示账套")

        print(f"生成演示账套《{BOOK_NAME}》…")
        book = build(db, user)
        print(f"\n账套 id = {book.id}")
        return 0 if verify(db, book) else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
