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
from sqlalchemy import create_engine, event, select, text, update  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402


# --------------------------------------------------------------------------
# 会话时区钉死成 UTC
# --------------------------------------------------------------------------
#
# PostgreSQL 的 TimeZone GUC 决定 TIMESTAMPTZ 取回来时带什么 tzinfo 对象：
# 字面量恰好是 `UTC` 时 psycopg 交出 ``datetime.timezone.utc``；写成
# ``Etc/UTC``（GitHub 的 ``postgres:16`` 服务容器就是默认这个）它交出
# ``zoneinfo.ZoneInfo('Etc/UTC')``。
#
# 两者是**同一时刻**，`utcoffset()` 都是 0，但 ``== timezone.utc`` 为 False。
# 于是 ``test_importer.py::test_入库时间与记账时间同一时源`` 在本机绿、在 CI 红 ——
# 它测到的是「跑测试那台机器的时区配置」，不是被测代码。
#
# 所以修的是环境，不是断言：把断言改成「比较偏移量」看着也能变绿，但那条断言
# 本来就是护栏（时间戳必须是 UTC），放松它等于把护栏摘掉。这里对**每个新建连接**
# 执行一次 SET TIME ZONE，让断言回到它本来要测的东西。
# 本来是挂在 Engine 类上的全局监听（``event.listens_for(Engine, "connect")``），
# 那样更省事，但会把建库用的 admin 引擎也带上 —— 那个引擎是
# ``isolation_level="AUTOCOMMIT"`` 建的，而 ``SET TIME ZONE`` 一执行连接就进入
# 事务，psycopg 直接拒绝（``can't change 'autocommit' now``），整个套件起不来。
# 所以只挂在应用引擎上：管理引擎只做 CREATE/DROP DATABASE，不读时间戳。
@event.listens_for(engine, "connect")
def _pin_session_timezone(dbapi_conn, _record):
    with dbapi_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")


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
    finally:
        admin.dispose()

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    # 用迁移建库，而不是 create_all。这样「baseline 迁移能不能重建出一套
    # 能用的 schema」就不是靠人相信，而是被整份测试套件天天验证 ——
    # 建少一个触发器、少一列，后面 500 多条用例就会红。
    upgrade_to_head(engine)
    yield
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


# --------------------------------------------------------------------- 基础夹具


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
    from app.seed.chart_of_accounts import install_chart

    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"测试公司{tag}", name=f"账套{tag}", start_year=2026, start_month=1,
             init_subjects_at=datetime.now(timezone.utc), init_subjects_source="CHART")
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
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
    rows = db.scalars(
        select(Subject).where(Subject.book_id == book.id).order_by(Subject.code)
    ).all()
    return {s.code: s for s in rows}


@pytest.fixture
def make_voucher(db, book, admin_user):
    """建凭证的小工厂，自动补齐借贷平衡与默认科目。"""

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

        subs = {s.code: s for s in db.scalars(
            select(Subject).where(Subject.book_id == book.id)
        ).all()}

        def conv(rows):
            out = []
            for code, amt, s in rows or []:
                if isinstance(code, str):
                    subject = subs.get(code)
                    if subject is None:
                        raise AssertionError(f"测试科目不存在：{code}")
                    sid = subject.id
                else:
                    sid = code
                out.append({"subject_id": sid, "amount": str(amt), "summary": s})
            return out

        entries = [dict(e, direction="DEBIT") for e in conv(dr)]
        entries += [dict(e, direction="CREDIT") for e in conv(cr)]

        v = create_voucher(
            db, book_id=book.id, user=admin_user,
            voucher_date=d if isinstance(d, date) else date.fromisoformat(d),
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
