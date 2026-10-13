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
                                     base_revision, current_revision,
                                     head_revision)
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


def test_存量库_stamp_到最早那版_不是_head(scratch):
    """``stamp head`` 会让之后的 revision 一次都不跑 —— 那就是 P3-01 本人的行为。

    所以提示语里给的必须是 base（0001_baseline），并且要跟着 ``upgrade head``；
    只给 stamp 一条命令的话，运维做完还是停在「版本对了但结构不对」的状态，
    而且再也看不出来。
    """
    eng, _ = scratch
    _fake_table(eng)
    with pytest.raises(SchemaVersionError) as e:
        assert_schema_ready(eng)
    msg = str(e.value)

    base = base_revision()
    head = head_revision()
    assert base and head and base != head, (
        f"这条用例要求 base({base}) != head({head})；"
        f"只有一条 revision 时谈「stamp 到哪」是没有意义的")
    assert f"alembic stamp {base}" in msg, f"没让运维 stamp 到最早的 {base}：{msg}"
    assert f"alembic upgrade head" in msg, f"stamp 之后没让继续 upgrade：{msg}"
    assert f"alembic stamp {head}" not in msg, (
        f"居然建议 stamp head —— 那等于断言这个库已经是最新的，"
        f"之后的 migration 全部被跳过：{msg}")
    # 得解释清楚为什么不是 head，否则运维会理所当然地改成 stamp head
    assert "静默不生效" in msg, f"没解释 stamp head 的后果：{msg}"


def test_base_是真实存在的_最老_revision():
    """base_revision() 不是常量，必须真的来自仓库里那份 revision 目录。"""
    from app.core.schema_version import _known_revisions

    known = _known_revisions()
    base = base_revision()
    assert base in known, f"base {base} 不在 {sorted(known)} 里 —— 这条建议是假的"


def test_库比代码旧_让运维_upgrade(scratch):
    """「库停在旧版本」—— 这是最常见、也最该被好好提示的一种情况。

    用的就是仓库里真实的旧 revision，不造假版本号：这条分支的意义在于
    「库落后时给的提示对不对」，拿一个代码里根本没见过的版本号去测它，
    走的是另一条分支（见下面 test_库比代码新）。
    """
    eng, _ = scratch
    _stamp(eng, base_revision())
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


# ------------------------------------------------- 0003：存量库收敛，且幂等

def test_期初外键是_RESTRICT_不是_CASCADE(db):
    """P1-05 的护栏：删科目不能连带删掉期初余额。

    ``ON DELETE CASCADE`` 在这里是**静默数据损失** —— 删一个只被期初引用、
    没有凭证引用的科目，期初行会一起消失，余额漂移，事后没有任何线索。
    这里查的是数据库里的事实，不是 ORM 声明：ORM 写着 RESTRICT 和库里
    真的是 RESTRICT 是两件事（P3-01 的整个教训）。
    """
    row = db.execute(text("""
        SELECT c.confdeltype::text
          FROM pg_constraint c
          JOIN pg_attribute a
            ON a.attrelid = c.conrelid
           AND a.attnum = ANY (c.conkey)
           AND a.attname = 'subject_id'
         WHERE c.conrelid = 'period_opening'::regclass
           AND c.contype = 'f'
    """)).scalar()
    assert row == "r", (
        f"period_opening.subject_id 的外键删除动作是 {row!r}，不是 RESTRICT(r) —— "
        f"删科目会连带删掉期初余额")


def test_期初外键约束名和_db_init_一致(db):
    """两条改这条外键的路径（baseline 重放 db_init.sql / 0003）必须留下同一个名字。

    名字不一致不代表功能坏，但会让「这条约束是谁建的」再也查不清 ——
    而「每处结构变化都有出处」正是 P3-01 想要的结果。
    """
    names = set(db.execute(text(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'period_opening'::regclass AND contype = 'f'"
    )).scalars().all())
    assert "fk_period_opening_subject" in names, (
        f"约束名对不上 db_init.sql 里 P1-05 那段 DO 块：{sorted(names)}")


def test_period_ord_是_integer(db):
    """模型是 ``Integer``，库里就不能是 ``smallint``。

    ord = (year - start_year) * 12 + month，理论上永远用不满 smallint 的
    32767 —— 但「理论够用」不是理由。结构必须和模型一致，否则下一轮
    「改类型」又会留下同样的化石，而这类差异只有逐项对比才能发现。
    """
    t = db.execute(text("""
        SELECT format_type(atttypid, atttypmod) FROM pg_attribute
         WHERE attrelid = 'period'::regclass AND attname = 'ord' AND attnum > 0
    """)).scalar()
    assert t == "integer", f"period.ord 的类型是 {t}，不是 integer"


def test_0003_重复执行不改变结构(db, monkeypatch):
    """幂等：已经收敛的库再跑一次 0003，结构不该有任何变化。

    必要性在于运维会重跑。不幂等的 migration 重跑一次就多一个约束，
    而这类问题往往在很久以后才以「基线对不上」的形式冒出来。
    """
    rev = _load_0003()
    # 不关连接：连接归 session 所有，由 db 夹具的 rollback 收尾。
    # 这里 with 出去的话 session 就再也 rollback 不回来了。
    conn = db.connection()
    monkeypatch.setattr(rev, "op", _OpShim(conn))

    before = _converge_snapshot(conn)
    rev.upgrade()
    assert _converge_snapshot(conn) == before, "跑了一次 0003 就把结构改动了"

    rev.upgrade()
    assert _converge_snapshot(conn) == before, "0003 不幂等：第二次执行改动了结构"


def test_0003_能把_CASCADE_收敛成_RESTRICT(scratch, monkeypatch):
    """正向验证：拿一个「create_all 时代」的库结构，0003 之后应该和 baseline 一致。

    只测「已经是目标形态时不动」是不够的 —— 那证明不了它真的会把错的改对。
    这里在 scratch schema 里摆出开发库当年那个状态（小int + CASCADE），
    跑一遍 0003，再逐项比。
    """
    eng, _ = scratch
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE subject (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE period (id INTEGER PRIMARY KEY, ord SMALLINT NOT NULL)")
        conn.exec_driver_sql(
            "CREATE TABLE period_opening (id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL, "
            "CONSTRAINT period_opening_subject_id_fkey FOREIGN KEY (subject_id) "
            "REFERENCES subject(id) ON DELETE CASCADE)")

    rev = _load_0003()
    with eng.connect() as c:
        monkeypatch.setattr(rev, "op", _OpShim(c))
        rev.upgrade()
        act = c.execute(text("""
            SELECT confdeltype::text FROM pg_constraint
             WHERE conrelid = 'period_opening'::regclass AND contype = 'f'
        """)).scalar()
        typ = c.execute(text("""
            SELECT format_type(atttypid, atttypmod) FROM pg_attribute
             WHERE attrelid = 'period'::regclass AND attname = 'ord' AND attnum > 0
        """)).scalar()
    assert act == "r", f"外键没收敛到 RESTRICT，还是 {act!r}"
    assert typ == "integer", f"period.ord 没收敛到 integer，还是 {typ!r}"


def test_0003_能把_同名不同动作_收敛成_RESTRICT(scratch, monkeypatch):
    """P3-2：约束**名字对、删除动作错**，再跑一次必须收敛。

    这是 0003 原先真正不幂等的地方。旧判据只比名字，于是遇到
    ``fk_period_opening_subject`` 存在但动作是 CASCADE 的存量状态时，
    它跳过 DROP，紧接着 ``ADD CONSTRAINT`` 用同一个名字再建一条 ——
    upgrade 直接在重名上失败。「在」不等于「对」：这条外键在 create_all 时代
    一直都在，只是名字和动作可能不对，判据必须把两者都算进去。

    上一批只钉了 db_init.sql 里那段按**列**收敛的 DO 块，没钉 0003 本体
    （文件不归那条工作线）。现在 0003 也改了，这里补上正向验证。
    """
    eng, _ = scratch
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE subject (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE period (id INTEGER PRIMARY KEY, ord INTEGER NOT NULL)")
        conn.exec_driver_sql(
            "CREATE TABLE period_opening (id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL, "
            # 名字已经是目标名，**只有删除动作是错的** —— 旧写法在这里会崩
            "CONSTRAINT fk_period_opening_subject FOREIGN KEY (subject_id) "
            "REFERENCES subject(id) ON DELETE CASCADE)")
    with eng.connect() as c:
        rev = _load_0003()
        monkeypatch.setattr(rev, "op", _OpShim(c))
        rev.upgrade()
        act = tuple(c.execute(text(
            "SELECT conname || ':' || confdeltype::text FROM pg_constraint "
            "WHERE conrelid = 'period_opening'::regclass AND contype = 'f'"
        )).scalars().all())
    assert act == ("fk_period_opening_subject:r",), (
        f"同名不同动作的外键没有被收敛，现在是 {act}（CASCADE 会连带删掉期初余额）")


class _OpShim:
    """``alembic.op`` 的替身，只提供 0003 用到的那一个方法。

    0003 的 upgrade 里除了 ``op.get_bind()`` 没有别的 op 调用（它写的都是
    手写 ALTER，因为「把 CASCADE 改成 RESTRICT 并重命名」没有 op 级原语）。
    所以这里不需要伪造 MigrationContext —— 换成真 context 反而会把用例
    的重点（幂等、收敛）淹没在 alembic 内部机制里。
    """

    def __init__(self, conn):
        self._conn = conn

    def get_bind(self):
        return self._conn


def _load_0003():
    """按路径加载 0003。

    不用 ``import migrations.versions.0003_converge_legacy``：revision 的文件名
    以数字开头，不是合法标识符，import 语句写不出来。
    """
    import importlib.util

    path = BACKEND / "migrations" / "versions" / "0003_converge_legacy.py"
    spec = importlib.util.spec_from_file_location("rev_0003", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _converge_snapshot(conn) -> tuple:
    """0003 关心的那部分结构。"""
    def rows(table):
        return tuple(sorted(conn.execute(text(
            f"SELECT conname || ':' || confdeltype::text FROM pg_constraint "
            f"WHERE conrelid = '{table}'::regclass")).scalars().all()))

    return (
        rows("period_opening"),
        rows("period"),
        conn.execute(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                          "WHERE attrelid='period'::regclass AND attname='ord' "
                          "AND attnum>0")).scalar(),
    )


# ------------------------------------- 静态守卫：脚本别和模型脱节

def test_脚本里构造_ORM_的字段名都真实存在():
    """``Book(year=...)`` 这类过时字段名，只在**运行时**才炸。

    而且炸的位置很坏：``scripts/init_db.py --reset`` 是先 ``DROP SCHEMA`` 再播种，
    字段名一错就是「库已经清空、数据一条没建」，而 README 里恰好写着这条命令。

    守的是「脚本和模型脱节」这一整类问题 —— 和 P3-01 是同一个病根：
    约束写在某个地方，真跑起来时却没人核对它还对不对。
    """
    import app.models  # noqa: F401  确保所有模型都已注册
    from app.models.base import Base

    # registry 里有全部已注册模型，包括多继承/混入的那些；
    # __subclasses__() 只到第一层，会漏。
    registry = {
        mapper.class_.__name__: {a.key for a in mapper.attrs}
        for mapper in Base.registry.mappers
    }

    offenders = []
    for py in sorted((BACKEND / "scripts").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            # 只看 Book(...)、SysUser(...) 这种直接构造，不追别名/工厂
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            fields = registry.get(node.func.id)
            if fields is None:
                continue
            for kw in node.keywords:
                if kw.arg and kw.arg not in fields:
                    offenders.append(
                        f"{py.name}:{node.lineno} {node.func.id}({kw.arg}=...) "
                        f"—— 模型里没有这个字段")

    assert not offenders, ("脚本里构造 ORM 用了不存在的字段，跑起来才炸：\n"
                           + "\n".join(offenders))


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

    **这份清单必须穷举，而且它曾经真的漏过一项**（审计 P1-9 / P2-33）：
    ``trg_subject_leaf_guard`` 是 cd1cded 加进 db_init.sql 的，没有配套
    revision，于是它在任何跑过 0001 的库上都不存在 —— 而这份清单当时
    恰好没列它，所以 CI 全绿。清单漏项与护栏没上线是同一个问题的两面：
    没有清单就没人知道漏了，清单不全就等于没有。

    因此新增/修改任何数据库护栏时，**三件事一起做**：
    改 ``app/core/db_init.sql``、新写一条 revision 把��交付给存量库、
    在下面这个集合里登记。第三件漏了，前两件就等于没做。
    """
    rows = db.execute(text(
        "SELECT trigger_name FROM information_schema.triggers "
        "WHERE trigger_schema = current_schema()")).scalars().all()
    names = {n for r in rows for n in ([r] if isinstance(r, str) else r)}
    required = {
        # 借贷平衡（延迟到提交时校验）
        "trg_entry_balance", "trg_voucher_balance",
        # 结账禁写：三处守卫必须齐全，缺一处就多一条绕过路径（P1-2）
        "trg_entry_lock_guard", "trg_voucher_guard", "trg_voucher_delete_guard",
        # 凭证状态与分录冗余列：两个方向都要有（P2-05）
        "trg_posted_voucher_guard", "trg_entry_sync", "trg_voucher_resync",
        # 科目：编码/类别不可变、is_leaf 自洽（被改行两个方向 + 父节点侧延迟校验，P2-04）
        "trg_subject_guard", "trg_subject_leaf_guard",
        "trg_subject_leaf_parent_guard",
        # 期初只读
        "trg_opening_guard",
        # 触发器改写行时也刷新 updated_at（P2-09）
        "trg_updated_at",
    }
    missing = required - names
    assert not missing, f"护栏触发器缺失：{sorted(missing)}"

    # 反过来也钉一下：**只**装了这几条就收工，等于有人把 db_init.sql 里的
    # 触发器删了而没人发现。集合相等比「必需的都在」更难蒙混。
    assert not (names - required), (
        f"出现了清单之外的触发器：{sorted(names - required)} —— "
        "要么是新加的护栏忘了登记（那正是 P2-33 的复发路径），要么是被删剩的残留")
