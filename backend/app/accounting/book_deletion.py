"""账套删除。

**两条路径，都是隐形的**：

1. 管理员收回一个账套的**最后一份授权** → 这个账套一个授权用户都没有了
   → 随这次授权变更一并删掉（见 :func:`purge_unowned_books`）。
2. 账套只剩**一个**授权用户时，这个人把账套改名成「删除」→ 删掉这本账
   （见 ``api/books.py::_delete_by_rename``）。

界面上**没有**「删除账套」按钮，也没有任何文案提示第二条路径 ——
机制不外露，只在审计日志里留痕（``DELETE_BOOK``）。

----
为什么删一本账要这么讲究
------------------------

``book`` 的子表外键**几乎全是** ``ON DELETE CASCADE``，看上去「删一行 book
就够」。实际不是，有两类东西会挡住它：

**一、外键不肯级联的**（必须显式先清，否则 DELETE book 直接报错）：

===========================  ==============  ==========================
引用                          delete_rule     什么时候撞上
===========================  ==============  ==========================
``invoice_config.*_subject_id`` → subject    RESTRICT   配过发票科目
``invoice_rule.subject_id``     → subject    RESTRICT   配过发票规则
``period_opening.subject_id``   → subject    RESTRICT   录过期初数
``entry.subject_id``            → subject    NO ACTION  有分录
``voucher.period_id``           → period     NO ACTION  有凭证
``report_result.period_id``     → period     NO ACTION  跑过报表
===========================  ==============  ==========================

**二、触发器直接抛错的**（账上有数据时必踩）：

* ``fn_voucher_delete_guard`` / ``fn_entry_lock_guard``：
  会计期间**已结账**时，删凭证、删分录都报「会计期间已结账，禁止删除凭证」。
* ``fn_posted_voucher_guard``：**已记账**的凭证不许删
  （「凭证 % 已记账，不能修改或删除；如需改动请先反记账」）。

这两道是**账务护栏**，它们拦的是「有人偷偷改账」。删整本账不是改账，
但护栏看不见「整本账要没了」这件事 —— 所以这里先把锁解开、把已记账的
凭证反记账，**再**删。这三步都在同一个事务里，删不掉就整体回滚，
账面不会留下「解了锁没删掉」的半截状态。

解开的锁、反记账的凭证，都是**即将被删掉的行**上的锁与状态：
这笔账马上就消失了，谈不上「有人改了已结账的账」。

**三、子表顺序不能乱**：``data_version`` 的触发器
（``fn_bump_book_data_version``）在删子表时会 ``UPDATE book SET data_version
= data_version + 1``。先删 book 再删子表，这条 UPDATE 打在一行已经不存在的
账套上（不报错，但版本号就丢了）。所以**子表一律先删，book 最后删**。

**四、审计要先写**：``audit_log.book_id`` 是 ``ON DELETE SET NULL`` ——
账套删掉之后，那条「账套被删了」的日志仍然留得住（book_id 变 NULL，
事实全在 detail / before 里）。但顺序不能反：先删账套再写日志，
INSERT 时会撞外键（账套已经不存在了）。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.models import (Book, Entry, InvoiceConfig, InvoiceRule, Period, PeriodOpening,
                        ReportResult, Subject, UserBook, Voucher)

#: 保留名。账套改名叫它 = 删掉这本账（见模块说明）。
DELETION_NAME = "删除"

#: 触发来源，只进审计日志，不进任何界面。
TRIGGER_REVOKE = "REVOKE_LAST_GRANT"      # 路径一：管理员收回最后一份授权
TRIGGER_RENAME = "RENAME_TO_DELETION"      # 路径二：改名成「删除」


def is_deletion_name(name: Optional[str]) -> bool:
    """这个账套名是不是「删除」。前后空白不算数。"""
    return (name or "").strip() == DELETION_NAME


def _counts(db: Session, book_id: int) -> dict:
    """这本账里各有多少行 —— 删掉之后就查不到了，删之前先记下来。"""

    def n(model) -> int:
        stmt = select(func.count()).select_from(model).where(model.book_id == book_id)
        return int(db.scalar(stmt) or 0)

    return {
        "subjects": n(Subject),
        "periods": n(Period),
        "vouchers": n(Voucher),
        "entries": n(Entry),
        "openings": n(PeriodOpening),
        "grants": n(UserBook),
    }


def delete_book(db: Session, book: Book, actor, *, trigger: str) -> dict:
    """物理删掉一本账，连同它全部业务数据。**调用方负责 commit。**

    返回的是**纯字典**，不是 ORM 对象：账套行已经没了，
    之后再读 ``book`` 的任何属性都会变成 ``ObjectDeletedError``。
    """
    book_id = book.id
    company_name = book.company_name
    name = book.name
    snapshot = {
        "id": book_id,
        "company_name": company_name,
        "name": name,
        "start_year": book.start_year,
        "start_month": book.start_month,
        **_counts(db, book_id),
    }

    # 审计**先写、先 flush**：audit_log.book_id 是 SET NULL，
    # 账套删掉之后这条日志仍留得住（book_id 变 NULL，事实全在 before 里）。
    # 反过来（先删账套再写日志）会在 INSERT 时撞外键。
    log(db, action="DELETE_BOOK", entity="book", entity_id=book_id, user=actor,
        book_id=book_id,
        detail=f"删除账套 {company_name} · {name}",
        before=snapshot)
    db.flush()

    # ---- 1. 拆掉会直接抛错的三道护栏 ---------------------------------------
    # 结账锁：period.locked=true 时，删凭证 / 删分录都被
    # fn_voucher_delete_guard / fn_entry_lock_guard 挡下。
    # 整本账都要没了，先把锁解开 —— 解的是**即将被删掉**的那些期间。
    db.execute(update(Period).where(Period.book_id == book_id).values(locked=False))
    # 已记账：POSTED 凭证不许删（fn_posted_voucher_guard）。
    # 「已记账 → 未记账」是库里允许的合法跳转，所以这里走的是正路。
    db.execute(
        update(Voucher)
        .where(Voucher.book_id == book_id, Voucher.status == "POSTED")
        .values(status="UNPOSTED")
    )

    # ---- 2. 按外键顺序显式清子表 -------------------------------------------
    # 先清指向 subject 的 RESTRICT，再清指向 period 的 NO ACTION，
    # 最后清 entry（它自己指向 subject，NO ACTION）。
    db.execute(delete(InvoiceConfig).where(InvoiceConfig.book_id == book_id))
    db.execute(delete(InvoiceRule).where(InvoiceRule.book_id == book_id))
    db.execute(delete(PeriodOpening).where(PeriodOpening.book_id == book_id))
    db.execute(delete(Entry).where(Entry.book_id == book_id))
    db.execute(delete(Voucher).where(Voucher.book_id == book_id))
    db.execute(delete(ReportResult).where(ReportResult.book_id == book_id))

    # ---- 3. 删账套本身 ------------------------------------------------------
    # 其余子表（subject / period / book_year / sys_user_book / print_template /
    # report_template / invoice_batch / invoice_record / import_batch /
    # plugin_*）由 CASCADE 带走；audit_log 是 SET NULL，日志留下。
    db.execute(
        delete(Book).where(Book.id == book_id).execution_options(synchronize_session=False)
    )
    # 把这个对象从会话里摘出去：它已经是库里的空气，
    # 留在会话里只会在 commit 时触发一次「刷新一个不存在的行」。
    db.expunge(book)
    return {"id": book_id, "company_name": company_name, "name": name,
            "trigger": trigger, "before": snapshot}


def purge_unowned_books(
    db: Session,
    actor,
    book_ids: Iterable[int],
    *,
    trigger: str = TRIGGER_REVOKE,
) -> list[dict]:
    """这些账套里，凡是现在**一个授权用户都没有**的，删掉。

    「没有授权用户」= ``sys_user_book`` 里这个账套一行都没有 ——
    没人能打开它，也没人能删它，只能烂在库里。

    **调用方负责 commit**（与授权变更在同一个事务里：
    授权改了一半、账套删不掉时，整体回滚）。

    返回被删掉的账套（纯字典列表）。
    """
    out: list[dict] = []
    for book_id in dict.fromkeys(int(b) for b in book_ids):
        # 锁住账套行：并发授权要往 sys_user_book 插一行，那条插入得先对账套行
        # 拿 KEY SHARE 锁 —— 锁住之后别人插不进来，
        # 「刚查完没人授权」到「删掉」之间不会冒出半个授权。
        exists = db.execute(
            select(Book.id).where(Book.id == book_id).with_for_update()
        ).scalar()
        if exists is None:
            continue          # 账套已经没了
        if db.scalar(
            select(func.count()).select_from(UserBook).where(UserBook.book_id == book_id)
        ):
            continue          # 还有人有权，不动
        book = db.get(Book, book_id)
        if book is None:
            continue
        out.append(delete_book(db, book, actor, trigger=trigger))
    return out
