"""账簿：总账、明细账、科目余额表、试算平衡。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting import vouchers as svc
from app.ledger import queries
from app.core.db import get_db
from app.core.deps import get_my_book
from app.models import Book, Period, Voucher

router = APIRouter(prefix="/api/books/{book_id}/ledger", tags=["账簿"])


def _period(db: Session, book_id: int, period_id: int | None) -> Period:
    stmt = select(Period).where(Period.book_id == book_id)
    if period_id:
        p = db.scalar(stmt.where(Period.id == period_id))
    else:
        p = db.scalar(stmt.order_by(Period.ord.desc()).limit(1))
    if p is None:
        raise HTTPException(404, "会计期间不存在")
    return p


@router.get("/balance")
def balance(book_id: int, period_id: Optional[int] = None,
            from_period_id: Optional[int] = None,
            book: Book = Depends(get_my_book),
            db: Session = Depends(get_db), include_unposted: bool = Query(False)):
    """科目余额表。带 ``from_period_id`` 就是查**一段连续区间**（止期仍是 period_id）。

    两张表（余额表 / 总账）共用同一份区间实现（queries.balance_sheet_of_accounts），
    所以同一段区间在两边问出来的数必然一致 —— 审计第 7.3 节记过的
    「导出与页面显示对不上」，根子就是同一个口径有两份实现。
    """
    period = _period(db, book.id, period_id)
    from_period = None
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
        # 起始晚于止期：界面把它归位了，直接调接口也照样挡住 ——
        # 否则算出来的是一段「倒着走」的区间，期初比期末还晚，数字能看不能信。
        #
        # 同样的判断在共享实现 ``queries.balance_sheet_of_accounts`` 里也有一道
        # （下面那个 ``from_period`` 传下去就会走到）。这里保留是因为它最早触发、
        # 报错最直接；共享那一道才是真正的兜底 —— 它覆盖 ``/general``、
        # 打印与导出等**所有**委托方，而以前只有这一个接口查过 ord。
        if from_period.ord > period.ord:
            raise HTTPException(400, "起始期间不能晚于结束期间")
    # 科目表每个年度各有一份：跨年区间要取**区间涉及的所有年度**的并集。
    # 只取首尾那两张的话，中间年度（2027）的科目表压根没进查询 ——
    # 那年的分录挂在第三套科目行上，区间发生额会**整段消失且不报错**
    # （审查修复）。``subjects_for_range`` 按区间取全。
    subjects = queries.subjects_for_range(db, book.id, from_period, period)
    rows, check = queries.balance_sheet_of_accounts(
        db, book.id, subjects, period, include_unposted, from_period=from_period)
    is_range = from_period is not None and from_period.id != period.id
    return {
        "period": period.code, "period_id": period.id,
        "from_period": from_period.code if is_range else None,
        "from_period_id": from_period.id if is_range else None,
        "range": is_range,
        "rows": rows, "check": check, "include_unposted": include_unposted,
    }


@router.get("/general")
def general(book_id: int, period_id: Optional[int] = None, from_period_id: Optional[int] = None,
            subject_id: Optional[int] = None,
            book: Book = Depends(get_my_book), db: Session = Depends(get_db),
            include_unposted: bool = Query(False), leaf_only: bool = Query(False)):
    """总账（用友式）：**选一个一级科目**，看它在这段区间里的月度台账。

    行结构与用友的「总分类账」一致：上年结转 → 每月「本月合计 / 累计」→
    结转下年。屏幕上的样子与打印件是同一张纸。

    ``subject_id`` 必须是**一级科目**（用友的总账是总分类账）。不给就取
    第一个一级科目 —— 页面总要有个东西显示。

    ``leaf_only`` **不再有意义**（总账的单位是一级科目），留着是为了不打断
    老调用方，它不再影响任何输出。
    """
    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period

    subjects = queries.general_ledger_subjects(db, book.id, to_period.year)
    if not subjects:
        return {
            "from_period": from_period.code, "to_period": to_period.code,
            "from_period_id": from_period.id, "to_period_id": to_period.id,
            "subjects": [], "subject": None, "rows": [],
            "include_unposted": include_unposted,
        }

    chosen = subjects[0]
    if subject_id:
        picked = next((s for s in subjects if s.id == subject_id), None)
        if picked is None:
            raise HTTPException(404, "科目不存在或不是一级科目")
        chosen = picked

    payload = queries.general_ledger(
        db, book.id, chosen, from_period, to_period, include_unposted)
    return {
        "from_period": payload["from_period"],
        "to_period": payload["to_period"],
        "from_period_id": from_period.id,
        "to_period_id": to_period.id,
        # 一级科目清单 —— 页面上让人挑「看哪个科目」。
        "subjects": [{"id": s.id, "code": s.code, "name": s.name} for s in subjects],
        "subject": payload["subject"],
        "rows": payload["rows"],
        "include_unposted": include_unposted,
    }


@router.get("/detail")
def detail(book_id: int, subject_id: int, period_id: Optional[int] = None,
           from_period_id: Optional[int] = None, book: Book = Depends(get_my_book),
           db: Session = Depends(get_db), include_unposted: bool = Query(False),
           from_subject_id: Optional[int] = None, to_subject_id: Optional[int] = None):
    """明细账。倒置区间由 ``queries.detail_ledger`` 自己挡（400，同一条消息）。

    它跟余额表/总账不是同一个函数，所以那道 ord 校验是它自己的 ——
    本路由只负责把起始期间解析出来并确认它属于本账套。
    """
    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period
    return queries.detail_ledger(
        db, book.id, subject_id, from_period, to_period, include_unposted,
        from_subject_id=from_subject_id, to_subject_id=to_subject_id,
    )


def _voucher_seq(db: Session, book_id: int, period_id: int, voucher_id: int) -> Optional[int]:
    """一张凭证的**号段数字** —— 它在期间里的位置靠这个定。

    数字不在 Python 里抠：抠数字的判据只有一处（``svc.no_seq``），
    Python 再写一份早晚会跟它分道扬镳（库里真有 "wochakan" 这种号）。

    **跨账套的凭证 id 一律当没传**（返回 None）：宁可少算一张，
    也不能把别家账的数字端回来。**不是本期的那张也一样当没传** ——
    位置是「本期里的第几张」，跨期比不了。
    """
    row = db.execute(
        select(svc.no_seq().label("seq"))
        .select_from(Voucher)
        .where(Voucher.id == voucher_id,
               Voucher.book_id == book_id,
               Voucher.period_id == period_id)
    ).first()
    return row.seq if row is not None else None


@router.get("/voucher-balances")
def voucher_balances(book_id: int, period_id: Optional[int] = None,
                     voucher_id: Optional[int] = Query(None),
                     insert_after: Optional[int] = Query(None),
                     insert_position: str = Query("before"),
                     book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """凭证录入页每行科目格左下角那个数字。

    基数取的是「**这张凭证之前**账上有多少」，不是整期期末 ——
    一张 100、一张 300、一张 500，月中那张该看到 100，不是 900
    （口径见 ``queries.voucher_subject_balances``）。

    位置从两个口子进来：

    * ``voucher_id``：已经在录的这张。它会边填边自动保存，位置得按**它**算。
    * ``insert_after`` + ``insert_position``：一张**还没落库**、插在某张中间的新凭证。
      插在后面 → 往后挪一号；插在前面 → 占参照的号（参照自己顺延一位）。

    两个都不给（= 一张普通的新凭证，落在本期末尾）时位置不明，
    基数退回「整期期末」—— 那正好是它前面的账。
    """
    period = _period(db, book.id, period_id)
    position = None
    if voucher_id:
        seq = _voucher_seq(db, book.id, period.id, voucher_id)
        if seq is not None:
            position = (seq, voucher_id)
    elif insert_after:
        seq = _voucher_seq(db, book.id, period.id, insert_after)
        if seq is not None:
            position = (seq + 1 if insert_position == "after" else seq, 0)
    balances = queries.voucher_subject_balances(db, book.id, period, position=position)
    return {"period_id": period.id, "period": period.code,
            "balances": {str(k): v for k, v in balances.items()}}


# 原来的 `GET /ledger/voucher-no-preview`（只回一个 `next` 号码）撤了：
# 前端一处都没接它，而新开凭证真正需要的是「号码 + 期间最后一张的日期」两个值，
# 那已经在 `GET /books/{id}/vouchers/next` 里了 —— 留在 ledger 底下也放错了地方。
