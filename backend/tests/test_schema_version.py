"""schema 版本闸门（审计 P3-01）。

改造前，每次启动都 ``create_all`` + 重放 ``db_init.sql``。而 ``create_all``
只建**缺失的表**，不碰已存在的表 —— 于是「给已有表加 NOT NULL 列」「改列类型」
这类变更在生产上静默不生效：服务照常启动，问题到运行时才炸，而那时数据已经写进去了。

修法是把职责反转：表结构的变更**只**由 Alembic revision 负责，
应用启动**只做版本检查**，对不上就拒绝启动。

这里钉三件事：
  1. 四种「对不上」都能被认出来，并给出可执行的修复命令；
  2. 启动路径上**不再有任何 DDL**（静态守卫，防回归）；
  3. 测试库本身是由迁移建出来的 —— 这样「baseline 迁移能不能重建出一套
     能用的 schema」被 500 多条用例天天验证，而不是靠人相信。
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from sqlalchemy import create_engine, text

from app.core.db import engine
from app.core.schema_version import (SchemaVersionError, assert_schema_ready,
                                     current_revision, head_revision)
from tests.conftest import TEST_DB_URL

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 夹具：临时 schema

@pytest.fixture
def scratch():
    """在同一个库里开一个独立 schema 当「另一个数据库」。

    为什么不新建真库：每个用例开一次库要 1~2 秒，几条用例就多出十几秒；
    而这些用例真正要的只是「一个结构已知、由我摆布的库」。
    """
    name = "scratch_sv"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        conn.exec_driver_sql(f"CREATE SCHEMA {name}")
    # search_path **只**留 scratch 本身。早先写成 ``scratch,public``，
    # 于是它能看见主 schema 里的 alembic_version 和全部业务表 ——
    # 所谓的「临时库」根本就是主库，测试于是全部通过而什么都没测到。
    eng = create_engine(
        TEST_DB_URL,
        connect_args={"options": f"-c search_path={name}"},
    )
    yield eng, name
    eng.dispose()
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _stamp(eng, rev: str) -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
        conn.exec_driver_sql("DELETE FROM alembic_version")
        # exec_driver_sql 走的是裸 DBAPI，不做参数绑定 —— 冒号会原样发给
        # postgres 当语法。带参数就得用 conn.execute(text(...), params)。
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": rev})


def _fake_table(eng, name="voucher") -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")


# ------------------------------------------------------------ 代码里有 head

def test_代码里确实有_head():
    head = head_revision()
    assert head, "migrations/versions 下没有 revision —— 部署出去也没有可迁移到的版本"


# ------------------------------------------------ 四种「对不上」都要能认出来

def test_版本一致就放行(db):
    assert assert_schema_ready(engine) == head_revision()


def test_已知版本集合是版本号字符串():
    """``walk_revisions()`` 吐的是 Script 对象，不是字符串。

    直接 set() 一下的话 ``current in known`` 恒为假，「库停在已知旧版本」
    会被误判成「库里是个不认识的版本号」—— 提示语从 ``upgrade head`` 变成
    「回滚代码」，而后者在版本落后时是错的建议。
    """
    from app.core.schema_version import _known_revisions

    known = _known_revisions()
    assert known, "一份 revision 都认不出来"
    assert all(isinstance(r, str) for r in known), f"集合里混进了非字符串：{known}"


def test_空库_拒绝启动并让运维去跑迁移(scratch):
    eng, _ = scratch
    with pytest.raises(SchemaVersionError) as e:
        assert_schema_ready(eng)
    msg = str(e.value)
    assert "alembic upgrade head" in msg, f"没告诉运维该执行什么：{msg}"
    # 关键：不能再悄悄把表建出来 —— 那正是 P3-01 要去掉的行为
    assert "不会替你建表" in msg, f"没说明服务不再自动建表：{msg}"


def test_存量库_拒绝启动且不自动_stamp(scratch):
    eng, _ = scratch
    _fake_table(eng)
    with pytest.raises(SchemaVersionError) as e:
        assert_schema_ready(eng)
    msg = str(e.value)
    assert "alembic stamp" in msg, f"存量库的修复命令给错了：{msg}"
    assert "不自动 stamp" in msg, f"没说明为什么不自动 stamp：{msg}"
    # 真正要紧的一条：调用之后库里**仍然没有** alembic_version。
    # 一旦这里偷偷 stamp 成功，上面那段解释就成了空话，
    # 而错误的 stamp 会让后续 migration 在错误的结构上执行。
    assert current_revision(eng) is None, "居然自动 stamp 了 —— 这是最不能发生的那种自动"


def test_库比代码旧_让运维_upgrade(scratch, monkeypatch):
    """「库停在旧版本」—— 这是最常见、也最该被好好提示的一种情况。

    现在仓库里只有 baseline 一个 revision，所以构造不出一个**真实的**旧版本号。
    与其为了测试硬造一个空迁移（那等于往版本历史里塞假动作），
    不如把「已知版本集合」替换掉，单独验证这条分支的判断和提示 ——
    分支逻辑才是这里要守的东西，多一个假 revision 反而会让人误以为
    已经验证过「真实的降级 → 升级」这条路。
    """
    import app.core.schema_version as sv

    eng, _ = scratch
    monkeypatch.setattr(sv, "_known_revisions",
                        lambda: {"0001_baseline", "0000_真实存在的旧版本"})
    _stamp(eng, "0000_真实存在的旧版本")
    with pytest.raises(SchemaVersionError) as e:
        assert_schema_ready(eng)
    msg = str(e.value)
    assert "alembic upgrade head" in msg, f"修复命令不对：{msg}"
    assert "先备份" in msg, f"没提醒备份就改表结构：{msg}"


def test_库比代码新_提示方向而不是让_upgrade(scratch):
    """回滚代码但没回滚数据库，是最常见的触发方式。

    这时 ``alembic upgrade head`` 没用（head 就是当前值），得说清楚。
    """
    eng, _ = scratch
    _stamp(eng, "9999_未来的版本")
    with pytest.raises(SchemaVersionError) as e:
        assert_schema_ready(eng)
    msg = str(e.value)
    assert "回滚" in msg, f"没识别出这是代码回滚导致的版本超前：{msg}"


# ------------------------------------------------------ 静态守卫：启动不许建表

def _calls_named(tree, names: set[str]) -> set[str]:
    """找出真正被**调用**的这些名字。

    用 AST 而不是全文搜字符串：源码里到处都在解释「以前这里是 create_all」，
    那些注释必须不算数。全文搜会让这条用例一上来就红，而红的原因跟
    它要守的东西毫无关系 —— 守不住的守卫迟早被人注释掉。
    """
    hit = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in names:
                hit.add(f.attr)
            elif isinstance(f, ast.Name) and f.id in names:
                hit.add(f.id)
    return hit


def test_启动路径上不再有_DDL():
    """服务启动只校验版本，不建表、不改表。

    盯的是 ``create_all`` 这个**调用**本身 —— 审计说的「新增 NOT NULL 列在生产
    静默不生效」，根源就是它。只要 ``app/`` 下还有一处调用，这套机制就形同虚设。
    """
    banned = {"create_all", "drop_all", "init_database"}
    offenders = []
    for py in sorted((BACKEND / "app").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        hit = _calls_named(tree, banned)
        if hit:
            offenders.append(f"{py.relative_to(BACKEND)}: {sorted(hit)}")
    assert not offenders, ("app/ 下还有建表/删表的调用，启动路径因此不可信：\n"
                           + "\n".join(offenders))


def test_旧的_init_db模块已经删掉():
    assert not (BACKEND / "app" / "core" / "init_db.py").exists(), (
        "init_db.py 还在。它是「启动时自己建表」的旧实现，留着就等于"
        "留了一条随时可能被重新接上的后门")


def test_main_启动只校验版本():
    src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    start = src.index('@app.on_event("startup")')
    nxt = src.find("\n@app.", start)
    startup = src[start: nxt if nxt > 0 else len(src)]
    assert "assert_schema_ready" in startup, "启动路径没调 assert_schema_ready"
    # 同样走 AST：这段代码的注释里就写着 create_all，按字符串判会永远红
    hit = _calls_named(ast.parse(startup), {"create_all", "drop_all", "init_database"})
    assert not hit, f"启动路径里还有建表调用 {sorted(hit)} —— 版本闸门形同虚设"


def test_测试库是由迁移建出来的():
    """conftest 必须走 alembic upgrade head，而不是 create_all。

    守的是「别为了让测试跑通又退回 create_all」—— 那会让整套测试失去对
    baseline 迁移的验证能力，而这种退回往往是为了图省事。
    """
    src = (BACKEND / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "upgrade_to_head" in src, "conftest 不走迁移了"
    assert "init_database" not in src, "conftest 又用回 create_all 建库了"


# --------------------------------------- 测试库本身处于 head，且护栏都在

def test_测试库版本等于_head(db):
    assert current_revision(engine) == head_revision()


def test_会计护栏触发器确实装上了(db):
    """baseline 迁移最容易漏的就是 db_init.sql —— 那些触发器不在 ORM 元数据里。

    漏了不会让任何一条普通用例变红：借贷平衡、结账禁写这些护栏都是在
    **触犯**时才报错，没触犯时一切正常。所以这里单独数一遍。
    """
    rows = db.execute(text(
        "SELECT trigger_name FROM information_schema.triggers "
        "WHERE trigger_schema = current_schema()")).scalars().all()
    names = {n for r in rows for n in ([r] if isinstance(r, str) else r)}
    required = {"trg_entry_balance", "trg_voucher_balance", "trg_entry_lock_guard",
                "trg_posted_voucher_guard", "trg_entry_sync", "trg_voucher_resync"}
    missing = required - names
    assert not missing, f"护栏触发器缺失：{sorted(missing)}"
