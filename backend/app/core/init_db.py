"""建表 + 安装会计护栏 + 安装内置报表模板。幂等，可重复执行。"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.db import Base, engine

log = logging.getLogger("cwq.init")

SQL_FILE = Path(__file__).with_name("db_init.sql")


def init_database(engine: Engine | None = None) -> None:
    import app.models  # noqa: F401  确保所有模型已注册

    eng = engine if engine is not None else globals()["engine"]
    Base.metadata.create_all(eng)

    sql = SQL_FILE.read_text(encoding="utf-8")
    with eng.begin() as conn:
        # 用裸 DBAPI 游标执行：psql 风格的整段 DDL 含大量分号与 %，
        # 走 SQLAlchemy 的参数化通道会被 psycopg 当作占位符解析。
        raw = conn.connection.driver_connection
        with raw.cursor() as cur:
            cur.execute(sql)

    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        from app.seed.builtin_reports import install
        from app.seed.voucher_no import repad_voucher_numbers

        install(db)
        db.commit()
        # 发号口径改成三位之后，把库里已有的号一起收窄（只补零，不改数字）。
        # 幂等：已经补过的一张都命中不了条件，跑第二遍是 0 改动。
        repad_voucher_numbers(db)
    except Exception as e:  # pragma: no cover
        db.rollback()
        # 失败一定要留下话：早先这个 except 只提「报表模板」，凭证号补零挂在同一个
        # try 下面又没提，触发器一顶回来，日志看着像「什么都没发生」，数据其实没迁。
        log.warning("初始化种子数据失败（报表模板 / 凭证号补零）: %s", e)
    finally:
        db.close()

    log.info("数据库初始化完成")
