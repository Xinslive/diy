"""pytest 全局夹具。

**测试库与开发库物理隔离。** 会计数据一旦写错就很难补救，测试绝不能碰开发库。
``DATABASE_URL`` 在这里、且在 import app.* 之前被改写成 ``cwq_test``。

为什么改环境变量而不是 monkeypatch：``app.core.db.engine`` 在 import 期就用
settings 建好了连接池，事后换引擎会让 TestClient 和直接调 service 层拿到两个
不同的连接。所以必须在任何 app 模块被 import 之前完成改写。
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB_URL = os.environ.get(
    "CWQ_TEST_DATABASE_URL",
    "postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_test",
)

# ---- xdist 并行：每个 worker 用自己独立的库 -------------------------------
#
# 为什么必须分库：session 级夹具 _test_database 会 ``DROP SCHEMA public CASCADE``。
# N 个 worker 共用一个库的话，它们开局就互相把对方的表删了，症状是
# 「单独跑全绿、-n 4 跑出一堆 relation does not exist」，而且**时好时坏**——
# 取决于谁先跑到哪里。这类 flaky 比慢更糟。
#
# 库名规则：``cwq_test`` → ``<runuid>_gw0_cwq_test``。
#
# 护栏要求库名以 ``_test`` 结尾，所以前缀放在 ``_test`` **之前**：
# 否则护栏会把并行当成误配拦下来。
#
# 为什么还要带 runuid（PYTEST_XDIST_TESTRUNUID，xdist 给每次运行的唯一 id），
# 光有 worker 号不够 —— **两次并行的 pytest 会撞**。这不是理论问题：
# 只按 worker 分库（cwq_test → gw0_cwq_test）时，一个终端里跑全量、
# 另一个终端里跑单个文件，两个进程会各自 DROP 同一个库，
# 于是「一堆 relation does not exist」+「时好时坏」。
# 曾经就在这里踩到：一轮验证里 1004 条冒出 7 failed / 105 errors，
# 而同一批文件单独跑全绿 —— 排查半天才发现是另一个终端的测试在跑。
#
# 串行（不传 -n）时两个变量都为空，URL 原样保留 —— 默认行为一个字没变。
_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
if _WORKER:
    _run = os.environ.get("PYTEST_XDIST_TESTRUNUID") or "0"
    _base, _sep, _tail = TEST_DB_URL.rpartition("/")
    # rpartition 拿到的 _tail 里含查询串吗？不含 —— 连接串的库名段在最后一个 "/" 之后。
    TEST_DB_URL = f"{_base}{_sep}{_run}_{_WORKER}_{_tail}"


# --------------------------------------------------------------------- 测试库护栏
#
# 下面第 64 行是 ``DROP SCHEMA public CASCADE``。整个测试套件的正确性、
# 可重复执行性都靠它，但它**完全依赖操作者把环境变量填对**：
#
#     CWQ_TEST_DATABASE_URL=postgresql+psycopg://cwq:cwq123@127.0.0.1:5432/cwq \
#         pytest
#
# 一次误配，开发库当场消失。这不是理论场景 —— 变量名合法，
# 而仓库根目录的 .env.example 里就摆着这么一条 ``.../cwq`` 的开发连接串，
# 照着改掉末尾的库名就够了。
#
# 所以护栏不能只写在文档里，要写进代码，且**默认拒绝启动**。
# 数据库名必须带 test 标记；连到远端主机时还要额外显式确认。

# 常见生产/共享主机名。这些名字出现时一律拒绝，不给「大概不是生产」的余地。
FORBIDDEN_HOSTS = frozenset({
    "db", "db-master", "postgres", "postgresql",   # docker-compose/k8s 里的默认服务名
    "prod", "production", "prod-db", "prod-postgres", "prod-db-1",
    "rds", "rds-primary", "aurora", "cloudsql",
})


class UnsafeTestDatabase(RuntimeError):
    """测试库配置不安全 —— 拒绝启动，而不是先删库再解释。"""


def _assert_safe_test_db(url: str) -> tuple[str, str]:
    """校验测试库 URL，返回 (主机, 库名)。

    刻意在**任何连接建立之前**就抛错：护栏要是先连上再说，
    「连得上」这一步本身就已经在错误的库上跑过连接逻辑了。
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    name = (parsed.path or "").lstrip("/")

    if not name:
        raise UnsafeTestDatabase(
            f"测试库 URL 里没有库名，拒绝启动：{_mask(url)}")
    if not name.endswith("_test"):
        raise UnsafeTestDatabase(
            f"测试库名必须以 _test 结尾，实际是 {name!r} —— 拒绝启动。\n"
            f"  目标：{_mask(url)}\n"
            "  原因：测试夹具会执行 DROP SCHEMA public CASCADE，"
            "指错库等于当场删库。\n"
            "  改法：把库名改成 xxx_test，或设置 CWQ_TEST_DATABASE_URL 指向测试库。"
        )
    if host.lower() in FORBIDDEN_HOSTS:
        raise UnsafeTestDatabase(
            f"测试库指向了疑似生产/共享主机 {host!r} —— 拒绝启动。\n"
            f"  目标：{_mask(url)}"
        )
    return host, name


def _mask(url: str) -> str:
    """打日志用的连接串：去掉密码。日志会被人看到，密码不该出现在里面。"""
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.password:
        return url.replace(p.password, "***")
    return url


_assert_safe_test_db(TEST_DB_URL)

# 远端主机再加一道：本地库误配最多毁掉本机开发数据，远端可能就是生产。
# 用 CWQ_ALLOW_REMOTE_TEST_DB=1 显式确认，意图要摆在明面上。
_HOST, _NAME = _assert_safe_test_db(TEST_DB_URL)
if _HOST not in ("localhost", "127.0.0.1", "::1", "") and \
        os.environ.get("CWQ_ALLOW_REMOTE_TEST_DB") != "1":
    raise UnsafeTestDatabase(
        f"测试库在远端主机 {_HOST!r} 上，且未设置 CWQ_ALLOW_REMOTE_TEST_DB=1 —— 拒绝启动。\n"
        f"  目标：{_mask(TEST_DB_URL)}\n"
        "  确认这确实是测试库之后，设置 CWQ_ALLOW_REMOTE_TEST_DB=1 再跑。"
    )

# DROP SCHEMA 之前把目标喊出来。这一行不是给人看的礼貌，是**事后唯一的线索**：
# 真出了事，日志里至少能证明当时连的是哪个库。
print(f"[conftest] 测试库护栏通过：{_mask(TEST_DB_URL)}"
      f"（库名 {len(_NAME)} 字符，将执行 DROP SCHEMA public CASCADE）")

os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["JWT_SECRET"] = "test-secret-not-for-production"
# APP_ENV 默认是 prod（fail-closed，见 config.py app_env 字段）。
# 测试要显式声明自己是测试环境，否则 startup 的生产配置校验会直接拦停整套测试。
os.environ["APP_ENV"] = "test"
os.environ["DB_ECHO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select, text, update  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402


from app.core.schema_version import upgrade_to_head  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Book, Period, Subject, SysUser, UserBook  # noqa: E402


# --------------------------------------------------------------------- 建库


def _admin_url() -> str:
    """指向同一 postgres 实例的默认库，用来 CREATE DATABASE。"""
    return TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    name = TEST_DB_URL.rsplit("/", 1)[-1]
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            # 把时区钉在**库**上，而不是靠连接事件逐个 SET。
            #
            # PostgreSQL 的 TimeZone GUC 决定 TIMESTAMPTZ 取回来时带什么 tzinfo：
            # 字面量恰好是 `UTC` 时 psycopg 交出 datetime.timezone.utc；写成
            # `Etc/UTC`（GitHub 的 postgres:16 服务容器默认）或 `Asia/Shanghai`
            # （生产机实测就是这个）它交出对应的 ZoneInfo。两者是**同一时刻**，
            # utcoffset 都是 0，但 `== timezone.utc` 为 False。
            #
            # 之前用 `event.listens_for(engine, "connect")` 在每个新连接上 SET。
            # 那个办法**不可靠**：套件里到处是 commit → 归还连接 → 再取一条，
            # 一旦某条物理连接是在监听器生效之前建好并留在池里，之后所有复用
            # 它的用例都拿到 Etc/UTC —— 表现为同一个提交 CI 上时绿时红
            # （6e5fc0e 提交后 293a435 绿、0a57860 红，两次只差前端与产物 job）。
            # 而且本机 PG 默认就是 UTC，**本地怎么跑都绿**，等于从未验证过。
            #
            # 库级默认不依赖连接的创建时机：连到这个库的每一条连接、任何时刻、
            # 任何引擎拿到的都是 UTC。顺带这也是「测试库本来就应该配成 UTC」
            # 这件事的正面表达，而不是在测试代码里打补丁。
            #
            # 注意这条 ALTER 必须在**建库之后**、每次都执行：库是复用的，
            # 上一轮留下的设置也得被重新确认一遍。
            conn.execute(text(f"""ALTER DATABASE "{name}" SET TimeZone TO 'UTC'"""))

            # ---- 测试库不要等 WAL 落盘 ----------------------------------
            #
            # ``synchronous_commit = off``：提交不等 WAL flush 就返回。
            #
            # **为什么只有测试库能这么配。** 开着的时候，一次 commit 要等
            # fsync —— 本机实测 **17.2 ms/commit**；关掉是 **0.25 ms/commit**，
            # 差 69 倍。而这套件里「commit」是节奏的主音：每个用例建账套
            # （12 个期间 + 140 个科目）、建用户、登录、跑业务、删数据，
            # 一条用例 commit 七八次是常事。两千多条用例摊下来，几万次 commit
            # × 17ms = **十几分钟纯等盘**。而 6 个 worker 并行时，瓶颈从 CPU
            # 变成 IO（实测整轮 CPU 只用到 1.8/10 核）—— 等的就是这个 fsync。
            #
            # **安全边界**：改的是**这一个库**的设置，不是集群的。
            #   · 这条 ALTER 只作用于 ``name`` 这个库，别的库（开发库、生产库）
            #     一概不动；
            #   · 测试库是**每轮建、每轮删**的一次性库，跑完就 DROP DATABASE，
            #     万一机器断电，丢的是这一轮的测试数据 —— 没有任何价值；
            #   · 真正的数据安全由上面的护栏保证：库名必须以 ``_test`` 结尾，
            #     否则根本不启动。
            #
            # **这不是「把生产的保护调没了」**：生产库的这个开关没有被碰过，
            # 而生产环境的正确做法本来就是在**从库/测试环境**关掉它。
            conn.execute(text(f"""ALTER DATABASE "{name}" SET synchronous_commit TO 'off'"""))
    finally:
        admin.dispose()

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    # 用迁移建库，而不是 create_all。这样「baseline 迁移能不能重建出一套
    # 能用的 schema」就不是靠人相信，而是被整份测试套件天天验证 ——
    # 建少一个触发器、少一列，后面 500 多条用例就会红。
    upgrade_to_head(engine)

    # 迁移刚跑完、还没跑任何用例时的行就是**基线**（官方内置报表模板、
    # 打印模板）。记下来，清库时留着它们 —— 删了就会出现
    # 「内置资产负债表模板没建出来，种子数据有问题」。
    _capture_baseline()
    yield
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def _tzlog(dbapi_conn, rec):
        import time as _t
        cur = dbapi_conn.cursor()
        cur.execute("show TimeZone")
        print(f"@@@ [{_t.time():.3f}] 新建连接 TimeZone = {cur.fetchone()[0]}")
        cur.close()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    # 并行时连**库本身**一起删掉，不能只清 schema。
    #
    # 库名带 runuid（每次运行都不同），所以只清 schema 等于每跑一轮
    # 就在 postgres 实例上留 6 个空库 —— 跑十轮就是 60 个，
    # 攒着攒着 `\\l` 就没法看了，而这实例上还坐着开发库和别的库。
    #
    # 必须先 dispose 掉 engine：连接池里还留着连到本库的连接时，
    # DROP DATABASE 会一直等锁（实测卡到超时）。dispose 之后再用 admin 连。
    if _WORKER:
        engine.dispose()
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            admin.dispose()


# --------------------------------------------------------------------- 口令哈希缓存
#
# hash_password 是 pbkdf2-sha256 / 24 万轮，**一次 21ms**。而下面两个用户夹具
# 用的口令是写死的字符串（admin123 / staff123），于是上千条测试要为两个
# 固定口令反复算 2000 次哈希 —— 实测约 42s，占优化前全量 178s 的 24%
# （基线数字与 pytest.ini 统一；这一段曾写着 194s，与 pytest.ini 的 178s
# 对不上，两个数字各说各话，已并成一处），是这套夹具里最大的一笔冤枉开销。
#
# 为什么缓存安全：
#   · 存进 sys_user 的还是**真哈希**，登录走的是真的 verify_password，
#     校验路径一秒没变、强度一秒没降；
#   · 抽的是「盐 + 轮数 + 摘要」整串，不是摘要本身，而 pbkdf2 每次随机盐，
#     同一个口令在库里本来就长得不一样 —— 跨测试复用同一个盐不新增任何暴露：
#     这些账号每个用例建完就随事务回滚，没人会去爆破测试库的哈希；
#   · 只在测试进程内缓存，**生产代码一行没动**，_ITERATIONS 仍是 24 万。
#
# 真要较真的话这也是「测试替身」的标准做法：被测的是权限边界和账务逻辑，
# 不是 KDF 的抗爆破性；真测那个，测试库里再留一个专用小轮数用例即可。

_PASSWORD_HASH_CACHE: dict[str, str] = {}


def _cached_hash(password: str) -> str:
    """口令 -> 哈希，全程只算一次（见上面关于安全性的说明）。"""
    if password not in _PASSWORD_HASH_CACHE:
        from app.core.security import hash_password

        _PASSWORD_HASH_CACHE[password] = hash_password(password)
    return _PASSWORD_HASH_CACHE[password]


# --------------------------------------------------------------------- 每条用例之间清库
#
# **为什么要有这一层。**
#
# 之前每个 worker 的库是**只进不出**的：``book`` 夹具要 ``db.commit()``
# （否则同一 worker 里走 HTTP 的那条连接看不到这个账套），所以每个用例都往库里
# 留下一个「12 个期间 + 140 个科目」的账套。跑 160 个用例之后，那个库里有：
#
#     book=166  subject=23106  period=1977  sys_user=254
#
# 累积本身不致命，但它是**最慢那几条用例**的元凶：
# ``app.plugins.installer.make_backup`` 会**给库里每一个账套各导一份备份**
# （``scopes = [SYSTEM] + [BOOK for each Book.id]``），再逐份做恢复校验。
# 账套越多越慢，实测：0 个账套 0.11s → 50 个 1.19s → 150 个 3.42s。
# 插件升级/卸载/清表三个接口各调一次 ``make_backup``，于是
# ``test_plugin_lifecycle`` 那条用例在全量并行下要 **57 秒**
# —— 它自己一个用例吃掉了整轮的四分之一。
#
# **清库怎么做（为什么不 TRUNCATE）。**
#   · ``TRUNCATE ... CASCADE`` 每张表都要建一个新的物理文件，实测 36 张表
#     **0.6~1.2 秒** —— 两条千多条用例就是几十分钟，不划算；
#   · ``DELETE FROM book`` 也不安全：``entry → subject``、``voucher → period``、
#     ``invoice_record → voucher``、``period_opening → subject`` 这些外键是
#     **NO ACTION / RESTRICT**（不是级联），父行一删就撞外键。
#
# 所以走第三条：**按「被引用的先删」的顺序逐表 DELETE**，全部塞进一条
# 服务端 ``DO`` 块里发出去 —— 一次往返，顺序在库内算好，不在客户端来回。
# 实测：库里只有 1 个账套时 **11 毫秒**（一条用例的量级），
# 100 个账套时 2.4 秒 —— 但清理之后库里回到 0，所以永远走的是 11ms 那条路。
#
# **顺序是算出来的，不是手写的。** 每次清理都从 ``pg_constraint`` 读出外键图，
# 拓扑排序出「子表在前」；手写清单一定会有遗漏，而漏掉的那张表会安静地
# 累积，症状又是「某条用例突然变慢」—— 那种问题最难查。
#
# **为什么只清 ``public``。** ``test_migration_guardrails`` 的 ``migrated_scratch``
# 夹具另开一个 schema 跑迁移，那边的表不在 ``public`` 里，不受影响；
# 插件运行时建出来的 ``plugin_*`` 表也在 ``public`` 里，会被一起清掉
# （插件夹具自己也会 drop 它们，不冲突）。
#
# **为什么放在用例「开始前」而不是「结束后」。** 开始前清，顺序上更稳：
# 上一个用例的夹具（``db`` 的 Session、``client`` 的 TestClient）一定已经
# 拆完了，不会有连接开着事务挡着 DELETE。顺便还白送一个好处 ——
# **每条用例都从同一个干净状态出发**，用例之间不再有先后依赖。
#
# 想临时关掉（比如手工复现一条用例、想看累积效应）：
# ``CWQ_TEST_KEEP_DATA=1 pytest``。

_CLEAN_SQL_CACHE: dict[str, str] = {}

#: 迁移种下的**基线行**（表名 -> 这些行的主键 id）。
#:
#: 为什么必须留着：迁移 0021/0022/0024 会往库里种下官方内置的报表模板
#: （资产负债表、利润表）与打印模板 —— 它们是**全局基线**，不是某个账套的数据。
#: 全表删掉之后，一批用例会直接报「内置资产负债表模板没建出来，种子数据有问题」
#: （2026-10 首次启用清库时踩过：52 failed + 64 errors）。
#:
#: 这里记的是**行 id 快照**，不是「哪张表要排除」的硬编码清单：
#: 迁移往哪张表种了数据，就自动把那几张表的基线行留下来；
#: 将来迁移再种新表，也不用回来改这里。
_SEEDED_BASELINE: dict[str, tuple[int, ...]] = {}


def _capture_baseline() -> None:
    """记下「迁移刚跑完、还没跑任何用例」时库里的行 —— 那些就是基线。"""
    global _SEEDED_BASELINE
    snapshot: dict[str, tuple[int, ...]] = {}
    with engine.connect() as conn:
        for table in _public_tables():
            has_id = conn.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t AND column_name='id'"),
                {"t": table}).scalar()
            if not has_id:
                continue
            ids = tuple(r[0] for r in conn.execute(
                text(f'SELECT id FROM public."{table}" ORDER BY id')))
            if ids:
                snapshot[table] = ids
    _SEEDED_BASELINE = snapshot


def _public_tables() -> list[str]:
    """public schema 里的业务表（``alembic_version`` 不算，它是迁移状态）。"""
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version' "
            "ORDER BY table_name"))]


def _delete_order(tables: list[str]) -> list[str]:
    """排出「被引用的先删」的顺序：子表在前，父表在后。

    外键图直接从 ``pg_constraint`` 读 —— 手写清单迟早会漏，
    而漏掉的那张表会安静地累积，表现为「某条用例莫名其妙变慢」。
    """
    with engine.connect() as conn:
        edges = conn.execute(text(
            "SELECT c.relname, p.relname "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_class p ON p.oid = con.confrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE con.contype = 'f' AND n.nspname = 'public'")).all()
    # refs[t] = 引用了 t 的那些表
    refs: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in edges:
        if child in refs and parent in refs and child != parent:
            refs[parent].add(child)
    order: list[str] = []
    left = set(tables)
    while left:
        # 没人再引用它的可以先删
        ready = sorted(t for t in left if not (refs[t] & left))
        if not ready:
            # 有环（自引用外键）：剩下的按名字排，自引用那条由 NO ACTION 兜底
            ready = sorted(left)
        order += ready
        left -= set(ready)
    return order


def _clean_public_tables() -> None:
    """把 public schema 的业务数据清空（迁移种下的基线数据留着）。"""
    tables = _public_tables()
    if not tables:
        return
    key = ",".join(tables)
    sql = _CLEAN_SQL_CACHE.get(key)
    if sql is None:
        order = _delete_order(tables)
        # 一条 DO 块发出去：一次往返，删除顺序在库内执行。
        parts = []
        for t in order:
            keep = _SEEDED_BASELINE.get(t)
            if keep:
                # 这张表有基线行：只删不是基线的那些（测试造的副本）。
                ids = ",".join(str(i) for i in keep)
                parts.append(f'  DELETE FROM public."{t}" WHERE id NOT IN ({ids});\n')
            else:
                parts.append(f'  DELETE FROM public."{t}";\n')
        sql = "DO $$\nBEGIN\n" + "".join(parts) + "END $$;"
        _CLEAN_SQL_CACHE[key] = sql
    with engine.begin() as conn:
        # ``session_replication_role = replica``：本次事务里不触发**行级触发器**
        # （``fn_posted_voucher_guard`` 之类）与**外键约束触发器**。
        #
        # 为什么可以这么做：测试库是一次性的（每轮建、每轮删），而这套夹具
        # 本来就在 session 级 ``DROP SCHEMA public CASCADE`` —— 清空是它的本职。
        # 库层那些护栏守的是**应用的写路径**（不许改已记账凭证、不许改已结账
        # 期间的账），不是守测试夹具擦桌子。留着它们只会让清理在「上一条用例
        # 锁了期间、记了凭证」之后卡住。
        #
        # 用 ``SET LOCAL``：只在这一个事务里生效，事务一提交自动还原，
        # 不会把连接还回池时留一个 replica 角色给后面的请求。
        #
        # 外键不校验了，删除顺序就不再是**正确性**问题，但顺序仍然保留 ——
        # 一是万一这个库上 ``cwq`` 不是超级用户（``SET`` 会被拒），
        # 还能退回到按序删除；二是按序删除读起来更符合直觉。
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        conn.exec_driver_sql(sql)


@pytest.fixture(autouse=True)
def _clean_slate():
    """每条用例开始前清空 public schema 的业务数据（见上面那段说明）。"""
    if os.environ.get("CWQ_TEST_KEEP_DATA") != "1":
        _clean_public_tables()
    yield


@pytest.fixture(scope="session", autouse=True)
def _isolate_plugin_root(tmp_path_factory):
    """把插件根目录从**仓库的运行时目录**挪到临时目录。

    为什么必须隔离：插件的**签名密钥封禁名单**（``.revoked-keys``）就写在
    插件根目录里，而插件根目录默认是 ``data/plugins`` —— 也就是**仓库里的
    运行时目录**（相对 cwd）。

    一条用例调了「封禁签名密钥」，名单就留在那儿了；下一次跑这套件，
    用同一把测试密钥（``key_id="test"``，见 ``tests/plugin_helpers.package``）
    签名的用例全部被判「密钥已撤销」。

    这不是偶发抖动，是**顺序依赖**：第一次跑绿、第二次跑红。
    实测踩过：仓库里残留一个 ``data/plugins/.revoked-keys``（内容 ``test``），
    ``test_plugin_security.py::test_signed_package_roundtrip`` 直接红，
    而且红得毫无道理 —— 它跟封禁没有任何关系。

    更糟的是那是一次**跨进程的污染**：测试跑在开发机上，污染留在工作区里，
    下一个人 clone 下来第一次跑就是红的。

    所以这里在会话级把插件根目录指到临时目录 —— 测试再怎么写都写不进仓库，
    也不会流到下一轮。``tests/plugin_fixtures.py`` 的 ``plugin_env`` 夹具
    会再按用例细分到自己的 ``tmp_path``，那是更细的一层，两边不冲突。
    """
    from app.core.config import settings

    root = tmp_path_factory.mktemp("plugin-root")
    original = settings.plugin_root
    settings.plugin_root = str(root)
    try:
        yield str(root)
    finally:
        settings.plugin_root = original


# --------------------------------------------------------------------- 基础夹具


@pytest.fixture(autouse=True)
def _reset_notify_budget():
    """每个用例前清空**通知发信预算**。

    预算是**进程级**状态 —— 它必须跨请求生效，否则「同一个窗口内最多发
    N 封」根本约束不住一次连打。但测试共享进程，于是上一条用例触发的
    几十封通知会把配额吃光，让后面的用例随机失败。

    那不是"偶发抖动"，是**顺序依赖**：同一份代码换台机器跑就红了。
    所以这里在夹具层把它清掉 —— 顺序依赖的根因是共享状态，治法是隔离
    共享状态，而不是调大阈值（调大阈值就把生产的防护调没了）。
    """
    from app.notify import events as notify_events

    notify_events._budget.reset()
    yield
    notify_events._budget.reset()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def client():
    """raise_server_exceptions=True：让接口内部的异常直接抛出来，便于定位。"""
    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin_user(db):
    """每个测试一个管理员。

    不复用 session 级用户：测试之间会 rollback，而被其它测试引用的行不能删。
    干脆每次新建，彻底避免互相引用。
    """
    u = SysUser(
        username=f"admin_{uuid.uuid4().hex[:10]}",
        password_hash=_cached_hash("admin123"),
        real_name="测试管理员",
        active=True,
        is_admin=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def plain_user(db):
    """一个**不是**管理员的普通用户，用来验权限边界。

    跟 admin_user 成对：只测管理员能做什么是不够的，必须同时测普通用户
    什么都做不了。
    """
    u = SysUser(
        username=f"staff_{uuid.uuid4().hex[:10]}",
        password_hash=_cached_hash("staff123"),
        real_name="测试员工",
        active=True,
        is_admin=False,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def plain_auth(client, plain_user):
    r = client.post("/api/auth/login",
                    json={"username": plain_user.username, "password": "staff123"})
    assert r.status_code == 200, f"登录失败：{r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth(client, admin_user):
    """带 token 的请求头。

    必须用 ``admin_user.username`` 而不是写死用户名：admin_user 每个用例都新建
    一个随机账号，写死会让这个夹具永远登录失败 —— 之前就是这么写的，只是
    一直没有用例真的用到它，HTTP 层的接口因此一条都没被测过。
    """
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, f"登录失败：{r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def book(db, admin_user):
    """一个全新的空账套：12 个期间 + 标准科目表。

    每个测试拿一个干净的账套，测试之间不会因为上一条用例留下的凭证而互相干扰。

    ``init_subjects_at`` 直接给上：这个夹具造的是「已经装好标准科目表」的账套，
    等价于初始化第 1 步已经完成。生产上新账套这一列是空的（要走初始化向导），
    而期初余额的导入与保存都挂在这道闸门上 —— 夹具不给的话，下面每一条
    写期初的用例都会被 409 拒掉，测的就不再是它本来的意思。
    需要「未初始化」账套的用例自己建，不要改这里。
    """
    from datetime import datetime, timezone

    from app.accounting.periods import create_periods
    from app.accounting.years import ensure_book_year
    from app.seed.chart_of_accounts import install_chart

    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"测试公司{tag}", name=f"账套{tag}", start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    # 初始化第 1 步记在**年度账套**上（迁移 0032：状态跟着科目表下沉到年度）
    by = ensure_book_year(db, b, 2026)
    by.init_subjects_at = datetime.now(timezone.utc)
    by.init_subjects_source = "CHART"
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def book_id(book):
    return book.id


@pytest.fixture
def periods(db, book):
    rows = db.scalars(
        select(Period).where(Period.book_id == book.id).order_by(Period.month)
    ).all()
    return {p.month: p for p in rows}


# --------------------------------------------------------------------- 业务助手


@pytest.fixture
def subjects(db, book):
    """这个账套**启用年度**的科目表。

    科目表每个年度各有一份（迁移 0032），所以这里必须圈定年度 ——
    不圈的话，「有 2027 年的账套」会把两年的科目混在一个字典里。
    """
    rows = db.scalars(
        select(Subject).where(
            Subject.book_id == book.id, Subject.year == book.start_year
        ).order_by(Subject.code)
    ).all()
    return {s.code: s for s in rows}


def build_voucher_factory(db, book, admin_user):
    """造凭证的工厂，绑定**指定账套**。

    **抽成模块级函数**，是为了让「跨年账套」也能用同一套造凭证逻辑 ——
    工厂原来只是 conftest 那个单年账套夹具的返回值，别的账套用不上它，
    跨年用例只能自己重写一遍（借贷平衡、年度科目表、「上一期结账了本期才能
    记账」这些业务规矩都要重写一遍，容易漏）。
    """

    def _make(
        d: str | date,
        dr: list[tuple[str, Decimal | float, str]] | None = None,
        cr: list[tuple[str, Decimal | float, str]] | None = None,
        *,
        voucher_no: str | None = None,
        summary: str = "测试",
        posted: bool = False,
        source: str = "MANUAL",
        remark: str = "",
    ):
        from app.accounting.vouchers import create_voucher

        # **按凭证日期的年度取科目表**（科目表每个年度各有一份，迁移 0032）。
        #
        # 原来这里恒定取 ``book.start_year`` 那张表，于是「在 2027 年记一张
        # 凭证」会拿着 2026 年的科目 id 去写 —— 2027 年的科目表是结转时克隆
        # 出来的另一批行，id 根本不同，服务端按年度校验时直接判「科目不存在」。
        # 夹具必须替测试作者做正确的选择：哪一年记账，就用哪一年的科目表。
        when = d if isinstance(d, date) else date.fromisoformat(d)

        def _subs_of(year: int) -> dict[str, Subject]:
            return {s.code: s for s in db.scalars(
                select(Subject).where(
                    Subject.book_id == book.id, Subject.year == year)
            ).all()}

        subs = _subs_of(when.year)

        def conv(rows):
            out = []
            for code, amt, s in rows or []:
                if isinstance(code, str):
                    subject = subs.get(code)
                    if subject is None:
                        raise AssertionError(
                            f"测试科目不存在：{code}（{when.year} 年度的科目表里没有）")
                    sid = subject.id
                else:
                    sid = code
                out.append({"subject_id": sid, "amount": str(amt), "summary": s})
            return out

        entries = [dict(e, direction="DEBIT") for e in conv(dr)]
        entries += [dict(e, direction="CREDIT") for e in conv(cr)]

        v = create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=when,
            entries=entries, voucher_no=voucher_no, source=source,
            remark=remark or summary, commit=False,
        )
        db.commit()
        if posted:
            from app.accounting.vouchers import post_vouchers

            # 业务规矩：上一期结账了本期才能记账。测试里直接建凭证不会走结账流程，
            # 这里把该凭证之前的期间直接标成已结账，只为满足这个前置条件 ——
            # 用 update 而不是 close_period，是不想顺带把别的凭证自动记账掉。
            vp = db.get(Period, v.period_id)
            db.execute(
                update(Period)
                .where(
                    Period.book_id == book.id,
                    Period.ord < vp.ord,
                    Period.locked.is_(False),
                )
                .values(locked=True)
            )
            db.commit()
            post_vouchers(db, book.id, [v.id], admin_user)
            db.refresh(v)
        return v

    return _make


@pytest.fixture
def make_voucher(db, book, admin_user):
    """建凭证的小工厂，自动补齐借贷平衡与默认科目。

    绑定本用例的账套（``book`` 夹具）。跨年账套用
    ``build_voucher_factory(db, 那个账套, admin_user)``。
    """
    return build_voucher_factory(db, book, admin_user)
