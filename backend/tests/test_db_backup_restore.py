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
import re
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
def test_roundtrip_restores_posted_voucher_and_keeps_the_guard(
        db, book, make_voucher, scratch_db):
    """**已记账**的凭证也要能原样恢复 —— 而且恢复**之后护栏还在**。

    **为什么这条要单独钉（它是这轮新加的护栏带来的新风险）。**

    审计报告 §3.15 新加了一条库层护栏：「已记账凭证的分录不许增删改」，
    它挂在 ``entry`` 的 BEFORE INSERT/UPDATE/DELETE 上。而**恢复就是往库里
    插 entry 行** —— 护栏会不会在恢复时把整个恢复打死？

    **现在不会，靠的是 pg_dump 的格式约定，不是运气。**
    plain 格式的 dump 把**触发器放在数据段之后**才装（实测这份 dump：
    ``COPY public.entry`` 在第 2100 行、``COPY public.voucher`` 在第 2716 行，
    而 ``CREATE TRIGGER trg_entry_lock_guard`` 在**第 3753 行**）——
    也就是说，**灌数据的时候那条护栏根本还没装上**，entry 怎么灌都撞不到它。
    （函数体是 pre-data 就有的，但触发器没有，护栏就不生效。）

    **反过来就危险**：往一个**已经建好 schema、触发器已武装**的库里灌数据，
    护栏就在位上了 —— 实测会直接失败（``fn_entry_sync`` 报「凭证不存在」，
    因为数据段顺序里 entry 排在 voucher 前面）。
    这就是 §3.38 那条「**先清 schema 再灌**」的真正理由 ——
    不只是「CREATE TABLE already exists」，更是「触发器在位时数据顺序会撞护栏」。
    哪天有人图省事改成 ``--data-only`` 灌进现成的库，这条退路就会在
    **真出事的那天**才断 —— 而恢复是系统唯一的退路。

    所以这里钉住两件事：① 已记账凭证连分录一起能恢复；② 恢复**之后**，
    那条护栏仍然拦得住（不然「恢复出来的库」是**没有护栏的库**，
    比恢复失败更危险 —— 它看起来是好的）。
    """
    from datetime import date

    v = make_voucher(date(2026, 1, 5),
                     dr=[("1001", 100, "已记账")], cr=[("5001", 100, "已记账")],
                     posted=True)
    n_entries = db.execute(
        text("SELECT count(*) FROM entry WHERE voucher_id = :v"), {"v": v.id}
    ).scalar()
    assert n_entries == 2, n_entries

    src = _url()
    proc = subprocess.run(db_backup.dump_command(src),
                          env=db_backup._env(db_backup._parse(src)),
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()

    rc = db_backup.restore(scratch_db, proc.stdout)
    assert rc == 0, ("恢复失败 —— 已记账凭证的分录把恢复挡住了？"
                     "这说明 dump 里 entry 的触发器不再装在数据段之后"
                     "（护栏在灌数据时就在位上了），"
                     "或者那条护栏本身坏了。"
                     "先跑 test_触发器在数据段之后才装_所以恢复时护栏不会挡 看看顺序变了没")

    dst_eng = create_engine(scratch_db)
    try:
        with dst_eng.connect() as conn:
            got = conn.execute(
                text("SELECT status FROM voucher WHERE id = :v"), {"v": v.id}
            ).all()
            assert got and got[0][0] == "POSTED", got
            n = conn.execute(
                text("SELECT count(*) FROM entry WHERE voucher_id = :v"),
                {"v": v.id}).scalar()
            assert n == 2, f"分录没回来：{n} 条"

        # 恢复**之后**，护栏仍然要拦得住 —— 恢复出来的库不能是没有护栏的库
        with pytest.raises(Exception) as ei:
            with dst_eng.begin() as conn:
                conn.execute(text("UPDATE entry SET amount = 999 WHERE voucher_id = :v"),
                             {"v": v.id})
        assert "已记账" in str(ei.value), str(ei.value)
    finally:
        dst_eng.dispose()


@requires_clients
def test_触发器在数据段之后才装_所以恢复时护栏不会挡():
    """护栏在恢复时不挡，靠的是**触发器在数据段之后才装** —— 这条要钉住。

    **为什么不能只写在注释里。** pg_dump 的格式约定（触发器属于 post-data 段）
    是这条退路能成立的前提。它哪天变了 —— 换了 pg_dump 版本、换了备份格式、
    或者有人改成 ``--data-only`` 灌进现成的库 —— 护栏就会在恢复时炸，
    整份恢复回滚（``psql --single-transaction`` ⇒ 一张表都恢复不了）。
    而恢复是**系统唯一的退路**，它断的那天正是最需要它的那天。

    所以这里直接把 dump 里的**先后顺序**断言出来，而不是靠人记得。
    """
    src = _url()
    proc = subprocess.run(db_backup.dump_command(src),
                          env=db_backup._env(db_backup._parse(src)),
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    sql = proc.stdout.decode("utf-8", "replace")

    copy_at = sql.find("COPY public.entry ")
    assert copy_at > 0, "dump 里没有 entry 的数据段 —— 这份 dump 根本不是本系统的"

    # entry 上的**每一个**触发器都必须在数据段之后才装
    positions = [m.start() for m in re.finditer(
        r"^CREATE TRIGGER .* ON public\.entry ", sql, re.M)]
    assert positions, "dump 里找不到 entry 上的触发器 —— 护栏没进备份？"
    for pos in positions:
        assert pos > copy_at, (
            "entry 的触发器装在数据段**之前** —— 恢复时护栏已经在位，"
            "往库里插 entry 行会被「已记账凭证的分录不许动」当场打死，"
            "整份恢复回滚。恢复必须「先清 schema 再灌」，"
            "不能改成 --data-only 灌进现成的库。")


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

# ---------------------------------------------------------------------------
# 定时备份（2026-10 上线安全收尾）
#
# 原来备份只在发布流程里跑，于是「一个月不发版 = 一个月没有新备份」。
# 加上 systemd timer 之后，dump 从「发布的一个步骤」变成了「每天都做的事」，
# 而**每天都做的事必须自己管住自己** —— 这是下面这组用例的由来。
# ---------------------------------------------------------------------------


def test_备份失败时不留下会被误当成备份的文件(tmp_path, monkeypatch):
    """**半截文件是最阴的那种失败。**

    shell 重定向 `pg_dump > backup.sql` 会先把文件建出来，pg_dump 随后才跑。
    客户端没装 / 口令错 / 磁盘满时，那个文件已经躺在目录里了：0 字节或半截，
    文件名完全正常。保留策略一开，它就一直留到被轮转掉 ——
    而某天有人拿它恢复，恢复「成功」了，库里是空的。

    这与 --restore 那侧的 RestoreRefused 是同一个担心，所以两头都得防。
    """
    def boom(fh):
        raise RuntimeError("pg_dump 根本没起来")

    monkeypatch.setattr(db_backup, "dump_to", boom)
    out = tmp_path / "db-2026-10-15.sql"
    with pytest.raises(RuntimeError):
        db_backup.dump_to_file(out)
    assert not out.exists(), "备份失败了却留下了一个空壳文件冒充备份"
    assert not list(tmp_path.glob("*.part")), "临时文件没清掉"


def test_dump失败时退出码非零且不留临时文件(tmp_path, monkeypatch):
    rc_holder = {}

    def fake_dump_to(fh):
        fh.write(b"-- partial dump")
        rc_holder["rc"] = 1
        return 1

    monkeypatch.setattr(db_backup, "dump_to", fake_dump_to)
    out = tmp_path / "db-2026-10-15.sql"
    assert db_backup.dump_to_file(out) == 1
    assert not out.exists()
    assert not list(tmp_path.glob("*.part")), "临时文件没清掉 —— 下次会被当成备份"


def test_成功时产出的是完整文件且无临时文件(tmp_path, monkeypatch):
    payload = b"-- PostgreSQL database dump\n" + b"x" * 2000
    monkeypatch.setattr(db_backup, "dump_to",
                        lambda fh: (fh.write(payload), 0)[1])

    out = tmp_path / "db-2026-10-15.sql"
    assert db_backup.dump_to_file(out) == 0
    assert out.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))


def test_产出文件权限是600(tmp_path, monkeypatch):
    """备份是全量账务数据，不该对本机其他账号可读。

    原来靠 umask 兜底 —— 而 umask 是调用方的运行环境决定的，默认 022
    会把它落成 644。这台机器上跑着多个站点，任何本地账号（包括别的站点
    的服务账号）都能读到凭证、科目和金额。所以权限要由备份自己钉死，
    不能指望运行环境碰巧给个严的 umask。

    断言的是**改名之后**的最终文件：``.part`` 改名成正式备份时是保留权限
    位的，所以最终文件才是真正会被拿去恢复的那一份。
    """
    monkeypatch.setattr(db_backup, "dump_to",
                        lambda fh: (fh.write(b"-- PostgreSQL database dump\n"), 0)[1])

    out = tmp_path / "db-2026-10-17.sql"
    assert db_backup.dump_to_file(out) == 0
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600, f"备份落成了 {mode:o}，本机其他账号可读"


def test_产出0字节按失败处理(tmp_path, monkeypatch):
    """pg_dump 返回 0 却什么都没写 —— 极少发生，但发生时它返回 0。

    只看退出码的话，这份「成功的 0 字节备份」会正常留在目录里，
    名字规规矩矩，体积 0。恢复侧会拒掉它，但到那时数据已经没了。
    """
    monkeypatch.setattr(db_backup, "dump_to", lambda fh: (fh.write(b""), 0)[1])
    out = tmp_path / "db-2026-10-16.sql"
    assert db_backup.dump_to_file(out) != 0, "0 字节备份被当成成功了"
    assert not out.exists()


def test_保留策略只留最近N份(tmp_path, monkeypatch):
    """没有保留策略的每日备份会慢慢把磁盘写满。

    而磁盘写满之后下一个失败的就是**备份本身** —— 一个把自己搞挂的策略。
    """
    for d in range(1, 6):
        p = tmp_path / f"db-2026-10-0{d}.sql"
        p.write_bytes(b"dump")
        # 显式设 mtime，保证排序稳定、不受文件系统时间精度影响
        os.utime(p, (1_700_000_000 + d * 86400, 1_700_000_000 + d * 86400))

    monkeypatch.setattr(db_backup, "dump_to", lambda fh: (fh.write(b"new"), 0)[1])
    db_backup.dump_to_file(tmp_path / "db-2026-10-06.sql", keep=3)

    left = sorted(p.name for p in tmp_path.glob("*.sql"))
    assert left == ["db-2026-10-04.sql", "db-2026-10-05.sql", "db-2026-10-06.sql"], left


def test_保留策略不删别的文件(tmp_path, monkeypatch):
    """只删自己管的 .sql。备份目录里若有 backup.log，
    按名字删的时候很容易把它一起清掉 —— 那正是唯一的历史记录。"""
    (tmp_path / "backup.log").write_text("以前的日志，不能动")
    for d in range(1, 4):
        (tmp_path / f"db-2026-10-0{d}.sql").write_bytes(b"dump")

    monkeypatch.setattr(db_backup, "dump_to", lambda fh: (fh.write(b"new"), 0)[1])
    db_backup.dump_to_file(tmp_path / "db-2026-10-04.sql", keep=1)

    assert (tmp_path / "backup.log").exists(), "保留策略把日志删了"

class TestStripUnsupportedSettings:
    """恢复前剔除服务端不认识的 SET 行。

    **为什么要有这个。** pg_dump 会把版本特有的会话参数写进 dump：
    ``transaction_timeout`` 是 PostgreSQL 17 才有的，pg_dump 17+ 会写
    一行 ``SET transaction_timeout = 0;``。PG 16 的服务端不认识它。

    而恢复命令带 ``--single-transaction`` + ``ON_ERROR_STOP=1`` —— 那一条
    SET 会让**整份 dump 回滚，一张表都不恢复**，退出码只有 3。

    实测（postgres:16 服务端 + pg_dump 18.6 客户端）：
      ERROR:  unrecognized configuration parameter "transaction_timeout"
      退出码 3；之后 \\dt → Did not find any relations.

    本仓库把恢复称作「系统唯一的退路」，所以这条必须堵住。
    """

    def _module(self):
        return _load_db_backup()

    def test_removes_only_unknown_setting(self):
        db = self._module()
        dump = (
            b"--\n-- PostgreSQL database dump\n--\n\n"
            b"SET statement_timeout = 0;\n"
            b"SET lock_timeout = 0;\n"
            b"SET transaction_timeout = 0;\n"
            b"SET client_encoding = 'UTF8';\n"
            b"CREATE TABLE t(id int);\n"
            b"INSERT INTO t VALUES (1);\n"
        )
        known = {"statement_timeout", "lock_timeout", "client_encoding"}
        out, removed = db.strip_unsupported_settings(dump, known)

        assert removed == ["transaction_timeout"]
        assert b"transaction_timeout" not in out
        # 服务端认识的必须原样留着
        assert b"SET statement_timeout = 0;" in out
        assert b"SET client_encoding = 'UTF8';" in out
        # 数据与结构一行都不能少
        assert b"CREATE TABLE t(id int);" in out
        assert b"INSERT INTO t VALUES (1);" in out

    def test_keeps_everything_when_all_known(self):
        db = self._module()
        dump = b"SET statement_timeout = 0;\nCREATE TABLE t(id int);\n"
        known = {"statement_timeout"}
        out, removed = db.strip_unsupported_settings(dump, known)
        assert removed == []
        assert out == dump

    def test_does_not_touch_data_looking_like_set(self):
        # 删的是**整行** SET 语句，不是文本里出现的参数名 ——
        # INSERT 的值里正好写着 transaction_timeout 时不能被改。
        db = self._module()
        dump = (
            b"SET transaction_timeout = 0;\n"
            b"INSERT INTO t(v) VALUES ('transaction_timeout');\n"
        )
        out, removed = db.strip_unsupported_settings(dump, set())
        assert removed == ["transaction_timeout"]
        assert b"INSERT INTO t(v) VALUES ('transaction_timeout');" in out

    def test_ignores_composite_set_statements(self):
        # SET SESSION CHARACTERISTICS AS TRANSACTION ... 不是单个参数，
        # 不该被当成「服务端不认识的参数名」而删掉。
        db = self._module()
        dump = b"SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL SERIALIZABLE;\n"
        out, removed = db.strip_unsupported_settings(dump, set())
        assert removed == []
        assert out == dump

    def test_server_setting_names_returns_none_when_unreachable(self):
        # 问不到服务端时返回 None（含义是「不知道」），调用方据此不剔任何东西。
        db = self._module()
        original = db.PSQL_BIN
        db.PSQL_BIN = "/definitely/not/here/psql"
        try:
            assert db.server_setting_names("postgresql://u:p@h:5432/d") is None
        finally:
            db.PSQL_BIN = original



# ---------------------------------------------------------------------------
# 恢复**旧备份**：护栏不能跟着备份一起消失（审计 §3.15 的第二道）
# ---------------------------------------------------------------------------

#: 引入「entry 上的行号唯一约束 + 已记账凭证不许改分录」那条护栏的迁移。
#: 迁到它的**前一个** revision，就得到一个「护栏还不存在」的库 ——
#: 也就是「迁移之前的备份」的样子。
_GUARD_REV = "0037_log_retention_indexes"


def _old_backup_name() -> str:
    base = _url().rsplit("/", 1)[-1]
    stem = base[:-len("_test")] if base.endswith("_test") else base
    return f"{stem}_oldbackup_test"


def _upgrade_to(eng, target: str) -> None:
    """把库迁到指定 revision（和 ``upgrade_to_head`` 同一条路径）。

    不复用 ``upgrade_to_head`` 是因为它只能迁到 head，而这里要的是
    「**停在护栏之前**」的那个版本。
    """
    from alembic import command

    from app.core.schema_version import _config

    cfg = _config()
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        with conn.begin():
            command.upgrade(cfg, target)


@pytest.fixture
def old_backup_db():
    """一个**旧版本**的库：只迁到护栏那条迁移之前。"""
    name = _old_backup_name()
    assert "test" in name, f"演练库名必须带 test 标记：{name}"
    _drop_if_exists(name)
    with _admin_engine().connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _url().rsplit("/", 1)[0] + "/" + name
    eng = create_engine(url)
    try:
        _upgrade_to(eng, _GUARD_REV)
        yield url
    finally:
        eng.dispose()
        _drop_if_exists(name)


def _entry_unique_constraints(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            return {r[0] for r in conn.execute(text(
                "SELECT conname FROM pg_constraint WHERE contype = 'u' "
                "AND conrelid = 'entry'::regclass")).all()}
    finally:
        eng.dispose()


@requires_clients
def test_恢复旧备份后护栏必须还在(scratch_db, old_backup_db):
    """恢复一份**迁移之前**的备份，得到的库必须仍然有护栏。

    **为什么这条要单独钉（它是一个静默的错，不是「恢复失败」）。**

    §3.15 的护栏（``uq_entry_voucher_line_no`` 唯一约束 + 「已记账凭证的
    分录不许增删改」的触发器）是**迁移**带上去的，**不是 dump 里自带的** ——
    dump 只记录「备份那一刻库里有什么」。恢复一份迁移之前的备份，库里就
    没有这些护栏，而恢复命令照样返回 **0（成功）**、日志里一句提示都没有。

    修复前的实测：恢复后的库里
      · ``uq_entry_voucher_line_no`` 不存在；
      · 「已记账凭证的分录不许动」那段不在触发器函数里；
      · **同时改借贷两边的金额能成功** —— 已记账凭证的金额被静默改写，
        凭证仍是 POSTED，零报错（§3.15 的失败场景原样复现）；
      · 插重复行号能入库，且借贷仍然平、所有自检全绿。

    所以「恢复成功」的判据必须是「数据回来了 **且** schema 在 head」。
    """
    # ① 先确认这份备份确实是「护栏还不存在」的
    assert "uq_entry_voucher_line_no" not in _entry_unique_constraints(old_backup_db), \
        "这份备份不该已经有护栏 —— 那这条用例测的就不是「旧备份」了"

    src = old_backup_db
    proc = subprocess.run(db_backup.dump_command(src),
                          env=db_backup._env(db_backup._parse(src)),
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()

    # ② 恢复
    rc = db_backup.restore(scratch_db, proc.stdout)
    assert rc == 0, (f"恢复返回了 {rc} —— 数据恢复与 schema 对齐是两步，"
                     f"都要过。{rc} == db_backup.EXIT_SCHEMA_NOT_ALIGNED "
                     f"说明数据回来了但 schema 没对齐（护栏没在位）。")

    # ③ 恢复后的库：版本在 head，且护栏在
    from app.core.schema_version import head_revision

    eng = create_engine(scratch_db)
    try:
        with eng.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()
    assert ver == head_revision(), \
        f"恢复后的 schema 版本是 {ver}，不是 head —— 护栏是迁移带上去的，没对齐就没有护栏"

    uniq = _entry_unique_constraints(scratch_db)
    assert "uq_entry_voucher_line_no" in uniq, \
        f"恢复后的库没有行号唯一约束（现有：{uniq}）—— 护栏没了"


def test_schema版本认不出来时不能算恢复成功(scratch_db):
    """空库（没有 alembic_version）→ 必须报「没对齐」，不能是 0。

    **为什么退出码要分开。** 「恢复失败」（psql 非 0，库里什么都没有）与
    「数据回来了但 schema 没对齐」（数据在、护栏不在）是两件完全不同的事，
    处理方式也不同（前者重跑恢复，后者补迁移 + 查数据冲突）。混成一个码的
    后果：发布流程把「schema 没对齐」当成「恢复失败」，于是反复重跑恢复，
    而真正要做的事没人做。
    """
    rc = db_backup._align_schema(scratch_db)
    assert rc == db_backup.EXIT_SCHEMA_NOT_ALIGNED, rc
    assert rc != 0, "认不出版本却返回 0 —— 那就是在说「恢复好了」"
