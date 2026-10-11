#!/usr/bin/env python
"""一键初始化：执行迁移建表 + 护栏 + 内置报表模板 + 演示数据。

    python scripts/init_db.py --reset
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.schema_version import upgrade_to_head  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import Book, Period, Subject, SysUser, UserBook  # noqa: E402


def reset() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    print("· 已清空数据库")


def seed() -> None:
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    db = SessionLocal()
    try:
        if db.scalar(select(SysUser).limit(1)):
            print("· 已有数据，跳过播种")
            return

        admin = SysUser(username="admin", password_hash=hash_password("123456"),
                        real_name="系统管理员", active=True, is_admin=True)
        zhang = SysUser(username="zhangsan", password_hash=hash_password("123456"),
                        real_name="张三", active=True)
        db.add_all([admin, zhang])
        db.flush()

        for name, start_month in [("租金账套", 1), ("管理费账套", 1)]:
            book = Book(company_name="紫欣星悦荟", name=name, year=2026, start_month=start_month)
            db.add(book)
            db.flush()
            create_periods(db, book)
            install_chart(db, book.id)
            db.add(UserBook(user_id=admin.id, book_id=book.id, role="OWNER"))
            db.add(UserBook(user_id=zhang.id, book_id=book.id, role="ACCOUNTANT"))

        db.commit()
        print("· 已创建用户 admin / zhangsan（密码均为 123456）")
        print("· 已创建 2 个账套（紫欣星悦荟 · 租金账套 / 管理费账套），各含 2026 全年 12 个会计期间")
    finally:
        db.close()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    # 建表交给 Alembic（P3-01）。这里不再 ``create_all`` ——
    # 那正是「服务启动时自己把缺的补上」的做法，schema 版本因此无从记录。
    upgrade_to_head()
    seed()
    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")).scalar()
        triggers = conn.execute(text(
            "SELECT count(*) FROM information_schema.triggers WHERE trigger_schema='public'")).scalar()
    print(f"· 完成：{tables} 张表，{triggers} 个触发器/约束触发器")
