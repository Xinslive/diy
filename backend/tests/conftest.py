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
    from app.core.security import hash_password

    u = SysUser(
        username=f"admin_{uuid.uuid4().hex[:10]}",
        password_hash=hash_password("admin123"),
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
    from app.core.security import hash_password

    u = SysUser(
        username=f"staff_{uuid.uuid4().hex[:10]}",
        password_hash=hash_password("staff123"),
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
    """
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"测试公司{tag}", name=f"账套{tag}", start_year=2026, start_month=1)
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
