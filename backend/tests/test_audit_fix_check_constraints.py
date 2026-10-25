"""库级 CHECK 护栏必须在 ORM 模型侧**同名同义**地声明（审计 P3-2 / P3-22）。

``app/models/accounting.py`` 里立过一条规矩：

    「**库级 CHECK 约束，模型侧必须同名同义**（审计 P3-2）」

理由是「模型 == 库」这条判据对 CHECK 约束**结构性失效**：Alembic 1.20 的
``compare_metadata`` 根本不比较 CHECK 约束 —— 缺了不会报、补了也不会报，
**两条路都看不见**。于是「以模型为准的心智模型、静态检查、基于模型的
测试工具」对漏掉的那几条约束完全失明。

**这次补的是哪几条。** 库里（public schema）有 **19** 条 CHECK 约束，模型
元数据只声明了 **11** 条 —— 库有模型无的有 8 条，其中 7 条在这一份的
分工内（``ck_voucher_source`` 归 ``models/accounting.py``）：

    admin_mfa_challenge  ck_admin_mfa_challenge_status
    sys_backup_run       ck_sys_backup_run_scope / _tier / _status
    plugin_cohort        plugin_cohort_id_check / plugin_cohort_expected_workers_check
    plugin_registry      plugin_registry_access_default_check

**注意数量。** 审计报告 §3.22 写的是「库里 21 条 CHECK」，那个数字把
``information_schema`` 里两个**域**（domain）上的约束算进去了
（``cardinal_number_domain_check`` / ``yes_or_no_check``）—— 它们不是
业务表上的约束，也不在 public schema 里。实际业务约束是 **19** 条
（迁移从零建起的库与活库 ``cwq`` 都是 19 条，集合完全相同）。下面这条
回归守卫是**拿活库比**，不是拿一个写死的数字比 —— 数字会变，集合不会。

**「同名同义」怎么验。** PostgreSQL 会把 ``IN (...)`` 归一化成
``= ANY(ARRAY[...])``，所以两边的**字面文本**不会一致。这里比的是
「约束名」+「约束里出现的字面量与数字」—— 那是「同义」的可机械判定
部分；再加上「库层护栏真的还活着」（插一条坏数据必须被挡下），
「同义」这件事才算验完。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from app.models import AdminMfaChallenge, BackupRun, PluginCohort, PluginRegistry
from app.models.base import Base

#: 这一份补上的 7 条：约束名 → 它挂在哪张表上。
_EXPECTED = {
    "admin_mfa_challenge": [
        "ck_admin_mfa_challenge_status",
    ],
    "sys_backup_run": [
        "ck_sys_backup_run_scope",
        "ck_sys_backup_run_status",
        "ck_sys_backup_run_tier",
    ],
    "plugin_cohort": [
        "plugin_cohort_expected_workers_check",
        "plugin_cohort_id_check",
    ],
    "plugin_registry": [
        "plugin_registry_access_default_check",
    ],
}


def _db_checks(db) -> dict[str, str]:
    """活库里 public schema 的 CHECK 约束：名字 → 定义。"""
    rows = db.execute(text(
        "SELECT conname, pg_get_constraintdef(oid) "
        "FROM pg_constraint "
        "WHERE contype = 'c' "
        "AND connamespace = 'public'::regnamespace")).all()
    return {r[0]: r[1] for r in rows}


def _orm_checks() -> dict[str, tuple[str, str]]:
    """ORM 元数据里的 CHECK 约束：名字 → (表名, 定义)。"""
    out: dict[str, tuple[str, str]] = {}
    for table_name, table in Base.metadata.tables.items():
        for con in table.constraints:
            if not isinstance(con, CheckConstraint):
                continue
            out[con.name] = (table_name, str(con.sqltext))
    return out


def _literals(sql: str) -> set:
    """把一段 SQL 里的「字面量」抽出来：引号里的字符串 + 整数。

    PostgreSQL 会把 ``IN ('A','B')`` 渲染成
    ``= ANY (ARRAY['A'::character varying, 'B'::character varying])`` ——
    渲染后的文本与模型里写的**不一样**，但「允许哪些取值」这件事是
    一样的。这里比的就是后者。
    """
    strings = set(re.findall(r"'([^']*)'", sql))
    numbers = set(re.findall(r"\b\d+\b", sql))
    return strings | numbers


# --------------------------------------------------------------------- 集合比对


def test_ORM声明了库里每一条CHECK约束(db):
    """**这条是这条缺陷的回归守卫。**

    库里有的 CHECK 约束，模型侧必须一条不少地声明出来。以前缺 8 条 ——
    而 Alembic 不会报、静态检查看不见、只有拿库比才看得见。
    """
    db_checks = _db_checks(db)
    orm_checks = _orm_checks()

    missing = sorted(set(db_checks) - set(orm_checks))
    assert not missing, (
        "这些库级 CHECK 约束在 ORM 模型侧没有声明（审计 P3-2/P3-22）：\n"
        + "\n".join(f"  {name}: {db_checks[name]}" for name in missing))

    extra = sorted(set(orm_checks) - set(db_checks))
    assert not extra, (
        "这些 CHECK 约束模型侧有、库里没有 —— 同样是「模型 == 库」失明：\n"
        + "\n".join(f"  {name}: {orm_checks[name][1]}" for name in extra))


def test_模型与库的CHECK约束数量一致(db):
    """数量也要对得上 —— 只比集合不比数量，会漏掉「同名不同义」的重复声明。"""
    assert len(_orm_checks()) == len(_db_checks(db)), (
        f"模型声明了 {len(_orm_checks())} 条 CHECK，库里有 {len(_db_checks(db))} 条")


@pytest.mark.parametrize("table_name,names", sorted(_EXPECTED.items()))
def test_这一份补上的约束都在模型里(db, table_name, names):
    """逐表点名：约束名与**挂在哪张表上**都要对。

    「挂在哪张表上」不是细节 —— 一条 ``ck_sys_backup_run_scope`` 挂到
    ``sys_backup_run`` 上才对；挂到别的表上，autogenerate 就会把它当成
    「库里有、模型里没有」。
    """
    orm_checks = _orm_checks()
    for name in names:
        assert name in orm_checks, f"模型侧没有声明 {name}"
        assert orm_checks[name][0] == table_name, (
            f"{name} 挂在 {orm_checks[name][0]} 上，应当挂在 {table_name} 上")


@pytest.mark.parametrize("table_name,names", sorted(_EXPECTED.items()))
def test_补上的约束与库里的语义一致(db, table_name, names):
    """**同名同义**：约束里允许的取值必须一模一样。"""
    db_checks = _db_checks(db)
    orm_checks = _orm_checks()
    for name in names:
        want = _literals(db_checks[name])
        got = _literals(orm_checks[name][1])
        assert got == want, (
            f"{name} 的语义与库里不一致：\n"
            f"  库：{db_checks[name]}\n"
            f"  模型：{orm_checks[name][1]}\n"
            f"  字面量差异：只在库里的 {sorted(want - got)}，只在模型里的 {sorted(got - want)}")


def test_插件的单行守卫与容量上限在模型里(db):
    """``plugin_cohort`` 的两条护栏要连**含义**一起钉住。

    * ``id = 1``：这张表全局只允许有一行（当前这一代 worker 的租约世代）。
      应用层万一插了第二行，租约就分裂成两代，worker 的排他租约随之失效。
    * ``expected_workers BETWEEN 1 AND 64``：预计 worker 数是小整数，
      0 与 1000 都是配置事故。
    """
    orm = _orm_checks()
    assert _literals(orm["plugin_cohort_id_check"][1]) == {"1"}, (
        f"单行守卫的语义不对：{orm['plugin_cohort_id_check'][1]}")
    assert _literals(orm["plugin_cohort_expected_workers_check"][1]) == {"1", "64"}, (
        f"容量上限的语义不对：{orm['plugin_cohort_expected_workers_check'][1]}")


def test_插件的默认访问权限守卫在模型里(db):
    """``plugin_registry.access_default = 'none'`` 是**安全护栏**。

    插件默认拿不到任何用户数据，只有显式授权之后才能碰到。这条约束
    一旦在模型侧消失，「以模型为准」的心智模型就看不见它了。
    """
    orm = _orm_checks()
    assert _literals(orm["plugin_registry_access_default_check"][1]) == {"none"}, (
        f"安全护栏的语义不对：{orm['plugin_registry_access_default_check'][1]}")


# --------------------------------------------------------------------- 库层还活着


def _expect_check_violation(db, fn, what):
    """插一条坏数据，必须被**库层**的 CHECK 挡下（sqlstate 23514）。"""
    db.rollback()
    try:
        fn()
    except IntegrityError as exc:
        db.rollback()
        assert getattr(exc.orig, "sqlstate", None) == "23514", (
            f"{what} 被挡下了，但不是 CHECK 约束挡的：{exc.orig}")
        return
    db.rollback()
    raise AssertionError(f"{what} 居然写进去了 —— 库层护栏没生效")


def test_库层的备份口径护栏还活着(db):
    """``scope`` 写成 ``BOOKS`` 必须被挡下（清理会按错误的层级删备份）。"""
    def bad():
        db.add(BackupRun(book_id=None, scope="BOOKS", book_label="", tier="daily",
                          status="success", oss_key="", size_bytes=0, row_count=0,
                          checksum="", message="",
                          started_at=datetime.now(timezone.utc)))
        db.flush()
    _expect_check_violation(db, bad, "scope='BOOKS'")


def test_库层的备份分级护栏还活着(db):
    def bad():
        db.add(BackupRun(book_id=None, scope="BOOK", book_label="", tier="hourly",
                          status="success", oss_key="", size_bytes=0, row_count=0,
                          checksum="", message="",
                          started_at=datetime.now(timezone.utc)))
        db.flush()
    _expect_check_violation(db, bad, "tier='hourly'")


def test_库层的备份状态护栏还活着(db):
    def bad():
        db.add(BackupRun(book_id=None, scope="BOOK", book_label="", tier="daily",
                          status="ok", oss_key="", size_bytes=0, row_count=0,
                          checksum="", message="",
                          started_at=datetime.now(timezone.utc)))
        db.flush()
    _expect_check_violation(db, bad, "status='ok'")


def test_库层的二次验证状态护栏还活着(db, admin_user):
    def bad():
        db.add(AdminMfaChallenge(
            user_id=admin_user.id, email_token_hash="x" * 64,
            client_secret_hash="y" * 64, status="VERIFIEDD",
            expires_at=datetime.now(timezone.utc)))
        db.flush()
    _expect_check_violation(db, bad, "status='VERIFIEDD'")


def test_库层的插件单行守卫还活着(db):
    def bad():
        db.add(PluginCohort(id=2, generation="g", started_at=0, expected_workers=1))
        db.flush()
    _expect_check_violation(db, bad, "plugin_cohort.id=2")


def test_库层的插件容量上限还活着(db):
    def bad():
        db.add(PluginCohort(id=1, generation="g", started_at=0, expected_workers=100))
        db.flush()
    _expect_check_violation(db, bad, "plugin_cohort.expected_workers=100")


def test_库层的插件默认访问权限护栏还活着(db):
    def bad():
        db.add(PluginRegistry(plugin_id="p", version="1", plugin_digest="d",
                              desired_revision="r", access_default="all"))
        db.flush()
    _expect_check_violation(db, bad, "plugin_registry.access_default='all'")
