#!/usr/bin/env python3
"""清掉演示 / 测试用的账套，让「账套管理」页只留真实在用的账。

    .venv/bin/python scripts/cleanup_demo_books.py            # 只看会删什么
    .venv/bin/python scripts/cleanup_demo_books.py --apply    # 真删

删的都是**可重建**的东西：

* ``启明物业``        —— ``seed_demo.py`` 造的演示账套
* ``压测账套01``      —— ``seed_perf.py`` 造的压测账套（15000 张凭证）
* ``租金账套`` / ``管理费账套`` —— 浏览器端 e2e 留下的空壳（0 张凭证），
  名字匹配 ``seed_demo.py`` 里那份清理正则

**不会碰**其它账套。名单是写死的，不按模式乱匹配 —— 真实账套的名字不该
被一条正则猜掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import Book, InvoiceConfig, InvoiceRule, Subject  # noqa: E402

#: 要清理的账套名（精确匹配）
TARGETS = ("启明物业", "压测账套01", "租金账套", "管理费账套")


def wipe_book(db, book_id: int) -> None:
    """删掉一个账套的全部数据。

    两道数据库护栏都得**按规矩过**，不能关掉触发器硬删：
    先解锁期间，再把已记账的凭证退回未记账，最后才删。

    顺序是有讲究的：``invoice_config`` / ``invoice_rule`` 外键指着
    ``subject.id``，科目先删就撞外键 —— 所以引用科目的那些行必须**先**走。
    分录随凭证级联删除，不用单独处理。
    """
    from app.models import Period, PeriodOpening, UserBook, Voucher

    db.execute(Period.__table__.update()
               .where(Period.book_id == book_id).values(locked=False))
    db.execute(Voucher.__table__.update()
               .where(Voucher.book_id == book_id, Voucher.status == "POSTED")
               .values(status="UNPOSTED"))
    db.execute(Voucher.__table__.delete().where(Voucher.book_id == book_id))
    db.execute(PeriodOpening.__table__.delete().where(PeriodOpening.book_id == book_id))

    # 发票配置与发票规则：科目外键指向 subject，科目先删就撞 RESTRICT
    for model in (InvoiceConfig, InvoiceRule):
        db.execute(model.__table__.delete().where(model.__table__.c.book_id == book_id))

    db.execute(Period.__table__.delete().where(Period.book_id == book_id))
    db.execute(Subject.__table__.update()
               .where(Subject.book_id == book_id).values(parent_id=None))
    db.execute(Subject.__table__.delete().where(Subject.book_id == book_id))
    db.execute(UserBook.__table__.delete().where(UserBook.book_id == book_id))
    db.execute(Book.__table__.delete().where(Book.id == book_id))
    db.commit()


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()
    try:
        books = db.scalars(select(Book).order_by(Book.id)).all()
        hits = [b for b in books if b.name in TARGETS]
        if not hits:
            print("没有要清理的账套。")
            return 0
        print("将要清理：")
        for b in hits:
            n = db.scalar(select(Subject.id).where(Subject.book_id == b.id).limit(1))
            print(f"  #{b.id:<4}{b.name:<16}公司：{b.company_name}")
        if not apply:
            print("\n（这是预览。加上 --apply 才真删）")
            return 0
        for b in hits:
            wipe_book(db, b.id)
            print(f"  已删除：{b.name}")
        print(f"\n清理完成，剩下 {len(books) - len(hits)} 个账套。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
