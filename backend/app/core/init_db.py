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

        install(db)
        db.commit()
    except Exception as e:  # pragma: no cover
        db.rollback()
        log.warning("安装内置报表模板失败: %s", e)
    finally:
        db.close()

    log.info("数据库初始化完成")
