"""数据库连接与会话。

连接池的四个容量参数（``pool_size`` / ``max_overflow`` / ``pool_timeout`` /
``pool_recycle``）全部来自 ``config.py``，那里的默认值逐字等于 SQLAlchemy
QueuePool 的默认值 —— 只有 ``pool_recycle`` 是新给的 300s，理由写在
config.py 对应字段的注释里。
"""
from collections.abc import Iterator

from sqlalchemy import create_engine, event
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


@event.listens_for(engine, "connect")
def _pin_session_timezone(dbapi_conn, _record) -> None:
    """把每条连接的会话时区钉死成 UTC。

    PostgreSQL 的 ``TimeZone`` GUC 决定 ``timestamptz`` 取回来时带什么
    tzinfo：**同一个时刻，写进去的时候不带偏置，读回来带什么完全由会话时区
    决定**。默认取自服务器配置，而服务器之间并不一致 ——

      · 生产机 110.42.44.53 实测是 ``Asia/Shanghai``
      · GitHub 的 ``postgres:16`` 服务容器是 ``Etc/UTC``
      · 字面量恰好是 ``UTC`` 时 psycopg 交出 ``datetime.timezone.utc``

    于是不钉的话，应用读回的每个时间戳都可能带 +08:00，而库里存的是同一瞬间。
    任何「拿库里时间戳和 ``utcnow()`` 比」的地方都会差 8 小时 —— TTL 清理、
    「是否逾期」「是否已结账」这类判断都会算错，而且是静默算错。

    钉死之后无论服务器怎么配都返回 UTC，账务口径与部署环境解耦。

    这里只挂应用引擎：``ALTER ROLE`` / ``ALTER DATABASE`` 那条路要写进部署步骤，
    换个连接串或者换个 PG 实例就失效了；而「应用自己保证读回来的时间戳是 UTC」
    是这条不变量本来就该成立的地方。

    **必须 commit，否则这条 SET 活不过第一次归还连接池。**
    PostgreSQL 里 ``SET`` 是**事务性**的：它在事务里执行、随那个事务一起
    提交或**回滚**。而连接池归还连接时做的正是 ``rollback()`` ——
    于是连接上那句 ``SET TIME ZONE 'UTC'`` 被撤销，时区打回服务器默认
    （本机是 ``Etc/UTC``，生产机实测是 ``Asia/Shanghai``）。

    后果不是「时区不准」这么轻：**每条连接只有第一次用是对的**，
    之后复用的时间戳全带服务器本地时区 —— 正好是这段注释想防的那件事，
    而且它只在连接被复用之后才发作，测试里表现为**时好时坏**
    （2026-10-22 在清理测试库时暴露：同一条用例，连接是新的就绿、
    复用的就红，报 ``Etc/UTC != timezone.utc``）。

    ``commit()`` 之后这条 SET 落到了「会话级已提交」的状态，
    后续任何 ``rollback()`` 都撤销不了它 —— 这才是「钉死」。
    """
    with dbapi_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    # 关键：SET 是事务性的，不提交就会随连接归还池时的 rollback 一起消失。
    dbapi_conn.commit()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
