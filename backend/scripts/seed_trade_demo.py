#!/usr/bin/env python3
"""生成一套**商贸类**企业的演示账套（用于对外演示与截图）。

    .venv/bin/python scripts/seed_trade_demo.py          # 生成
    .venv/bin/python scripts/seed_trade_demo.py --reset  # 先删掉旧的再生成

场景：宏发商贸有限公司 2026 年度。
    1~11 月 —— 已结转损益、已结账
    12 月    —— 业务已录入但未结转未结账（留两张未记账凭证，页面上能同时看到两种状态）

业务形态：食品 / 饮料 / 日用百货的批发兼零售。
    · 采购：借 库存商品 + 进项税额，贷 应付账款（次月付 80%，再下月付 20%）
    · 销售：借 银行存款 / 支付宝 / 应收账款，贷 主营业务收入 + 销项税额
    · 成本：月末把销售成本从 库存商品 结转到 主营业务成本
    · 税金：每月结转应交增值税、按季缴纳城建税与教育费附加
    · 费用：运费、业务招待费、房租、水电、工资、财务手续费

金额全部写死（不用随机），每次生成的数据完全一致，便于对着核。
"""
from __future__ import annotations

import sys
import calendar
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.accounting.periods import (  # noqa: E402
    carry_forward_profit, close_period, create_periods, ensure_periods,
)
from app.accounting.vouchers import create_voucher, post_vouchers  # noqa: E402
from app.accounting.years import copy_chart, ensure_book_year  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.ledger import queries  # noqa: E402
from app.ledger.engine import LedgerEngine  # noqa: E402
from app.models import (  # noqa: E402
    Book, Entry, Period, PeriodOpening, ReportTemplate, Subject, SysUser,
    UserBook, Voucher,
)
from app.seed.chart_of_accounts import install_chart  # noqa: E402

D = Decimal
BOOK_NAME = "宏发商贸"
COMPANY = "宏发商贸有限公司"
YEAR = 2026
LAST_OPEN = 12          # 这一月留着不结账
VAT = D("0.13")         # 一般纳税人：货物销售 13%
SURCHARGE = D("0.10")   # 城建税 7% + 教育费附加 3%

# ---------------------------------------------------------------- 期初余额
# 借方合计 3,188,000 = 贷方合计 3,188,000（贷方类按借正贷负存负数）
#
# 应交税费挂的是**末级** 222102 未交增值税，不是 2221 —— 2221 有下级科目，
# 期初挂在上级上会与子级重复计入，借贷当场就不平。
OPENINGS = [
    ("1001", "26000"), ("100201", "862000"), ("100202", "238000"),
    ("101201", "63000"), ("1122", "524000"),
    ("140501", "676000"), ("140502", "318000"), ("140503", "481000"),
    ("2202", "-520000"), ("222102", "-84000"),
    ("3001", "-2000000"), ("310415", "-584000"),
]

# ---------------------------------------------------------------- 二级明细科目
# 标准科目表里的 1002 / 1012 / 1405 是空的，商贸企业要按银行账户、收款渠道、
# 商品品类拆开，否则「银行存款」永远只有一个数，余额表看着不像真账。

#: 期初挂着的应收 / 应付。第 2 个月随第一批货款、货款回款一起清掉，
#: 否则这笔钱会在账上一直躺着，年末看着像一笔永远收不回来的款。
OPENING_AR = D("524000")
OPENING_AP = D("520000")
SUBSUBJECTS = [
    ("1002", "100201", "银行存款-基本户（工商银行）", "DEBIT", "ASSET"),
    ("1002", "100202", "银行存款-一般户（建设银行）", "DEBIT", "ASSET"),
    ("1012", "101201", "其他货币资金-支付宝", "DEBIT", "ASSET"),
    ("1012", "101202", "其他货币资金-微信支付", "DEBIT", "ASSET"),
    ("1405", "140501", "库存商品-食品", "DEBIT", "ASSET"),
    ("1405", "140502", "库存商品-饮料", "DEBIT", "ASSET"),
    ("1405", "140503", "库存商品-日用百货", "DEBIT", "ASSET"),
]


def money(v) -> Decimal:
    return D(str(v))


def wipe_book(db, book_id: int) -> None:
    """删掉一个账套的全部数据（两道触发器护栏都得按规矩过）。"""
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


def drop_demo(db) -> None:
    for b in db.scalars(select(Book).where(Book.name.like(f"{BOOK_NAME}%"))).all():
        wipe_book(db, b.id)


def build(db, user: SysUser) -> Book:
    book = Book(company_name=COMPANY, name=BOOK_NAME,
                start_year=YEAR, start_month=1)
    db.add(book)
    db.flush()
    create_periods(db, book)
    install_chart(db, book.id, YEAR)

    book_year = ensure_book_year(db, book, YEAR)
    book_year.init_subjects_at = datetime.now(timezone.utc)
    book_year.init_subjects_source = "CHART"
    book_year.init_openings_at = datetime.now(timezone.utc)
    db.add(UserBook(user_id=user.id, book_id=book.id, role="OWNER"))
    db.commit()

    # ---- 二级明细科目 ----
    base = {s.code: s for s in db.scalars(select(Subject).where(
        Subject.book_id == book.id, Subject.year == YEAR)).all()}
    for pcode, code, name, direction, category in SUBSUBJECTS:
        parent = base[pcode]
        parent.is_leaf = False
        db.add(Subject(
            book_id=book.id, year=YEAR, code=code, name=name,
            parent_id=parent.id, level=parent.level + 1,
            category=category, direction=direction, is_leaf=True,
            carry_forward=False, sort_no=parent.sort_no,
        ))
    # 进项税额在会计上就是**借方余额**的科目；标准科目表为了整棵应交税费
    # 都是贷方而把它标成了贷，这里拨回来 —— 否则科目表上写着「贷」、
    # 余额表上却显示借方余额，同一个科目两个方向。
    base["22210101"].direction = "DEBIT"
    db.commit()

    S = {s.code: s.id for s in db.scalars(select(Subject).where(
        Subject.book_id == book.id, Subject.year == YEAR)).all()}
    periods = {p.month: p for p in
               db.scalars(select(Period).where(Period.book_id == book.id)).all()}

    for code, amt in OPENINGS:
        db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                             subject_id=S[code], amount=money(amt)))
    db.commit()

    def voucher(d: date, entries, remark: str, src="MANUAL", post=True):
        payload = [{"subject_id": S[c], "direction": dirn, "amount": amt, "summary": s}
                   for c, dirn, amt, s in entries]
        v = create_voucher(db, book_id=book.id, user=user, voucher_date=d,
                           entries=payload, remark=remark, source=src)
        if post:
            post_vouchers(db, book.id, [v.id], user)
        return v

    # 累计量，用来在次月付上一月的货款
    prev_ap: list[Decimal] = [Decimal(0)] * 13     # index m = 第 m 月应付合计
    prev_ar: list[Decimal] = [Decimal(0)] * 13     # index m = 第 m 月挂账应收
    tax_acc = {"222102": Decimal(0), "222108": Decimal(0), "222113": Decimal(0)}

    for m in range(1, 13):
        # 夹到当月最后一天：2 月没有 30 号，写死 day(30) 会 ValueError。
        last_day = calendar.monthrange(YEAR, m)[1]
        day = lambda dd: date(YEAR, m, min(dd, last_day))

        # ---- 本月的量 ----
        sales = money(1_420_000 + 38_000 * (m - 1))          # 不含税收入
        vat_out = (sales * VAT).quantize(D("0.01"))
        gross = sales + vat_out
        cash_part = (gross * D("0.70")).quantize(D("0.01"))  # 七成现收
        ar_part = gross - cash_part                          # 三成挂应收
        to_bank = (cash_part * D("0.85")).quantize(D("0.01"))
        to_alipay = cash_part - to_bank

        cost_food = (sales * D("0.52")).quantize(D("0.01"))
        cost_drink = (sales * D("0.16")).quantize(D("0.01"))
        cost_daily = (sales * D("0.07")).quantize(D("0.01"))
        cost_total = cost_food + cost_drink + cost_daily

        # 每三个月的季末备一次货，其余月份进销平
        build_up = D("0.03") if m % 3 == 0 else D("0")
        p_food = (cost_food * (D(1) + build_up)).quantize(D("0.01"))
        p_drink = (cost_drink * (D(1) + build_up)).quantize(D("0.01"))
        p_daily = (cost_daily * (D(1) + build_up)).quantize(D("0.01"))
        p_total = p_food + p_drink + p_daily
        vat_in = (p_total * VAT).quantize(D("0.01"))
        ap_now = p_total + vat_in

        freight = money(26_000 + 500 * (m - 1))
        entertain = money(6_400 + 300 * (m - 1))
        rent = money(45_000)
        utility = money(9_200 + 400 * (m - 1))
        salary_sell = money(42_000 + 300 * (m - 1))
        salary_admin = money(38_000 + 300 * (m - 1))
        salary_total = salary_sell + salary_admin
        office = money(4_200)
        fee = money(620 + 20 * (m - 1))
        float_cash = money(8_000)

        # 0. 按季缴纳税费（1、4、7、10 月各缴一次上一季度）
        # **必须排在当月其他凭证之前**：凭证号按创建顺序发，而「整理凭证」的
        # 时序检查要求凭证号与日期同序。月初缴税的日期比当月其他凭证早，
        # 那它就得先拿到号 —— 否则会出现「记0017 是 4-30、记0018 却是 4-02」。
        if m % 3 == 1 and m > 1:
            total = tax_acc["222102"] + tax_acc["222108"] + tax_acc["222113"]
            if total > 0:
                voucher(day(2), [
                    ("222102", "DEBIT", str(tax_acc["222102"]), "缴纳增值税"),
                    ("222108", "DEBIT", str(tax_acc["222108"]), "缴纳城建税"),
                    ("222113", "DEBIT", str(tax_acc["222113"]), "缴纳教育费附加"),
                    ("100201", "CREDIT", str(total), "缴纳税费"),
                ], "缴纳上一季度税费")
                for k in tax_acc:
                    tax_acc[k] = Decimal(0)

        # 1. 采购入库：挂应付账款
        voucher(day(4), [
            ("140501", "DEBIT", str(p_food), f"采购食品 {m}月"),
            ("140502", "DEBIT", str(p_drink), f"采购饮料 {m}月"),
            ("140503", "DEBIT", str(p_daily), f"采购日用百货 {m}月"),
            ("22210101", "DEBIT", str(vat_in), f"采购进项税额 {m}月"),
            ("2202", "CREDIT", str(ap_now), f"应付供应商货款 {m}月"),
        ], f"{m}月 采购入库")

        # 2. 销售出库并收款
        voucher(day(8), [
            ("100201", "DEBIT", str(to_bank), f"销售收款 {m}月"),
            ("101201", "DEBIT", str(to_alipay), f"支付宝收款 {m}月"),
            ("1122", "DEBIT", str(ar_part), f"销售挂账 {m}月"),
            ("5001", "CREDIT", str(sales), f"主营业务收入 {m}月"),
            ("22210106", "CREDIT", str(vat_out), f"销项税额 {m}月"),
        ], f"{m}月 销售出库")

        # 3. 收回上月挂账的应收账款
        #    第 2 个月连**期初**那笔一起收 —— 否则期初挂的 524,000 会在账上
        #    一直挂着，年末看着像一笔永远收不回来的款。
        if m == 2:
            recv = prev_ar[1] + OPENING_AR
            voucher(day(12), [
                ("100201", "DEBIT", str(recv), "收回应收账款"),
                ("1122", "CREDIT", str(recv), "收回应收账款"),
            ], f"{m}月 收回应收账款")
        elif m >= 3:
            voucher(day(12), [
                ("100201", "DEBIT", str(prev_ar[m - 1]), f"收回应收账款 {m}月"),
                ("1122", "CREDIT", str(prev_ar[m - 1]), f"收回应收账款 {m}月"),
            ], f"{m}月 收回应收账款")

        # 4. 支付上月货款（第 2 个月连期初那笔应付一起付）
        if m == 2:
            pay = prev_ap[1] + OPENING_AP
            voucher(day(15), [
                ("2202", "DEBIT", str(pay), f"支付货款 {m}月"),
                ("100201", "CREDIT", str(pay), f"支付货款 {m}月"),
            ], f"{m}月 支付货款")
        elif m >= 3:
            voucher(day(15), [
                ("2202", "DEBIT", str(prev_ap[m - 1]), f"支付货款 {m}月"),
                ("100201", "CREDIT", str(prev_ap[m - 1]), f"支付货款 {m}月"),
            ], f"{m}月 支付货款")

        # 5. 结转销售成本
        voucher(day(20), [
            ("5401", "DEBIT", str(cost_food), f"结转食品成本 {m}月"),
            ("5401", "DEBIT", str(cost_drink), f"结转饮料成本 {m}月"),
            ("5401", "DEBIT", str(cost_daily), f"结转百货成本 {m}月"),
            ("140501", "CREDIT", str(cost_food), f"结转食品成本 {m}月"),
            ("140502", "CREDIT", str(cost_drink), f"结转饮料成本 {m}月"),
            ("140503", "CREDIT", str(cost_daily), f"结转百货成本 {m}月"),
        ], f"{m}月 结转销售成本")

        # 6. 计提工资
        voucher(day(22), [
            ("560107", "DEBIT", str(salary_sell), f"计提销售人员工资 {m}月"),
            ("560209", "DEBIT", str(salary_admin), f"计提管理人员工资 {m}月"),
            ("221101", "CREDIT", str(salary_total), f"计提职工薪酬 {m}月"),
        ], f"{m}月 计提工资")

        # 7. 发放工资
        voucher(day(25), [
            ("221101", "DEBIT", str(salary_total), f"发放工资 {m}月"),
            ("100201", "CREDIT", str(salary_total), f"发放工资 {m}月"),
        ], f"{m}月 发放工资")

        # 8~11. 各项付现费用
        voucher(day(26), [
            ("560104", "DEBIT", str(freight), f"运费 {m}月"),
            ("100201", "CREDIT", str(freight), f"运费 {m}月"),
        ], f"{m}月 支付运费")
        voucher(day(26), [
            ("560106", "DEBIT", str(entertain), f"业务招待费 {m}月"),
            ("100201", "CREDIT", str(entertain), f"业务招待费 {m}月"),
        ], f"{m}月 业务招待费")
        voucher(day(27), [
            ("560207", "DEBIT", str(rent), f"门店房租 {m}月"),
            ("100201", "CREDIT", str(rent), f"门店房租 {m}月"),
        ], f"{m}月 支付房租")
        voucher(day(27), [
            ("560206", "DEBIT", str(utility), f"水电费 {m}月"),
            ("100201", "CREDIT", str(utility), f"水电费 {m}月"),
        ], f"{m}月 支付水电费")

        # 12. 提取备用金 + 办公费（现金）
        voucher(day(27), [
            ("1001", "DEBIT", str(float_cash), "提取备用金"),
            ("100201", "CREDIT", str(float_cash), "提取备用金"),
        ], f"{m}月 提取备用金")
        # 12 月这张留成**未记账**：页面上要能同时看到「已记账」和「未记账」两种状态。
        voucher(day(27), [
            ("560204", "DEBIT", str(office), f"办公交通费 {m}月"),
            ("1001", "CREDIT", str(office), f"办公交通费 {m}月"),
        ], f"{m}月 办公交通费", post=(m != LAST_OPEN))

        # 13. 银行手续费
        voucher(day(28), [
            ("560302", "DEBIT", str(fee), f"银行手续费 {m}月"),
            ("100201", "CREDIT", str(fee), f"银行手续费 {m}月"),
        ], f"{m}月 银行手续费")

        # 14. 月末结转应交增值税
        net_vat = vat_out - vat_in
        voucher(day(30), [
            ("22210106", "DEBIT", str(vat_out), f"结转销项税额 {m}月"),
            ("22210101", "CREDIT", str(vat_in), f"结转进项税额 {m}月"),
            ("222102", "CREDIT", str(net_vat), f"结转未交增值税 {m}月"),
        ], f"{m}月 结转应交增值税")

        # 15. 计提城建税与教育费附加
        surcharge = (net_vat * SURCHARGE).quantize(D("0.01"))
        if surcharge > 0:
            voucher(day(30), [
                ("5403", "DEBIT", str(surcharge), f"计提税金及附加 {m}月"),
                ("222108", "CREDIT", str((surcharge * D("0.7")).quantize(D("0.01"))),
                 f"计提城建税 {m}月"),
                ("222113", "CREDIT", str((surcharge * D("0.3")).quantize(D("0.01"))),
                 f"计提教育费附加 {m}月"),
            ], f"{m}月 计提税金及附加")
            tax_acc["222102"] += net_vat
            tax_acc["222108"] += (surcharge * D("0.7")).quantize(D("0.01"))
            tax_acc["222113"] += (surcharge * D("0.3")).quantize(D("0.01"))

        prev_ap[m] = ap_now
        prev_ar[m] = ar_part
        print(f"  {YEAR}-{m:02d} 业务凭证录入完成")

        # 16. 月末：结转损益 + 结账（12 月故意留着）
        if m != LAST_OPEN:
            r = carry_forward_profit(db, book.id, periods[m], user)
            post_vouchers(db, book.id, [
                v.id for v in db.scalars(
                    select(Voucher).where(Voucher.book_id == book.id,
                                          Voucher.source == "CARRY_FORWARD",
                                          Voucher.period_id == periods[m].id)).all()
            ], user)
            close_period(db, periods[m], user)
            print(f"  {YEAR}-{m:02d} 结转损益 {len(r['vouchers'])} 张 → 已结账")

    # ---------------------------------------------------------------- 2027 年 1 月
    # 跨年补录的一段「还没整理过」的账：凭证号跳了一个（记0004 不存在），
    # 日期也不是按顺序录的（记0005 是 1 月 8 日，却排在 1 月 20 日那张后面）。
    #
    # 2026 年的账是干净的 —— 那一年要拿去演示，不能有断号。
    # 「整理凭证」那个界面需要这种场景才看得出它在做什么，所以单独放在下一年。
    ensure_periods(db, book, YEAR + 1)
    copy_chart(db, book.id, YEAR, YEAR + 1)
    db.commit()

    # 2027 年的初始化状态：第 1 步（定科目）copy_chart 已经标好了；
    # 第 2 步（期初）这一年的期初本来就是空的 —— 这段补录的账只为了给
    # 「整理凭证」那个界面准备，不承接上年余额。按系统自带的「本期没有期初
    # 余额，直接开始记账」这条出口标完成。
    #
    # **不标的后果**：账套卡片上的初始化状态按「最近一期有凭证的年度」算，
    # 2027 年有凭证 → 卡片按 2027 年报 → 2027 年没标 → 卡片上挂一个
    # 「待初始化」。而这一年明明已经在记账了，截图上会自相矛盾。
    from app.accounting.initialization import (OPENINGS_SOURCE_NONE,
                                               mark_openings_ready)
    mark_openings_ready(db, ensure_book_year(db, book, YEAR + 1),
                        OPENINGS_SOURCE_NONE)
    db.commit()

    S27 = {s.code: s.id for s in db.scalars(select(Subject).where(
        Subject.book_id == book.id, Subject.year == YEAR + 1)).all()}

    def voucher27(d: date, entries, remark: str, no: str):
        payload = [{"subject_id": S27[c], "direction": dirn, "amount": amt, "summary": s}
                   for c, dirn, amt, s in entries]
        create_voucher(db, book_id=book.id, user=user, voucher_date=d,
                       entries=payload, remark=remark, source="MANUAL",
                       voucher_no=no, commit=False)

    voucher27(date(YEAR + 1, 1, 5), [
        ("140501", "DEBIT", "186000", "采购食品"),
        ("22210101", "DEBIT", "24180", "采购进项税额"),
        ("2202", "CREDIT", "210180", "应付供应商货款"),
    ], "1月 采购入库", "记0001")
    voucher27(date(YEAR + 1, 1, 12), [
        ("100201", "DEBIT", "840000", "销售收款"),
        ("5001", "CREDIT", "743362", "主营业务收入"),
        ("22210106", "CREDIT", "96638", "销项税额"),
    ], "1月 销售出库", "记0002")
    voucher27(date(YEAR + 1, 1, 20), [
        ("2202", "DEBIT", "168144", "支付货款"),
        ("100201", "CREDIT", "168144", "支付货款"),
    ], "1月 支付货款", "记0003")
    # 记0004 不存在 —— 断号就是从这里来的
    voucher27(date(YEAR + 1, 1, 8), [
        ("560107", "DEBIT", "42300", "计提销售人员工资"),
        ("221101", "CREDIT", "42300", "计提职工薪酬"),
    ], "1月 计提工资", "记0005")
    voucher27(date(YEAR + 1, 1, 28), [
        ("560207", "DEBIT", "45000", "门店房租"),
        ("100201", "CREDIT", "45000", "门店房租"),
    ], "1月 支付房租", "记0006")
    db.commit()
    print(f"  {YEAR + 1}-01 补录凭证 5 张（含 1 处断号、1 处时序倒置）")

    db.commit()
    db.refresh(book)
    return book


# ---------------------------------------------------------------- 自检


def verify(db, book: Book) -> bool:
    eng = LedgerEngine(db, book.id)
    periods = {p.month: p for p in
               db.scalars(select(Period).where(Period.book_id == book.id,
                                                Period.year == YEAR)).all()}
    tree = queries.subject_tree(db, book.id, YEAR)
    ok = True

    print("\n=== 自检 ===")
    for m in (1, 6, 12):
        rows, check = queries.balance_sheet_of_accounts(db, book.id, tree, periods[m], False)
        good = check["period_balanced"] and check["closing_balanced"] and check["opening_balanced"]
        ok &= good
        print(f"  {YEAR}-{m:02d} 试算平衡 {'✓' if good else '✗'}  "
              f"借={check['total_debit']:,} 贷={check['total_credit']:,}")

    dec = periods[12]
    S = {s.code: s for s in db.scalars(select(Subject).where(
        Subject.book_id == book.id, Subject.year == YEAR)).all()}
    cl = eng.compute([s.id for s in S.values()], dec.ord)
    leaves = [s for s in S.values() if s.is_leaf]

    def total(cat):
        return sum((cl[s.id].closing for s in leaves if s.category == cat), D(0))

    assets, liab, equity, pl = total("ASSET"), total("LIABILITY"), total("EQUITY"), total("PROFIT")
    profit_acc = cl[S["3103"].id].closing
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
    n_unposted = db.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.book_id == book.id, Voucher.status == "UNPOSTED"))
    print(f"  凭证 {n_v} 张（其中结转 {n_cf} 张，未记账 {n_unposted} 张），已结账 {n_lock} / 12 期")

    # 关键科目的余额看一眼，截图上要能看得出是商贸
    print("\n  关键科目期末余额：")
    for code in ("100201", "1122", "140501", "140502", "140503", "2202", "2221",
                 "5001", "5401", "5601", "5602", "5603"):
        if code in S:
            print(f"    {code:<8}{S[code].name:<18}{cl[S[code].id].closing:>18,}")
    return ok


def main() -> int:
    db = SessionLocal()
    try:
        user = db.scalar(select(SysUser).where(SysUser.username == "admin"))
        if user is None:
            print("找不到 admin 用户，先跑一次 init_database")
            return 1

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
