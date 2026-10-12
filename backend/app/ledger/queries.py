"""账簿查询：总账、明细账、科目余额表、试算平衡。

全部基于 app.ledger.engine 的派生量，不存在第二套余额计算。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.ledger.engine import ZERO, Figures, LedgerEngine, display_side
from app.models import Book, Entry, Period, Subject, Voucher
from app.models.book import book_first_ord

CATEGORY_ORDER = {"ASSET": 0, "LIABILITY": 1, "EQUITY": 2, "COST": 3, "PROFIT": 4}


def _entry_stmt(book_id: int, include_unposted: bool) -> Select:
    stmt = (
        select(Entry, Voucher, Subject)
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .join(Subject, Subject.id == Entry.subject_id)
        .where(Entry.book_id == book_id)
    )
    if not include_unposted:
        stmt = stmt.where(Voucher.status == "POSTED")
    return stmt


def subject_tree(db: Session, book_id: int) -> list[Subject]:
    """按会计科目表顺序返回所有科目（含非明细级、**含停用**）。

    刻意不过滤 ``active``：停用的语义是「不让再选它」，不是「删掉它」——
    老凭证还指着它，账还得算得出来、还得看得见。过滤掉的话，历史余额
    在界面上直接消失，用户会以为账丢了。

    写路径另有闸门（``accounting/vouchers.py`` 建凭证时校验科目可用），
    这里是只读展示，不该再拦一道。
    """
    return list(
        db.scalars(
            select(Subject)
            .where(Subject.book_id == book_id)
            .order_by(Subject.category, Subject.code)
        ).all()
    )


# --------------------------------------------------------------------- 明细账


def subject_of_book(db: Session, book_id: int, subject_id: int) -> Subject:
    """取**属于本账套**的科目，取不到就 404。

    以前这里是裸的 ``db.get(Subject, subject_id)``，不带 book_id 约束 ——
    同一个文件里 ``general`` 校验了期间归属，``detail`` 却不校验科目，
    两种口径。后果不是泄露金额（分录查询自带 Entry.book_id 过滤），
    而是别的账套的科目元数据（编码、名称、余额方向）会跨账套参与计算，
    页面看着正常、实际是空账，用户会以为这个科目真的一分钱没有。

    更糟的是传一个**不存在**的 id：``db.get`` 返回 None，
    后面 ``display_side(None, ...)`` 直接 AttributeError → 500。
    用户手输一个错 id 就能让接口报服务器错误，而不是「科目不存在」。

    这里不拦 ``is_leaf``：上级科目是合法 ID，点它的小计行应当看到
    空账而不是 404。末级性由调用方在需要时自己判断。
    """
    subject = db.get(Subject, subject_id)
    if subject is None or subject.book_id != book_id:
        raise HTTPException(404, "科目不存在")
    return subject


def detail_ledger(
    db: Session,
    book_id: int,
    subject_id: int,
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
) -> dict:
    """明细账：逐笔列出分录并滚动计算余额。"""
    eng = LedgerEngine(db, book_id, include_unposted)
    subject = subject_of_book(db, book_id, subject_id)
    rows = []

    # 区间期初 = 起始期间的期初
    opening_before = eng.opening(subject_id, from_period.ord)
    side, amount = display_side(subject, opening_before)
    running = opening_before
    rows.append(
        {
            "kind": "OPENING",
            "voucher_no": "期初余额",
            "voucher_date": None,
            "period": f"{from_period.year}-{from_period.month:02d}",
            "summary": "期初余额",
            "debit": ZERO,
            "credit": ZERO,
            "direction": side,
            "balance": amount,
        }
    )

    period_labels = {
        p.id: f"{p.year}-{p.month:02d}"
        for p in db.scalars(select(Period).where(Period.book_id == book_id)).all()
    }

    stmt = (
        _entry_stmt(book_id, include_unposted)
        .where(Entry.subject_id == subject_id)
        .where(Entry.period_ord >= from_period.ord, Entry.period_ord <= to_period.ord)
        .order_by(Voucher.voucher_date, Voucher.voucher_no, Entry.line_no)
    )
    dr_sum = cr_sum = ZERO
    for entry, voucher, _subj in db.execute(stmt).all():
        running += entry.amount if entry.direction == "DEBIT" else -entry.amount
        dr_sum += entry.amount if entry.direction == "DEBIT" else ZERO
        cr_sum += entry.amount if entry.direction == "CREDIT" else ZERO
        side, amount = display_side(subject, running)
        rows.append(
            {
                "kind": "ENTRY",
                "voucher_id": voucher.id,
                "voucher_no": voucher.voucher_no,
                "voucher_date": voucher.voucher_date.isoformat(),
                "period": period_labels.get(voucher.period_id, ""),
                "summary": entry.summary,
                "debit": entry.amount if entry.direction == "DEBIT" else ZERO,
                "credit": entry.amount if entry.direction == "CREDIT" else ZERO,
                "direction": side,
                "balance": amount,
                "status": voucher.status,
            }
        )

    side, amount = display_side(subject, running)
    rows.append(
        {
            "kind": "TOTAL",
            "voucher_no": "本期合计",
            "voucher_date": None,
            "period": "",
            "summary": "",
            "debit": dr_sum,
            "credit": cr_sum,
            "direction": side,
            "balance": amount,
        }
    )

    return {
        "subject": {"id": subject.id, "code": subject.code, "name": subject.name,
                    "direction": subject.direction, "category": subject.category},
        "from_period": f"{from_period.year}-{from_period.month:02d}",
        "to_period": f"{to_period.year}-{to_period.month:02d}",
        "rows": rows,
    }


# --------------------------------------------------------------------- 总账


def general_ledger(
    db: Session,
    book_id: int,
    subjects: list[Subject],
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
) -> list[dict]:
    """总账：每个科目一行，含区间期初、本期发生、累计发生、期末。"""
    eng = LedgerEngine(db, book_id, include_unposted)
    ids = [s.id for s in subjects]
    fmap = eng.compute(ids, to_period.ord)
    # 区间发生额单独算，不能拿本年累计相减：区间跨年时两边的累计口径不同
    pmap = eng.range_movements(ids, from_period.ord, to_period.ord)

    book = eng._book()
    has_prev = from_period.ord > book_first_ord(book)
    if has_prev:
        prev_map = eng.compute(ids, from_period.ord - 1)

    out = []
    for s in subjects:
        f = fmap[s.id]
        p_dr, p_cr = pmap[s.id]
        if has_prev:
            opening = prev_map[s.id].closing
        else:
            opening = f.opening

        closing = opening + p_dr - p_cr
        side, amount = display_side(s, closing)
        out.append(
            {
                "subject_id": s.id,
                "code": s.code,
                "name": s.name,
                "level": s.level,
                "category": s.category,
                "is_leaf": s.is_leaf,
                "opening_direction": display_side(s, opening)[0],
                "opening": display_side(s, opening)[1],
                "period_debit": p_dr,
                "period_credit": p_cr,
                "ytd_debit": f.dr_cum,
                "ytd_credit": f.cr_cum,
                "direction": side,
                "closing": amount,
            }
        )
    return out


# --------------------------------------------------------------------- 科目余额表 / 试算平衡


def balance_sheet_of_accounts(
    db: Session,
    book_id: int,
    subjects: list[Subject],
    period: Period,
    include_unposted: bool = False,
) -> tuple[list[dict], dict]:
    """科目余额表。返回 (行, 试算平衡校验结果)。

    试算平衡：
        Σ 借方发生额 == Σ 贷方发生额
        Σ 借方余额    == Σ 贷方余额
    """
    eng = LedgerEngine(db, book_id, include_unposted)
    fmap = eng.compute([s.id for s in subjects], period.ord)

    rows = []
    total_open_dr = total_open_cr = total_dr = total_cr = total_close_dr = total_close_cr = ZERO

    for s in subjects:
        f = fmap[s.id]
        o_side, o_amt = display_side(s, f.opening)
        c_side, c_amt = display_side(s, f.closing)
        rows.append(
            {
                "subject_id": s.id,
                "code": s.code,
                "name": s.name,
                "level": s.level,
                "category": s.category,
                "is_leaf": s.is_leaf,
                "opening_direction": o_side,
                "opening": o_amt,
                "period_debit": f.period_dr,
                "period_credit": f.period_cr,
                "ytd_debit": f.dr_cum,
                "ytd_credit": f.cr_cum,
                "direction": c_side,
                "closing": c_amt,
            }
        )
        # 只累计明细级科目，否则父级汇总行会重复计数
        if s.is_leaf:
            total_open_dr += f.opening if f.opening > 0 else ZERO
            total_open_cr += -f.opening if f.opening < 0 else ZERO
            total_dr += f.period_dr
            total_cr += f.period_cr
            total_close_dr += f.closing if f.closing > 0 else ZERO
            total_close_cr += -f.closing if f.closing < 0 else ZERO

    check = {
        "period_balanced": total_dr == total_cr,
        "closing_balanced": total_close_dr == total_close_cr,
        "opening_balanced": total_open_dr == total_open_cr,
        "total_debit": total_dr,
        "total_credit": total_cr,
        "total_closing_debit": total_close_dr,
        "total_closing_credit": total_close_cr,
    }
    return rows, check


# --------------------------------------------------------------------- 凭证录入页的科目余额


def voucher_subject_balances(
    db: Session,
    book_id: int,
    period: Period,
    exclude_voucher_id: int | None = None,
    include_unposted: bool = True,
) -> dict[int, str]:
    """凭证录入页辅助记账用的「除本张以外」科目余额：subject_id -> 期末余额(借正贷负)。

    口径与科目余额表**完全同源**（同一个 ``Figures.closing``），差别只有三处，
    每处都是为了「正在录入的这一张」不干扰自己：

    - ``exclude_voucher_id``：剔除正在编辑的那张凭证。它会在 0.7 秒后自动保存，
      算进去的话余额会跟着手上这张一起动 —— 你往 1001 里填 500，余额就 +500，
      看着像「我刚存的凭证已经进账了」，其实是自己在跟自己说话。
    - ``include_unposted=True``：辅助记账要看的是**这个人眼下记到哪儿了**。
      默认排除未记账的话，月底集中记账前余额全是空的，这个提示就没意义了。
    - 只回金额不回方向：调用方要的是「一个数」，符号本身已经说明落在哪一边
      （正=借方、负=贷方）。再拆成「方向 + 绝对值」是给表格看的，
      塞进一个格子里只会让人多读一遍。

    上级科目一并算出来（引擎本来就上卷）：凭证只挂在末级上，但排凭证时
    「1002 银行存款」这种非末级也可能临时选上去看一眼余额。
    """
    eng = LedgerEngine(db, book_id, include_unposted, exclude_voucher_id=exclude_voucher_id)
    fmap = eng.compute(up_to_ord=period.ord)
    return {sid: str(f.closing) for sid, f in fmap.items()}
