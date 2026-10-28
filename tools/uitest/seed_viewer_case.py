"""界面自查用的**临时只读账号**（VIEWER）。

**为什么要有它**：2026-10-28 修的老遗留 —— 只读账号打开「流水明细」会弹一条
「当前账套为只读权限」（而流水行其实好好地显示着），打开凭证页也会吃一个 403。
那两条**只有用只读账号才验得到**：管理员账号压根不会触发它们。

**自己造、自己清**（与 ``seed_flow_lock_case.py`` 同一个规矩）：

* 跑之前**先清同名残留再新建** —— 上一轮失败留下的账号不许混进这一轮；
* 跑完由自查脚本调 ``--clean`` 清掉；清不掉也不影响下一轮（下一轮会先清）。

用法（dev 库）::

    cd backend && .venv/bin/python ../tools/uitest/seed_viewer_case.py
    cd backend && .venv/bin/python ../tools/uitest/seed_viewer_case.py --clean
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import Book, SysUser, UserBook  # noqa: E402

USERNAME = "uitest_viewer"
PASSWORD = "uitest123456"


def _book_id(db) -> int:
    """自查用的账套：admin 拥有的那一本（dev 库里是「宏发商贸」）。"""
    admin = db.scalar(select(SysUser).where(SysUser.username == "admin"))
    if admin is not None:
        link = db.scalar(select(UserBook).where(
            UserBook.user_id == admin.id, UserBook.role == "OWNER"))
        if link is not None:
            return link.book_id
    book = db.scalar(select(Book).order_by(Book.id).limit(1))
    if book is None:
        raise SystemExit("dev 库里一本账套都没有 —— 自查没法做")
    return book.id


def _cleanup(db) -> None:
    u = db.scalar(select(SysUser).where(SysUser.username == USERNAME))
    if u is None:
        return
    for link in db.scalars(select(UserBook).where(UserBook.user_id == u.id)).all():
        db.delete(link)
    db.delete(u)
    db.commit()


def main() -> None:
    clean_only = "--clean" in sys.argv
    with SessionLocal() as db:
        # **先清同名残留再新建** —— 上一轮失败留下的账号不许混进这一轮。
        _cleanup(db)
        if clean_only:
            print(json.dumps({"cleaned": True, "username": USERNAME}, ensure_ascii=False))
            return

        book_id = _book_id(db)
        u = SysUser(
            username=USERNAME,
            password_hash=hash_password(PASSWORD),
            real_name="界面自查·只读",
            active=True,
            is_admin=False,
        )
        db.add(u)
        db.flush()
        db.add(UserBook(user_id=u.id, book_id=book_id, role="VIEWER"))
        db.commit()
        book = db.get(Book, book_id)
        print(json.dumps({
            "username": USERNAME,
            "password": PASSWORD,
            "book_id": book_id,
            "book_name": getattr(book, "name", ""),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
