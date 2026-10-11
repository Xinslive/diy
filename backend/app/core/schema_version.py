"""数据库 schema 版本校验（审计 P3-01）。

**这里做的事，以及为什么只做这么多。**

改造前，应用每次启动都 ``Base.metadata.create_all()`` 并重放整份 ``db_init.sql``。
而 ``create_all`` 只建**缺失的表**，不碰已存在的表 —— 于是「给已有表加一列
NOT NULL」「改列类型」「加索引」这类变更在生产上会静默不生效：服务照常启动，
问题要到运行时才炸，而那时数据已经写进去了。

修法不是「把 create_all 换成一个更勤快的 DDL 执行器」，而是**反转职责**：

  * schema 的变更**只**由 Alembic revision 负责，走版本控制、走评审；
  * 应用启动**只做版本检查**，不执行任何 DDL。对得上就放行，
    对不上就拒绝启动并说清楚该执行哪条命令。

一个连「自己跑在哪个 schema 版本上」都不知道的服务，出问题时没有任何
可复现的立足点 —— 这是 P3-01 判为 B 级的根本原因。

**存量库怎么办。** 第一次上这套机制时，生产库已经有表、但没有 ``alembic_version``。
这种情况**不自动 stamp**：自动 stamp 等于替人断言「这个库恰好等于 baseline」，
而 assert 是没法验证的。所以这里拒绝启动，把两条命令和适用条件一起打出来，
由运维确认后再执行。宁可多一次人工确认，也不要在错误的断言上继续跑。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# 参数也叫 engine，模块级这个必须换个名字，否则函数体里的 engine
# 一律指参数，``engine if engine is not None else engine`` 这种废话就出来了
from app.core.db import engine as default_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


class SchemaVersionError(RuntimeError):
    """schema 版本对不上。消息必须自带「现在该怎么办」。"""


def _config():
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return cfg


def head_revision() -> str | None:
    """代码里最新的那个 revision。

    ``migrations/`` 整个目录没跟着代码走时，Alembic 抛的是 ``PathError`` ——
    那个栈对运维没有任何信息量。这里吞掉，让调用方给出「部署不完整」的说法。
    """
    from alembic.script import ScriptDirectory

    try:
        return ScriptDirectory.from_config(_config()).get_current_head()
    except Exception:
        return None


def current_revision(engine: Engine) -> str | None:
    """库里 ``alembic_version`` 表记的版本；表不存在返回 None。"""
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        if "alembic_version" not in names:
            return None
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return rows[0] if rows else None


def _app_table_count(engine: Engine) -> int:
    """有多少张业务表。用来分辨「空库」和「没版本的存量库」。"""
    with engine.connect() as conn:
        return len([
            t for t in inspect(conn).get_table_names()
            if t not in ("alembic_version",)
        ])


def assert_schema_ready(engine: Engine | None = None) -> str:
    """启动闸门。版本对得上就返回该版本，对不上就抛 :class:`SchemaVersionError`。

    ``engine`` 省略时用应用自己的全局 engine（启动路径走这条）。
    """
    eng = engine if engine is not None else default_engine

    head = head_revision()
    if head is None:
        raise SchemaVersionError(
            "migrations/versions 下没有任何 revision，代码里没有可迁移到的目标版本。"
            "这是部署不完整（migrations 目录没跟着代码走），不是数据库的问题。"
        )

    current = current_revision(eng)

    if current == head:
        return current

    if current is None:
        existing = _app_table_count(eng)
        if existing == 0:
            raise SchemaVersionError(
                f"数据库是空的，且没有 alembic_version —— schema 还没建。\n"
                f"  执行：cd {BACKEND_ROOT} && .venv/bin/alembic upgrade head\n"
                f"（服务不会替你建表了：启动时自动建表正是 P3-01 要去掉的行为）"
            )
        raise _legacy_error(head, existing)

    known = _known_revisions()
    if current in known:
        raise SchemaVersionError(
            f"数据库停在 {current}，代码要求 {head} —— 库比代码旧。\n"
            f"  执行：cd {BACKEND_ROOT} && .venv/bin/alembic upgrade head\n"
            f"  注意：migration 会改表结构，**先备份**再执行。"
        )
    raise _unknown_error(current, head)


def _legacy_error(head: str, existing: int) -> SchemaVersionError:
    return SchemaVersionError(
        f"数据库里已经有 {existing} 张表，但没有 alembic_version —— 这是引入 Alembic "
        f"之前建的存量库，代码无法确认它当前对应哪个版本。\n"
        f"这里**不自动 stamp**：那等于替人断言「这个库恰好等于 {head}」，"
        f"而这个断言没法验证，一旦库其实已经跑在别的版本上，"
        f"后面的 migration 会在错误的结构上执行。\n"
        f"  1) 先确认这个库的代码就是引入 Alembic 之前那一版、且跑得正常；\n"
        f"  2) 备份；\n"
        f"  3) 执行：cd {BACKEND_ROOT} && .venv/bin/alembic stamp {head}\n"
        f"  如果这个库其实已经在跑更新的代码，请人工核对结构后，"
        f"改用能反映真实结构的版本号 stamp，而不是 {head}。"
    )


def _known_revisions() -> set[str]:
    """这份代码认识的全部版本号。

    ``walk_revisions()`` 吐出的是 ``Script`` **对象**，直接塞进 set 的话
    ``current in known`` 永远为假 —— 于是「库停在某个已知旧版本」会被误判成
    「库里是个不认识的版本号」，提示语从「upgrade head」变成「回滚代码」，
    而后者在版本落后时是错的建议。
    """
    from alembic.script import ScriptDirectory

    try:
        return {r.revision for r in ScriptDirectory.from_config(_config()).walk_revisions()}
    except Exception:
        return set()


def _unknown_error(current: str, head: str) -> SchemaVersionError:
    return SchemaVersionError(
        f"数据库的 schema 版本是 {current}，这份代码里找不到这个 revision "
        f"（代码要的是 {head}）。\n"
        f"最常见的原因：**回滚了代码但没回滚数据库**。新版本的 migration 已经"
        f"在库上跑过，退回旧代码后旧代码不认识新的版本号。\n"
        f"  要么把代码换回对应的版本，要么按新代码的 migration 继续往前。"
    )


def upgrade_to_head(engine: Engine | None = None) -> str:
    """把库迁到 head。**只给脚本和部署用**，应用启动路径不调它。"""
    from alembic import command

    eng = engine if engine is not None else default_engine
    cfg = _config()
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        with conn.begin():
            command.upgrade(cfg, "head")
    return head_revision() or ""
