"""恢复路径的测试（P2-25）。

**这份测试盯的是「退路还在不在」。** 原来只有 dump，没有 restore，
也没有任何测试，于是「备份能用」被当成了「出事能救回来」——这两件事不一样。

核心用例做的是**真实往返**：把测试库 dump 出来，恢复进一个全新的空库，
然后核对恢复出来的库和源库表结构、行数一致。这正是真实恢复的形态
（灾难后往空库里灌），也绕开了「往已有数据的库里灌会撞 CREATE TABLE」。

比较行数而不是断言某张表，是故意的：断言具体表名会在别的用例改了表结构后
假失败，而「两边表集合相同 + 同样的表行数相同」正是恢复该保证的东西。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_db_backup():
    """按文件路径加载 scripts/db_backup.py。

    不走 ``from scripts import db_backup``：``scripts/`` 没有 ``__init__.py``，
    它是一堆独立运维脚本而不是一个包。
    """
    spec = importlib.util.spec_from_file_location(
        "cwq_db_backup", BACKEND_ROOT / "scripts" / "db_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


db_backup = _load_db_backup()

# 客户端二进制（pg_dump / psql）。
#
# 本机没装时往返用例会跳过 —— 装了 pytest 的人不一定装了 PostgreSQL 客户端。
# 但 CI 里**不许**跳过：设了 CWQ_REQUIRE_PG_CLIENT=1 就表示「这里必须有客户端」，
# 缺了就该红。静默 skip 会让这份退路测试在流水线上变成一个永远不执行的
# 占位符 —— 那正是审计 P1-25 说的「绿是因为没人真的跑过它」。
_HAVE_CLIENT = bool(shutil.which(db_backup.PG_DUMP_BIN)
                    and shutil.which(db_backup.PSQL_BIN))
_REQUIRE_CLIENTS = os.environ.get("CWQ_REQUIRE_PG_CLIENT") == "1"

requires_clients = pytest.mark.skipif(
    not _HAVE_CLIENT and not _REQUIRE_CLIENTS,
    reason="本机没有 pg_dump/psql；CI 装 postgresql-client 且设 "
           "CWQ_REQUIRE_PG_CLIENT=1 后必跑")


@pytest.mark.skipif(_HAVE_CLIENT, reason="本机有客户端，这条不适用")
def test_missing_clients_is_failure_when_required():
    if not _REQUIRE_CLIENTS:
        pytest.skip("本机开发环境，允许没有 PostgreSQL 客户端")
    pytest.fail("CWQ_REQUIRE_PG_CLIENT=1 要求 pg_dump/psql 必须存在 —— "
                "CI 里请装 postgresql-client，否则恢复路径从未被真实验证过")


def _url() -> str:
    """测试库连接串。

    conftest 在 import app.* 之前把 ``DATABASE_URL`` 改成了测试库，
    所以这里读到的就是测试库 —— 不用再去 import conftest 拿那个常量，
    也就不用赌 pytest 往 sys.path 里塞了什么。
    """
    from app.core.config import settings
    return settings.database_url


def _admin_url() -> str:
    return _url().rsplit("/", 1)[0] + "/postgres"


def _scratch_name() -> str:
    """演练用的空库名。

    沿用 conftest 那条约定：名字里必须带 ``test``。这里的 DROP DATABASE
    是整套测试里最重的一把刀，值得再挂一道自己看得见的护栏。
    """
    base = _url().rsplit("/", 1)[-1]
    return f"{base}_restore_test"


def _admin_engine() -> Engine:
    return create_engine(_admin_url(), isolation_level="AUTOCOMMIT")


def _drop_if_exists(name: str) -> None:
    with _admin_engine().connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def scratch_db():
    name = _scratch_name()
    assert "test" in name, f"演练库名必须带 test 标记：{name}"
    _drop_if_exists(name)
    with _admin_engine().connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield _url().rsplit("/", 1)[0] + "/" + name
    finally:
        _drop_if_exists(name)


def _signature(url: str) -> dict[str, int]:
    """一个库的结构 + 每张表的行数。"""
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            tables = sorted(
                t for t in inspect(conn).get_table_names()
                # alembic_version 等元数据表也带上：恢复要连版本一起恢复，
                # 不然恢复出来的库连「自己该在哪个 revision」都不知道。
                if not t.startswith("pg_"))
            return {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
                    for t in tables}
    finally:
        eng.dispose()


# --------------------------------------------------------------------- 命令行拼装


def test_password_never_reaches_argv():
    """口令只能走 PGPASSWORD，不能出现在进程参数里。

    ``ps`` 对同机器上任何用户可见 —— 一次例行备份把生产口令泄露出去，
    这正是 db_backup.py 开头那段注释要防的事，所以钉成测试。
    """
    url = "postgresql+psycopg://cwq:sup3rsecret@db.example:5432/cwq"
    assert "sup3rsecret" not in " ".join(db_backup.dump_command(url))
    assert "sup3rsecret" not in " ".join(db_backup.restore_command(url))


def test_driver_prefix_stripped_for_pg_tools():
    """pg_dump / psql 都不认 postgresql+psycopg:// 这个 SQLAlchemy 驱动段。"""
    url = "postgresql+psycopg://cwq:pw@127.0.0.1:15432/cwq_test"
    assert db_backup.dump_command(url)[:1] == [db_backup.PG_DUMP_BIN]
    assert "cwq_test" in db_backup.dump_command(url)
    assert "postgresql+psycopg" not in " ".join(db_backup.restore_command(url))


def test_url_without_host_is_refused():
    """解析不出主机名就是配置错误，必须拒绝而不是拿个默认值去连。"""
    with pytest.raises(ValueError):
        db_backup.dump_command("postgresql+psycopg:///cwq")


def test_restore_refuses_itself_when_executable_missing(monkeypatch):
    """找不到客户端二进制时报 127，且说清怎么指路，不是静默跳过。"""
    monkeypatch.setattr(db_backup, "PG_DUMP_BIN", "definitely-not-pg_dump-xyz")
    assert db_backup.dump(_url()) == 127


# --------------------------------------------------------------------- 恢复的闸


def test_restore_refuses_empty_dump():
    """备份失败留下的 0 字节文件，绝不能被当成数据灌回去。

    这是最坏的一种情况：恢复「成功」了，数据库被清空，
    而日志里写着恢复完成 —— 比不恢复糟得多。
    """
    with pytest.raises(db_backup.RestoreRefused) as e:
        db_backup.assert_restorable(b"")
    assert "备份" in str(e.value)


def test_restore_refuses_truncated_or_foreign_file():
    """半份文件、或者根本不是 dump 的东西，同样拒绝。"""
    with pytest.raises(db_backup.RestoreRefused):
        db_backup.assert_restorable(
            "-- 备份到一半磁盘满了\nCREATE TABLE x (i int);\n".encode())


def test_restore_accepts_real_dump_header():
    """闸不能把真 dump 也拦下来 —— 拦过头就等于没有退路。"""
    real = (b"--\n-- PostgreSQL database dump\n--\n\n"
            b"SET statement_timeout = 0;\nSELECT pg_catalog.set_config('search_path', '', false);\n")
    db_backup.assert_restorable(real)  # 不抛 = 通过


def test_main_restore_refusal_exits_nonzero(tmp_path, capsys):
    bad = tmp_path / "not-a-dump.sql"
    bad.write_bytes(b"hello\n")
    rc = db_backup.main(["--restore", "--file", str(bad)])
    assert rc == 3
    assert "拒绝恢复" in capsys.readouterr().err


def test_main_restore_missing_file_exits_nonzero(tmp_path):
    rc = db_backup.main(["--restore", "--file", str(tmp_path / "nope.sql")])
    assert rc == 2


# --------------------------------------------------------------------- 真实往返


@requires_clients
def test_dump_restore_roundtrip(scratch_db):
    """真的 dump 一遍、真的灌进空库、再核对数据回来了。

    演练库是**空库**：真实恢复就是往空库里灌。往一个已经有数据的库里灌会
    撞 ``CREATE TABLE already exists``，那样的失败不是这份测试要证明的事。
    """
    src_sig = _signature(_url())

    dump_cmd = db_backup.dump_command(_url())
    proc = subprocess.run(dump_cmd, env=db_backup._env(db_backup._parse(_url())),
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    sql = proc.stdout
    assert sql, "pg_dump 什么也没输出"

    # 灌之前：演练库必须是空的，否则「恢复成功」说明不了任何事
    assert _signature(scratch_db) == {}

    rc = db_backup.restore(scratch_db, sql)
    assert rc == 0, f"恢复失败，退出码 {rc}"

    got_sig = _signature(scratch_db)
    assert got_sig == src_sig, (
        f"恢复出来的库和源库不一致\n"
        f"  只在源库：{sorted(set(src_sig) - set(got_sig))}\n"
        f"  只在恢复库：{sorted(set(got_sig) - set(src_sig))}\n"
        f"  行数不同：{ {t: (src_sig[t], got_sig[t]) for t in set(src_sig) & set(got_sig) if src_sig[t] != got_sig[t]} }"
    )
    assert len(got_sig) > 5, "恢复出来的库几乎没表，这条测试多半是空转"


@requires_clients
def test_roundtrip_recovers_actual_data(scratch_db):
    """再进一步：造一条有辨识度的数据，恢复后按内容找回来。

    只比行数不够 —— 两边都是 0 行也算「一致」。所以往演练库里灌一份
    源库快照之后，再写一行带唯一标记的数据，走一遍 dump/restore，
    确认这行数据原样回来了（而不是靠行数凑巧对上）。
    """
    import uuid

    marker = "恢复演练-" + uuid.uuid4().hex[:8]
    src = _url()
    src_eng = create_engine(src)
    try:
        with src_eng.begin() as conn:
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS restore_probe ('
                ' id serial primary key, marker text not null)'))
            conn.execute(text(
                "INSERT INTO restore_probe (marker) VALUES (:m)"), {"m": marker})
        proc = subprocess.run(db_backup.dump_command(src),
                              env=db_backup._env(db_backup._parse(src)),
                              capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode()
        assert db_backup.restore(scratch_db, proc.stdout) == 0

        dst_eng = create_engine(scratch_db)
        try:
            with dst_eng.connect() as conn:
                found = conn.execute(
                    text("SELECT marker FROM restore_probe")).scalars().all()
        finally:
            dst_eng.dispose()
        assert marker in found, f"标记行没回来，恢复出来的数据是 {found}"
    finally:
        with src_eng.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS restore_probe"))
        src_eng.dispose()