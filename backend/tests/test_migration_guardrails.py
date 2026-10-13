"""迁移链与数据库层护栏（审计 P1-9 / P2-33 及其同批条目）。

**这份文件要钉的不是某个 bug，而是「护栏怎么到达存量库」这件事本身。**

前两轮审计的所有修复都落在应用层。库级护栏只存在于
``app/core/db_init.sql``，而它唯一的读者是 ``0001_baseline`` —— 只跑一次。
于是「往 db_init.sql 加一条触发器」这个被 ``ARCHITECTURE.md`` §11 明确
推荐的做法，在**任何跑过 0001 的库上都不生效**，而 CI 全绿（conftest 每次
都在全新库上建表）。``cd1cded`` 加的叶子护栏就是这样在生产上从未上线的，
唯一能发现这件事的触发器清单还恰好漏了它。

所以这里刻意分成三层：

1. **存量库升级实测**（``test_存量库升级后…``）：拿一个 schema 真的从
   0001 建到 head，再把护栏打回 P1-09 之前的样子、stamp 回 0004，
   然后重跑 upgrade —— 断言触发器**真的出现了**。这是 P2-33 说
   「永远不会出现」的那件事，必须被一条会红的用例盯着。
2. **行为断言**：护栏覆盖了哪些路径（INSERT / 两个方向 / 冗余列漂移）。
3. **防回归机制**：模型与库的外键零差异（挡住 P1-10 的 autogenerate 回退）、
   0005 幂等、索引到位。

**依赖两个独立连接的地方（并发那几条）不能用 ``db`` 夹具** ——
它自己占着一个连接并持有事务，会把被测的锁序搅乱。所以并发那几条
自己建数据、自己开连接。
"""
from __future__ import annotations

import importlib.util
import pathlib
import threading
import time

import pytest
from sqlalchemy import create_engine, text

from app.core.db import engine
from app.core.schema_version import head_revision
from tests.conftest import TEST_DB_URL

BACKEND = pathlib.Path(__file__).resolve().parents[1]

LEAF_GUARD = "trg_subject_leaf_guard"
PARENT_LEAF_GUARD = "trg_subject_leaf_parent_guard"


# ---------------------------------------------------------------- 迁移级夹具


def _alembic_config():
    from app.core.schema_version import _config

    return _config()


def _upgrade(eng, target: str = "head") -> None:
    """和 ``app.core.schema_version.upgrade_to_head`` 同一条路径。

    不直接复用它是因为要指定 target（重放某一条 revision）；
    事务的处理方式必须一致，否则测的就不是部署真正走的那条路了。
    """
    from alembic import command

    cfg = _alembic_config()
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        with conn.begin():
            command.upgrade(cfg, target)


def _stamp(eng, rev: str) -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": rev})


def _load_rev(name: str):
    """按路径加载 revision —— 文件名以数字开头，import 语句写不出来。"""
    path = BACKEND / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def migrated_scratch():
    """一个**从迁移建出来的**独立 schema，当「另一个数据库」。

    为什么不新建真库：``0001_baseline`` 会顺带装一遍内置报表模板，
    每个用例都建一遍要多花几秒；而这几条用例真正要的是
    「一个由 0001–0006 建出来的、我可以随便摆布的库」。

    search_path **只**留它自己 —— 写成 ``scratch,public`` 的话它能看见主
    schema 的全部表和 alembic_version，所谓的临时库根本就是主库，
    用例全部通过而什么都没测到（``test_schema_version.py`` 里踩过这个坑）。
    """
    name = "scratch_mg"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        conn.exec_driver_sql(f"CREATE SCHEMA {name}")
    eng = create_engine(
        TEST_DB_URL, connect_args={"options": f"-c search_path={name}"})
    _upgrade(eng)
    yield eng, name
    eng.dispose()
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _triggers(eng, table: str) -> set[str]:
    with eng.connect() as conn:
        return set(conn.execute(text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = to_regclass(:t) AND NOT tgisinternal"),
            {"t": table}).scalars())


# ================================================================ 1. 存量库升级
#
# 这是整份文件里最要紧的部分：它测的是「护栏能不能到达存量库」，
# 而不是「护栏在全新库上有没有」。后者 conftest 每天都在间接验证，
# 而生产库永远走不到那条路径。


def _downgrade_leaf_guard_to_pre_fix_state(eng) -> None:
    """把叶子护栏还原成 cd1cded 当时的形态。

    cd1cded 加的是：``BEFORE UPDATE`` + 只挡 ``false -> true``，而且
    没有配套 revision。真实存量库就是这个状态 —— 触发器名字在，
    函数体是旧的，延迟到提交时判的那条压根不存在。
    """
    with eng.begin() as conn:
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_updated_at ON subject")
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {LEAF_GUARD} ON subject")
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {PARENT_LEAF_GUARD} ON subject")
        conn.exec_driver_sql("DROP FUNCTION IF EXISTS fn_subject_leaf_parent_guard()")
        # cd1cded 那版函数体：只看 TG_OP='UPDATE' 且 false->true
        conn.exec_driver_sql("""
            CREATE OR REPLACE FUNCTION fn_subject_leaf_guard() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' AND NEW.is_leaf AND NOT OLD.is_leaf THEN
                    IF EXISTS (SELECT 1 FROM subject c WHERE c.parent_id = NEW.id) THEN
                        RAISE EXCEPTION '旧版护栏（只挡 false 到 true）'
                            USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
                RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
        conn.exec_driver_sql(
            f"CREATE TRIGGER {LEAF_GUARD} BEFORE UPDATE ON subject "
            "FOR EACH ROW EXECUTE FUNCTION fn_subject_leaf_guard()")
        # 结构也一并打回旧形态，验证 0005 不只是补触发器
        conn.exec_driver_sql("ALTER TABLE entry DROP CONSTRAINT IF EXISTS fk_entry_book_period")
        conn.exec_driver_sql("ALTER TABLE entry ALTER COLUMN period_ord TYPE smallint")
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_entry_book_period")
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_period_opening_book")


@pytest.mark.parametrize("trg", ["trg_updated_at", PARENT_LEAF_GUARD])
def test_存量库升级后_新护栏真的出现(migrated_scratch, trg):
    """P1-09 / P2-33 的正面验证：模拟「已跑过 0001–0004 的旧库」再升级。

    做法是**把护栏真的打回旧形态**而不是只 stamp 一个版本号 ——
    stamp 只改版本号，不改结构，而真实存量库是「版本号停在 0004、
    结构也停在 0004 那一刻」的状态。两者必须同时复原，否则测的是一个
    不存在的场景。

    这两条在旧库里**根本不存在**（叶子护栏是 cd1cded 加进 db_init.sql 的，
    没配套 revision；父节点那条与 updated_at 那条是这次才加的），所以断言
    是「升级前不在、升级后在」。
    """
    eng, _ = migrated_scratch
    _downgrade_leaf_guard_to_pre_fix_state(eng)
    _stamp(eng, "0004_token_version")

    before = _triggers(eng, "subject")
    assert trg not in before, (
        f"前置没搭对：{trg} 在「旧库」里本来就存在，这条用例什么也没证明")
    _upgrade(eng, "head")
    after = _triggers(eng, "subject")
    assert trg in after, (
        f"升级到 head 之后 {trg} 仍然不在 subject 上 —— "
        "这就是 P2-33 说的「永远不会出现」。"
        f"库里现有：{sorted(after)}")


def test_存量库升级后_叶子护栏函数体换成新版(migrated_scratch):
    """同名触发器 + 旧函数体，是最阴的一种「交付成功」。

    旧库里 ``trg_subject_leaf_guard`` 名字**在**（cd1cded 加的），
    函数体却是只挡 ``false -> true`` 的那一版。只写 ``CREATE TRIGGER``
    而不 ``CREATE OR REPLACE FUNCTION`` 的迁移，会让旧函数体原封不动留着
    —— 触发器在、护栏还是漏的。所以这条断言的是**函数体**，不是名字。
    """
    eng, _ = migrated_scratch
    _downgrade_leaf_guard_to_pre_fix_state(eng)
    _stamp(eng, "0004_token_version")
    with eng.connect() as conn:
        legacy = conn.execute(text(
            "SELECT p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = 'fn_subject_leaf_guard' "
            "AND n.nspname = current_schema()"
        )).scalar()
    assert "旧版护栏" in legacy, "前置没搭对：旧函数体没搭上去"

    _upgrade(eng, "head")
    with eng.connect() as conn:
        body = conn.execute(text(
            "SELECT p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = 'fn_subject_leaf_guard' "
            "AND n.nspname = current_schema()"
        )).scalar()
    assert "旧版护栏" not in body, "升级后叶子护栏的函数体还是 cd1cded 那版"
    assert "TG_OP = 'UPDATE' AND NEW.is_leaf AND NOT OLD.is_leaf" not in body, (
        "升级后叶子护栏仍然只看 false->true 这一个方向（P2-04 未修）")


def test_存量库升级后结构也一并收敛(migrated_scratch):
    """结构类改动（组合外键、列类型、索引）同样必须到达存量库。"""
    eng, _ = migrated_scratch
    _downgrade_leaf_guard_to_pre_fix_state(eng)
    _stamp(eng, "0004_token_version")
    _upgrade(eng, "head")

    with eng.connect() as conn:
        typ = conn.execute(text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'entry'::regclass AND attname = 'period_ord' "
            "AND attnum > 0")).scalar()
        idx = set(conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE indexname IN "
            "('ix_entry_book_period', 'ix_period_opening_book')")).scalars())
        fk = conn.execute(text(
            "SELECT confdeltype::text FROM pg_constraint "
            "WHERE conrelid = 'entry'::regclass AND conname = 'fk_entry_book_period'"
        )).scalar()
    assert typ == "integer", f"entry.period_ord 还是 {typ}，存量库没收到 P2-08 的收敛"
    assert idx == {"ix_entry_book_period", "ix_period_opening_book"}, (
        f"P2-06 的索引没到位：{sorted(idx)}")
    assert fk == "c", "entry 冗余列的组合外键没到位（或删除动作不对）"


def test_0005_能把_同名不同动作_的外键收敛(migrated_scratch):
    """P3-2 的形态：约束**名字对、删除动作错**，再跑一次必须收敛。

    0003 的幂等性漏洞就在这里：它按「名字相等」决定不 DROP，紧接着
    ``ADD CONSTRAINT`` 同名约束，于是 upgrade 直接失败。
    0003 本体不在本工作线的归属范围内，但 db_init.sql 里那段 DO 块
    按**列**找外键而不是按名字，对任何状态都收敛 —— 这里钉的是它。
    """
    eng, _ = migrated_scratch
    with eng.begin() as conn:
        # 造出「同名但动作错」：先删正确的，再建一个同名 CASCADE 的
        conn.exec_driver_sql(
            "ALTER TABLE period_opening DROP CONSTRAINT fk_period_opening_subject")
        conn.exec_driver_sql(
            "ALTER TABLE period_opening ADD CONSTRAINT fk_period_opening_subject "
            "FOREIGN KEY (subject_id) REFERENCES subject(id) ON DELETE CASCADE")
    _stamp(eng, "0004_token_version")
    _upgrade(eng, "head")

    with eng.connect() as conn:
        act = conn.execute(text(
            "SELECT confdeltype::text FROM pg_constraint "
            "WHERE conrelid = 'period_opening'::regclass "
            "AND conname = 'fk_period_opening_subject'")).scalar()
    assert act == "r", (
        f"同名不同动作的外键没被收敛，删除动作还是 {act!r}（CASCADE 会连带删掉期初）")


def test_重复_upgrade_head_两次_结构不变(migrated_scratch):
    """幂等（审计 P3-2 的验收口径）：运维会重跑，重跑不该动结构。

    钉的是**整条链**而不只是某一条 revision：``upgrade head`` 是运维真正会敲的
    那条命令，重复执行必须零变化。这里先跑一次 head、再跑两次，逐次比对
    触发器 / 约束 / 列默认值三份快照。

    上一批只验了 0005 的 ``upgrade()`` 本体，没验整条链 —— 而 P3-2 的病灶
    恰恰发生在「链上某一条对某个存量状态不幂等」时，只验单条测不出来。
    """
    eng, _ = migrated_scratch

    def snapshot() -> tuple:
        with eng.connect() as conn:
            return (
                tuple(sorted(conn.execute(text(
                    "SELECT tgname || '@' || tgrelid::regclass::text FROM pg_trigger "
                    "WHERE NOT tgisinternal")).scalars())),
                tuple(sorted(conn.execute(text(
                    "SELECT conname || '@' || conrelid::regclass::text FROM pg_constraint "
                    "WHERE conname NOT LIKE '%%\\_fkey'")).scalars())),
                tuple(sorted(conn.execute(text(
                    "SELECT table_name || '.' || column_name || '=' || "
                    "coalesce(column_default, '-') FROM information_schema.columns "
                    "WHERE table_schema = current_schema()")).scalars())),
            )

    _upgrade(eng, "head")      # 已在 head；这次必须什么都不做
    first = snapshot()
    _upgrade(eng, "head")      # 第二次
    assert snapshot() == first, "重复 upgrade head 改动了结构 —— 迁移不幂等"
    _upgrade(eng, "head")      # 第三次
    assert snapshot() == first, "第三次 upgrade head 改动了结构"


def test_0005_重复执行不改变结构(migrated_scratch):
    """幂等：运维会重跑。不幂等的迁移重跑一次就多一个约束。

    在**独立 schema + 独立引擎**上直接调 ``upgrade()``：0005 的全部动作
    都是 DDL，而 PG 的 DDL 是事务性的，跑完把事务回滚就等于什么都没发生，
    所以连续跑两次再比对结构，中间不需要任何清理。
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    eng, name = migrated_scratch
    rev = _load_rev("0005_db_guardrails.py")
    _stamp(eng, "0004_token_version")

    def snapshot() -> tuple:
        with eng.connect() as conn:
            return (
                tuple(sorted(conn.execute(text(
                    "SELECT tgname || '@' || tgrelid::regclass::text FROM pg_trigger "
                    "WHERE NOT tgisinternal")).scalars())),
                tuple(sorted(conn.execute(text(
                    "SELECT conname || '@' || conrelid::regclass::text FROM pg_constraint "
                    "WHERE conname NOT LIKE '%%\\_fkey'")).scalars())),
                tuple(sorted(conn.execute(text(
                    "SELECT table_name || '.' || column_name || '=' || "
                    "coalesce(column_default, '-') FROM information_schema.columns "
                    "WHERE table_schema = current_schema()")).scalars())),
            )

    with eng.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            rev.op = Operations(ctx)
            rev.upgrade()
        ctx2 = MigrationContext.configure(conn)
        with ctx2.begin_transaction():
            rev.op = Operations(ctx2)
            rev.upgrade()   # 第二次：必须什么都没变
        before = snapshot()
        ctx3 = MigrationContext.configure(conn)
        with ctx3.begin_transaction():
            rev.op = Operations(ctx3)
            rev.upgrade()   # 第三次
        assert snapshot() == before, "0005 不幂等：重复执行改动了结构"
    assert name


# ================================================================ 2. 护栏覆盖的路径


def _subject(db, book_id, code, *, parent=None, is_leaf=True):
    db.execute(text(
        "INSERT INTO subject (book_id, code, name, parent_id, level, category, "
        "direction, is_leaf, ext_attrs, created_at, updated_at) VALUES "
        "(:b, :c, :c, :p, :l, 'ASSET', 'DEBIT', :leaf, '{}'::jsonb, now(), now())"),
        {"b": book_id, "c": code, "p": parent, "l": 2 if parent else 1,
         "leaf": is_leaf})
    return db.execute(text("SELECT id FROM subject WHERE book_id=:b AND code=:c"),
                      {"b": book_id, "c": code}).scalar()


def _leaf_subject_ids(db, book_id, n: int = 2) -> list[int]:
    return list(db.execute(text(
        "SELECT id FROM subject WHERE book_id = :b AND is_leaf ORDER BY code LIMIT :n"),
        {"b": book_id, "n": n}).scalars())


def _balanced_voucher(db, book_id, *, no: str, day: int = 15,
                      stale: bool = False) -> int:
    """造一张**借贷平衡**的凭证 —— trg_entry_balance 是延迟约束触发器，
    提交时才发现不平衡，那时错误信息与真正的病因隔着十万八千里。

    ``stale=True`` 把两张时间戳都往前推一天，好让 updated_at 的变化看得出来。
    """
    when = "now() - interval '1 day'" if stale else "now()"
    vid = db.execute(text(
        "INSERT INTO voucher (book_id, period_id, voucher_no, voucher_date, "
        f"status, source, created_at, updated_at) "
        "VALUES (:b, (SELECT min(id) FROM period WHERE book_id = :b), :no, "
        f"DATE '2026-01-{day:02d}', 'UNPOSTED', 'MANUAL', {when}, {when}) "
        "RETURNING id"),
        {"b": book_id, "no": no}).scalar()
    s1, s2 = _leaf_subject_ids(db, book_id, 2)
    db.execute(text(
        "INSERT INTO entry (voucher_id, book_id, period_ord, line_no, subject_id, "
        f"direction, amount, created_at, updated_at) VALUES "
        f"(:v, :b, 1, 1, :s1, 'DEBIT', 100, {when}, {when}), "
        f"(:v, :b, 1, 2, :s2, 'CREDIT', 100, {when}, {when})"),
        {"v": vid, "b": book_id, "s1": s1, "s2": s2})
    db.commit()
    return vid


def test_叶子护栏_挡住了_true到true_这个漏网方向(db, book):
    """P2-04 的方向一：旧写法只挡 ``false -> true``。

    「已经错了之后再改个别的东西」会把矛盾原样带着走一遍 —— 这在真实
    数据修复脚本里非常常见（改个名称、调下排序），而旧护栏放行。
    """
    from sqlalchemy.exc import DBAPIError

    parent = _subject(db, book.id, "1900", is_leaf=False)
    child = _subject(db, book.id, "1901", parent=parent)
    db.commit()

    # 造出「is_leaf=true 却有下级」的存量脏状态（这正是 P1-01 之前的老库会有的）。
    # 要造出来就得先把叶子护栏关掉 —— 否则它会正确地拦下我们自己的造脏动作。
    db.execute(text("ALTER TABLE subject DISABLE TRIGGER trg_subject_leaf_guard"))
    db.commit()
    db.execute(text("UPDATE subject SET is_leaf = true WHERE id = :p"), {"p": parent})
    db.commit()
    db.execute(text("ALTER TABLE subject ENABLE TRIGGER trg_subject_leaf_guard"))
    db.commit()

    # 现在是「已经错了」的库状态。改个名字 —— 一个不该改错的状态被带了过去。
    with pytest.raises(DBAPIError, match="is_leaf"):
        db.execute(text("UPDATE subject SET name = '改个名' WHERE id = :p"),
                   {"p": parent})
        db.commit()
    db.rollback()
    assert child


def test_插子科目不动父节点_提交时被拒(db, book):
    """P2-04 收尾的那一刀：上一版触发器只管**被改的那一行**。

    「插一个子科目、父节点一行都不碰」这条路径下，子行自己 is_leaf=true 是对的，
    所以 ``trg_subject_leaf_guard`` 不会响；而父节点「有下级却仍是末级」这个
    矛盾**确实已经产生了**。上一批把对应的约束触发器写出来又测通过，却因为
    既有夹具会红而没有上线；夹具改完之后，这一版把它送上了。

    错误必须落在**提交**上（DEFERRABLE INITIALLY DEFERRED），因为真实的创建
    路径是「先插子行、再把父级改成非末级」，同一事务内后一步才能修好前一步。
    """
    from sqlalchemy.exc import DBAPIError

    parent = _subject(db, book.id, "1900", is_leaf=True)
    db.commit()
    db.execute(text("INSERT INTO subject (book_id, code, name, parent_id, level, "
                    "category, direction, is_leaf, ext_attrs, created_at, updated_at) "
                    "VALUES (:b, '1901', '1901', :p, 2, 'ASSET', 'DEBIT', true, "
                    "'{}'::jsonb, now(), now())"),
               {"b": book.id, "p": parent})
    with pytest.raises(DBAPIError, match="末级"):
        db.commit()
    db.rollback()
    assert db.execute(text("SELECT count(*) FROM subject WHERE book_id = :b AND code = '1901'"),
                      {"b": book.id}).scalar() == 0, "被拒之后子科目不该留在库里"


def test_同一事务里改父节点_不挡(db, book):
    """护栏不能变成「建不了子科目」。

    写成 BEFORE / IMMEDIATE 的话，api/subjects.py 的创建路径（先插子行、
    后把父级改成非末级）自己就先报错 —— 那就等于把护栏变成了功能缺失。
    延迟到提交时判，正是为了让同事务内的修复有机会生效。
    """
    parent = _subject(db, book.id, "1900", is_leaf=True)
    db.commit()
    db.execute(text("INSERT INTO subject (book_id, code, name, parent_id, level, "
                    "category, direction, is_leaf, ext_attrs, created_at, updated_at) "
                    "VALUES (:b, '1901', '1901', :p, 2, 'ASSET', 'DEBIT', true, "
                    "'{}'::jsonb, now(), now())"),
               {"b": book.id, "p": parent})
    db.execute(text("UPDATE subject SET is_leaf = false WHERE id = :p"), {"p": parent})
    db.commit()
    rows = db.execute(text("SELECT code, is_leaf FROM subject WHERE book_id = :b "
                           "AND code IN ('1900', '1901') ORDER BY code"),
                      {"b": book.id}).all()
    assert rows == [("1900", False), ("1901", True)], rows


def test_父节点护栏不挡_挂到本来就非末级的父级(db, book):
    parent = _subject(db, book.id, "1900", is_leaf=False)
    db.commit()
    _subject(db, book.id, "1901", parent=parent)
    db.commit()
    assert db.execute(text("SELECT count(*) FROM subject WHERE book_id = :b"),
                      {"b": book.id}).scalar() >= 1



def test_叶子护栏_不挡_只是改名(db, book):
    """护栏不能变成「这个科目从此不能动」。

    一个没有下级、is_leaf=true 的科目，改名/改排序必须照常成功 ——
    否则这条守卫就从「挡错账」变成了「制造不便」，而那正是原设计明确
    不挡的方向（「拦错账方向而不拦不方便方向」）。
    """
    sid = _subject(db, book.id, "1900", is_leaf=True)
    db.commit()
    db.execute(text("UPDATE subject SET name = '改名了', sort_no = 5 WHERE id = :p"),
               {"p": sid})
    db.commit()
    assert db.execute(text("SELECT name FROM subject WHERE id = :p"),
                      {"p": sid}).scalar() == "改名了"


def test_三处结账守卫都改成了_读加锁(db):
    """P1-2：裸 SELECT 在 READ COMMITTED 下读到的是快照，不是「此刻的事实」。

    三处守卫必须**同时**换成 ``FOR SHARE``：任何一处退回裸 SELECT，
    它就重新变成一条绕过路径，而另外两处的修复会显得「已经做过了」。
    """
    with engine.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT proname, prosrc FROM pg_proc WHERE proname IN "
            "('fn_voucher_guard', 'fn_voucher_delete_guard', 'fn_entry_lock_guard')"
        )).all())
    assert set(rows) == {"fn_voucher_guard", "fn_voucher_delete_guard",
                         "fn_entry_lock_guard"}, (
        f"少了一处结账守卫：{sorted(rows)}")
    for name, src in rows.items():
        assert "FOR SHARE" in src, f"{name} 还是裸 SELECT，P1-2 的 TOCTOU 窗口还在"
        assert "FOR UPDATE" not in src, (
            f"{name} 用了 FOR SHARE 之外的锁模式；写侧与结账侧必须同序，"
            "否则会新增一条死锁边")


def test_凭证插入时_先锁期间再拿凭证行锁(migrated_scratch):
    """锁顺序的自证：守卫是 BEFORE 触发器，所以期间锁**先于**外键的 FOR KEY SHARE。

    写侧的顺序因此恒为「period → voucher」，与 ``close_period``
    （先 ``SELECT ... FOR UPDATE`` 整条期间链、再自动记账去 UPDATE voucher）
    同序。同序 + ``FOR SHARE`` 与外键的 ``FOR KEY SHARE`` 兼容
    ⇒ 守卫加锁不引入新的死锁边。

    断言的是「守卫挂在 BEFORE 上」—— 这是顺序成立的**前提**。
    把它改成 AFTER，顺序立刻反转，下面那条并发用例就会开始出现死锁。
    """
    with engine.connect() as conn:
        when = conn.execute(text(
            "SELECT tgtype & 2 <> 0 AS is_before "
            "FROM pg_trigger WHERE tgrelid = 'voucher'::regclass "
            "AND tgname = 'trg_voucher_guard'")).scalar()
        entry_when = conn.execute(text(
            "SELECT tgtype & 2 <> 0 AS is_before "
            "FROM pg_trigger WHERE tgrelid = 'entry'::regclass "
            "AND tgname = 'trg_entry_lock_guard'")).scalar()
    assert when, "trg_voucher_guard 不再是 BEFORE 触发器 —— 锁顺序反转了"
    assert entry_when, "trg_entry_lock_guard 不再是 BEFORE 触发器 —— 锁顺序反转了"


def test_并发_结账持锁时建凭证_被守卫挡住(migrated_scratch):
    """P1-2 的实测：写侧必须**阻塞在守卫里**，等结账提交后看到 locked=true。

    旧护栏（裸 SELECT）的时序是：写侧读到快照里的 locked=False 放行 →
    外键才去取 FOR KEY SHARE 被结账事务阻塞 → 结账提交 → 写侧解阻塞落库，
    **已结账期间里凭空多一张 UNPOSTED 凭证**。它记不了账（一切记账路径都
    要求前置期间已结账而后置未结账），默认口径账簿只算 POSTED，
    金额对所有余额和报表隐形 —— 存在，但没人知道它存在。

    这里断言的是「写侧在结账提交之前始终没落地」，而不是「最后报错了」：
    后者两种实现都能满足，而前者只有加锁才能做到。
    """
    eng, _ = migrated_scratch
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO book (id, company_name, name, start_year, start_month, "
            "subject_code_rule, status, created_at, updated_at) "
            "VALUES (901, '并发测试', '并发账套', 2026, 1, '4-2-2-2', 'ACTIVE', "
            "now(), now())")
        conn.exec_driver_sql(
            "INSERT INTO period (id, book_id, year, month, ord, locked, version, "
            "created_at, updated_at) VALUES (901, 901, 2026, 1, 1, false, 1, "
            "now(), now())")

    closer = eng.connect()
    closer.execute(text("BEGIN"))
    closer.execute(text("SELECT locked FROM period WHERE id = 901 FOR UPDATE"))

    outcome: dict = {}

    def writer():
        try:
            # 不手写 BEGIN/COMMIT：``with eng.connect()`` 自己管事务，
            # 再叠一条 BEGIN 只会拿到「already in transaction」的警告。
            with eng.connect() as c:
                c.execute(text(
                    "INSERT INTO voucher (book_id, period_id, voucher_no, "
                    "voucher_date, created_at, updated_at) "
                    "VALUES (901, 901, '记-001', DATE '2026-01-15', now(), now())"))
            outcome["ok"] = True
        except Exception as exc:                      # noqa: BLE001
            outcome["err"] = exc

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(1.5)
    still_running = t.is_alive()
    closer.execute(text("UPDATE period SET locked = true WHERE id = 901"))
    closer.commit()
    closer.close()
    t.join(15)

    assert still_running, (
        "结账事务还持着期间锁时，写侧就返回了 —— 它**没有**阻塞在守卫里。"
        f"outcome={outcome!r} —— TOCTOU 窗口仍然存在（P1-2 未修）")
    assert not outcome.get("ok"), (
        f"凭证竟然写进了已结账期间，outcome={outcome}")
    assert "已结账" in str(outcome.get("err")), (
        f"写侧被挡住了但报的不是「已结账」：{outcome.get('err')!r}")
    assert "deadlock" not in str(outcome.get("err")).lower(), (
        "出现了死锁 —— 加锁引入了新的死锁边，锁顺序分析是错的")

    with eng.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM voucher WHERE book_id = 901")).scalar()
    assert n == 0, "已结账期间里留下了凭证"


# ================================================================ 3. 冗余列 / 结构


def test_直写SQL改不了分录的账套归属(db, book):
    """P2-05：冗余列只要有第二条写入路径就一定会漂。

    旧触发器只挂在 ``UPDATE OF voucher_id`` 上，于是
    ``UPDATE entry SET book_id = 别的账套`` 根本不触发 —— 分录挂在一个
    与凭证无关的账套上，账簿按 book_id 聚合会把它算进另一个单位，
    凭证页面却照常显示，且没有任何报错。
    """
    vid = _balanced_voucher(db, book.id, no="记-001")
    other_book = db.execute(text(
        "INSERT INTO book (company_name, name, start_year, start_month, "
        "subject_code_rule, status, created_at, updated_at) "
        "VALUES ('别的单位', '别的账套', 2026, 1, '4-2-2-2', 'ACTIVE', now(), now()) "
        "RETURNING id")).scalar()
    db.commit()

    ids = db.execute(text("SELECT id FROM entry WHERE voucher_id = :v ORDER BY line_no"),
                     {"v": vid}).scalars().all()
    db.execute(text("UPDATE entry SET book_id = :o WHERE id = :e"),
               {"o": other_book, "e": ids[0]})
    db.commit()
    assert db.execute(text("SELECT book_id FROM entry WHERE id = :e"),
                      {"e": ids[0]}).scalar() == book.id, (
        "分录被挂到了别的账套上而没有任何报错（P2-05 未修）")

    db.execute(text("UPDATE entry SET period_ord = 7 WHERE id = :e"), {"e": ids[1]})
    db.commit()
    assert db.execute(text("SELECT period_ord FROM entry WHERE id = :e"),
                      {"e": ids[1]}).scalar() == 1, "分录的 period_ord 可以被直接改写"


def test_分录冗余列指向不存在的期间会被外键挡住(db, book):
    """组合外键把 ``(book_id, period_ord)`` 钉在真实期间上。

    没有它时 ``period_ord`` 只是一个整数：改成任何一个不存在的期间号
    都不报错，账簿就把金额算进了一个不存在的月份。
    """
    from sqlalchemy.exc import DBAPIError

    vid = _balanced_voucher(db, book.id, no="记-002")
    eid = db.execute(text("SELECT id FROM entry WHERE voucher_id = :v "
                          "ORDER BY line_no LIMIT 1"), {"v": vid}).scalar()
    # 关掉同步触发器，否则它会把 period_ord 重算回 1，外键根本没机会说话
    db.execute(text("ALTER TABLE entry DISABLE TRIGGER trg_entry_sync"))
    db.commit()
    try:
        with pytest.raises(DBAPIError, match="fk_entry_book_period"):
            db.execute(text("UPDATE entry SET period_ord = 9999 WHERE id = :e"),
                       {"e": eid})
            db.commit()
        db.rollback()
    finally:
        db.execute(text("ALTER TABLE entry ENABLE TRIGGER trg_entry_sync"))
        db.commit()


def test_updated_at_在触发器改写行时也刷新(db, book):
    """P2-09：``onupdate`` 只对 ORM 发出的 UPDATE 生效。

    ``fn_voucher_resync_entries`` 换期回填分录时走的是数据库自己的
    ``UPDATE entry``，ORM 完全不知情，``updated_at`` 停在旧值 ——
    而按时间戳取证正是审计与排障的常用手段。
    """
    vid = _balanced_voucher(db, book.id, no="记-003", stale=True)
    before = db.execute(text("SELECT updated_at FROM entry WHERE voucher_id = :v "
                             "ORDER BY line_no LIMIT 1"), {"v": vid}).scalar()
    assert before < db.execute(text("SELECT now()")).scalar(), (
        "前置没搭对：时间戳没有往前推，看不出变化")

    target = db.execute(text(
        "SELECT id FROM period WHERE book_id = :b AND month = 2"),
        {"b": book.id}).scalar()
    db.execute(text(
        "UPDATE voucher SET period_id = :p, voucher_date = DATE '2026-02-15' "
        "WHERE id = :v"), {"p": target, "v": vid})
    db.commit()
    after = db.execute(text("SELECT updated_at FROM entry WHERE voucher_id = :v "
                            "ORDER BY line_no LIMIT 1"), {"v": vid}).scalar()
    assert after > before, (
        f"触发器回填了分录，但 updated_at 没动：{before} -> {after}（P2-09 未修）")


def test_绕过ORM的写入不再因为缺默认值而失败(db, book):
    """P2-10：默认值只写在 ORM 的 ``default=`` 里时，绕路写入会 NOT NULL 失败。"""
    # category 没有业务默认值（它是必填项），其余有默认值的列一律不给 ——
    # 旧库里它们只在 ORM 的 default= 里，绕路写入会 NOT NULL 失败。
    db.execute(text(
        "INSERT INTO subject (book_id, code, name, category, created_at, updated_at) "
        "VALUES (:b, '9900', '只给最少列', 'ASSET', now(), now())"), {"b": book.id})
    db.execute(text(
        "INSERT INTO voucher (book_id, period_id, voucher_no, voucher_date, "
        "created_at, updated_at) "
        "VALUES (:b, (SELECT min(id) FROM period WHERE book_id = :b), "
        "'记-004', DATE '2026-01-15', now(), now())"),
        {"b": book.id})
    db.commit()
    row = db.execute(text(
        "SELECT status, source, attachments FROM voucher WHERE voucher_no = '记-004'"
    )).one()
    assert row == ("UNPOSTED", "MANUAL", 0), f"server_default 没生效：{row}"


def test_角色取值受数据库约束(db, plain_user, book):
    """P2-12：``role`` 是权限判据，而各处判据都是大小写敏感的黑名单。

    绕过 API 写进一个 ``'viewer'``，它既不等于 ``"VIEWER"``（放行）
    又不等于 ``"OWNER"``，于是一行本该被拒的账号拿到了完整写权限 ——
    fail-open，与这套系统一贯的 fail-closed 原则相反。
    """
    from sqlalchemy.exc import DBAPIError

    db.execute(text(
        "INSERT INTO sys_user_book (user_id, book_id, role, created_at, updated_at) "
        "VALUES (:u, :b, 'ACCOUNTANT', now(), now())"),
        {"u": plain_user.id, "b": book.id})
    db.commit()
    with pytest.raises(DBAPIError, match="ck_sys_user_book_role"):
        db.execute(text(
            "INSERT INTO sys_user_book (user_id, book_id, role, created_at, updated_at) "
            "VALUES (:u, :b, 'viewer', now(), now())"),
            {"u": plain_user.id, "b": book.id})
        db.commit()
    db.rollback()


def test_角色的合法取值与_api_常量一致():
    """约束里的取值必须与 ``api/system.py::ROLES`` 逐字相同。

    两边不一样会制造出最难查的一类问题：**库里进不去，但代码以为能进**。
    ``_check_roles`` 是精确集合比对（不是大小写敏感的），所以只要这里
    和它一致，「写不进去」这件事在 API 层就已经被挡过一次了。
    """
    from app.api.system import ROLES

    with engine.connect() as conn:
        body = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'sys_user_book'::regclass "
            "AND conname = 'ck_sys_user_book_role'")).scalar()
    for role in ROLES:
        assert f"'{role}'" in body, f"库里没有 {role}，但 api/system.py::ROLES 里有"
    assert set(ROLES) == {"OWNER", "ACCOUNTANT", "VIEWER"}


def test_打印模板_有账套归属列_且存量模板留空(db):
    """P1-5 根治项：模板必须能归属到账套，门禁才能回到「该账套的角色」。

    **NULL 的语义是「全局兜底」**，不是「没有归属」：迁移之前建的模板
    一律是全局的，回填到任意一个账套等于替人断言归属。所以这里断言
    「可空 + 有外键」，不断言「非空」—— 后者是应用层改造完成后的事。
    """
    with engine.connect() as conn:
        nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'print_template' AND column_name = 'book_id'"
        )).scalar()
        fk = conn.execute(text(
            "SELECT confdeltype::text FROM pg_constraint "
            "WHERE conrelid = 'print_template'::regclass "
            "AND conname = 'fk_print_template_book'")).scalar()
    assert nullable == "YES", "print_template.book_id 必须可空（存量模板留 NULL）"
    assert fk == "c", "print_template.book_id 的外键没到位"


def test_两条热路径索引都在(db):
    """P2-6：索引由 0006 建，INCLUDE 由 autogenerate 不比对，所以这里手工断言。"""
    with engine.connect() as conn:
        defs = dict(conn.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
            "('ix_entry_book_period', 'ix_period_opening_book')")).all())
    assert set(defs) == {"ix_entry_book_period", "ix_period_opening_book"}, (
        f"P2-06 的索引缺失：{sorted(defs)}")
    assert "INCLUDE (subject_id, direction, amount)" in defs["ix_entry_book_period"], (
        f"ix_entry_book_period 没有 INCLUDE 三列：{defs['ix_entry_book_period']}")
    assert "book_id, period_ord" in defs["ix_entry_book_period"]
    assert "(book_id, period_id)" in defs["ix_period_opening_book"]


def test_账簿明细账用的窄索引没有被替换掉(db):
    """``ix_entry_subject_ord`` 留着：明细账逐科目取数靠它，宽索引替不了。"""
    with engine.connect() as conn:
        names = set(conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'ix_entry_subject_ord'"
        )).scalars())
    assert names, "ix_entry_subject_ord 被删了 —— 明细账取数会退化成扫该账套全部分录"


# ================================================================ 4. 防回归机制


def test_模型与库的外键零差异(db):
    """P1-10 的防回归：autogenerate 是一台「把护栏改回缺陷」的机器。

    模型里 ``period_opening.subject_id`` 曾写 ``ondelete="CASCADE"``，
    库里是 RESTRICT（0003 的 docstring 说这条外键正是它存在的理由）。
    谁照着模型跑一次 ``alembic revision --autogenerate``，就会生成一条
    把 RESTRICT 改回 CASCADE 的 revision —— 那正是实测过的静默数据损失
    （「2 条期初删成 1 条，0 → -100」，没有任何报错）。

    所以这里用 ``compare_metadata`` 直接比对**模型与真实库**：任何
    Add/Remove/Modify 外键或唯一约束的差异都会让这条变红。
    刻意**不**断言「零差异」：别的模型改动若忘了写 migration，会让这条
    假红，而那属于别人的工作线。这里只盯约束这一类 —— 它正是把护栏
    改回缺陷的那条路。
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401  确保所有模型都已注册
    from app.models.base import Base

    # compare_metadata 把一批变更装进 ModifyTableOps（容器），逐条摊开。
    # 只认「约束类」变更：外键的增删改与唯一键的增删，就是把库级护栏
    # 改回缺陷的那条路。列类型/默认值不在这里盯 —— 那是别的工作线。
    CONSTRAINT_OPS = {
        "AddConstraintOp", "DropConstraintOp", "CreateForeignKeyOp",
        "CreateUniqueConstraintOp", "CreatePrimaryKeyOp",
        "CreateCheckConstraintOp",
    }

    with engine.connect() as conn:
        diffs = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}),
            Base.metadata)

    offenders: list[str] = []
    for table, ops in diffs:
        for outer in ops:
            for item in getattr(outer, "ops", None) or [outer]:
                if type(item).__name__ in CONSTRAINT_OPS:
                    offenders.append(
                        f"{table.name}: {type(item).__name__} "
                        f"{getattr(item, 'constraint_name', '') or ''} "
                        f"{getattr(item, 'constraint', '') or ''}")
    assert not offenders, (
        "模型与库在约束上不一致 —— 照模型跑一次 autogenerate 就会把它们改回去：\n"
        + "\n".join(offenders)
        + "\n（这类差异就是 P1-10：护栏在库里是对的、在模型里是错的）")


def test_期初外键_模型侧也是_RESTRICT():
    """同一个不变量的第二道断言，直接钉住模型声明本身。

    ``compare_metadata`` 那条已经能盖住它，但它要连库；这条在
    **不连库**（比如 IDE 里看模型、或者 CI 只跑静态检查）时也能守住，
    而且失败信息直指要改的那一行。
    """
    from app.models import PeriodOpening

    fks = [f for f in PeriodOpening.__table__.foreign_key_constraints
           if {c.name for c in f.columns} == {"subject_id"}]
    assert len(fks) == 1, "period_opening.subject_id 应当恰好有一条外键"
    assert fks[0].ondelete == "RESTRICT", (
        f"模型侧写的是 {fks[0].ondelete!r}，库里是 RESTRICT —— "
        "autogenerate 会照模型把它改回 CASCADE，那正是 P1-05 实测过的静默数据损失")


def test_head_是本文件认识的那一版():
    """新增 revision 时必须把 head 登记进来 —— 否则这条钉子会变成「历史遗留」。

    它守的是「有没有人在没打招呼的情况下动了迁移链」。往这个集合里加一项
    是一次**显式的**声明动作：新 revision 落地时顺手补一行，而不是等到某天
    有人把链改坏了才来查。
    """
    assert head_revision() in {
        "0005_db_guardrails",
        "0006_hot_path_indexes",
        "0007_subject_leaf_parent_guard",   # P2-04 的父节点护栏送达存量库
        "0008_login_ip_throttle",           # P2-11 的 IP 维度节流状态表
        "0009_subject_year_code_edit",      # 科目编码：本年度无发生额即可改
        "0010_subject_year_code_subtree",   # 同上，但「发生额」按整棵子树判（汇总级也算有记账）
    }, (
        f"head 变成了 {head_revision()!r} —— 迁移链是不是被改动了？"
        "（新增 revision 的话，把新 head 登记进上面那个集合）")
