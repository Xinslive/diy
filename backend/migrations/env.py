"""Alembic 运行环境。

连接串**只**从 ``app.core.config.settings`` 取，不读 alembic.ini ——
单点来源。测试里 ``conftest`` 会先把 ``DATABASE_URL`` 指向测试库，
于是这里跟着指向测试库，``alembic upgrade head`` 打的才是测试库。
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.core.db import Base

# 必须先 import app.models，模型的元数据才会注册到 Base 上，
# 否则 autogenerate 会认为「所有表都是多余的」。
import app.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    # disable_existing_loggers 必须显式关掉。
    # 迁移不总是 ``alembic`` 命令行跑的 —— conftest 为了让整套测试用迁移建库，
    # 会在**当前进程里**调 command.upgrade，于是这里的 fileConfig 成了对宿主
    # 应用的一次全局重配。而它的默认值 True 会把已经存在的 logger 全部禁用，
    # 直接后果是 caplog 抓不到任何东西、启动日志整段消失。
    # 迁移只该管自己那几行输出，不该改别人的日志。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: 每条 revision 各自一个事务，而不是把整条链包在**一个**事务里。
#:
#: **为什么开这个开关（实测过的结论，不是照抄 alembic 文档）。**
#:
#: 0001..0007 全程被 ``context.begin_transaction()`` 包在一个事务里，带来两个
#: 具体后果：
#:
#: 1. **索引只能建在锁里。** ``0006`` 放弃 ``CREATE INDEX CONCURRENTLY`` 的
#:    直接原因就在这里：跑 0006 时，0005 刚加的外键仍持着 ``entry`` /
#:    ``period_opening`` 的 ACCESS EXCLUSIVE 锁，而同一次 upgrade 里的另一条
#:    连接要等这把锁 —— 等锁的那个事务正是持锁的那个事务，必然自锁。
#: 2. **一条失败要重放整条链。** 0005 / 0007 都是「重放整份 db_init.sql」，
#:    里面有全表校验级的 ``ADD CONSTRAINT ... FOREIGN KEY``。整条链一个事务时，
#:    中途失败 = 全部回滚 = 下次重来一遍。
#:
#: 每条 revision 自己一个事务之后：每条内部仍然是原子的（PostgreSQL 的 DDL 是
#: 事务性的，``transactional_ddl`` 为真，这一点**没有变**），变的只是**失败时
#: 已提交的部分会留下来**，而 ``alembic_version`` 正好记录了停在哪 —— 下次
#: ``upgrade head`` 从那里接着跑。这与本项目「每条 revision 自包含且幂等」的
#: 写法是一致的（见 0001 / 0003 / 0005 / 0007 的 docstring）。
#:
#: **它不能做什么（别把这两件事混为一谈）。**
#:
#: 开这个开关**只是**让 ``op.get_context().autocommit_block()`` 在**命令行**
#: 路径上可用（实测：``alembic upgrade head`` 通过）。它**不**解决
#: ``app/core.schema_version.upgrade_to_head`` 那条路径 ——
#: 那里把连接交给 alembic 之前先开了 ``with conn.begin():``，alembic 据此判定
#: ``_in_external_transaction`` 并**跳过**本开关的逐 revision 事务，于是
#: ``autocommit_block()`` 里的 ``assert self._transaction is not None`` 照样
#: 炸（实测 AssertionError，见 0006 docstring 的阻塞点 1）。
#: 换句话说：**0006 想改用 CONCURRENTLY，前置条件还包括让 ``upgrade_to_head``
#: 不再自带外层事务**，而那个文件不在本工作线的归属范围内。所以这里只开前置
#: 条件，不动 0006（改它也不会有任何可观测的效果）。
TRANSACTION_PER_MIGRATION = True


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        transaction_per_migration=TRANSACTION_PER_MIGRATION,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            _run(connection)
    else:
        # 由调用方提供连接（conftest / 测试里复用已有 engine 时走这条）
        _run(connectable)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      transaction_per_migration=TRANSACTION_PER_MIGRATION,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
