"""数据库连接与会话。

连接池的四个容量参数（``pool_size`` / ``max_overflow`` / ``pool_timeout`` /
``pool_recycle``）全部来自 ``config.py``，那里的默认值逐字等于 SQLAlchemy
QueuePool 的默认值 —— 只有 ``pool_recycle`` 是新给的 300s，理由写在
config.py 对应字段的注释里。
"""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
    # 下面四个以前全走 QueuePool 默认值（全仓零配置），现在写下来、可配置。
    # 注意它们是**每个进程一套** —— 多 worker 时要按「进程数 ×」去算 PG 侧
    # 的 max_connections，理由见 config.py 里对应注释。
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_pool_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
