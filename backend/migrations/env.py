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

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
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
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
