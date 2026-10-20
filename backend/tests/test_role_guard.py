"""账套角色判定必须是白名单语义（审计 P2-12）。

原来的判定是 ``if link.role == "VIEWER"`` —— **字面量比较**。这意味着：

    UPDATE sys_user_book SET role = 'viewer' WHERE user_id = 2;

执行完之后，这个人拿到了完整写权限。绕过 API 直接改库的路径上没有任何东西
拦得住 psql、数据修复脚本、或者一个误连到生产库的 ETL —— 而本项目在
别处的第一原则恰恰是 fail-closed（``config.py`` 的弱密钥直接拒绝启动、
写门禁按 OWNER 收紧、锁定计数放库里）。**只在权限判定这一处 fail-open，
等于把整套原则架空。**

两层防线，各自独立成立：

1. **应用层**（``core/deps.py::is_read_only``）：不认识的角色一律按只读处理。
2. **数据库层**（``sys_user_book`` 的 CHECK 约束，迁移 ``0005_db_guardrails``
   经 ``db_init.sql`` 交付）：不认识的角色根本进不去。

**为什么应用层还要留着**：库约束是后加的，存量库在约束挂上之前（NOT VALID
之前）就已经有脏数据；而且任何一次约束缺席（迁移未跑、回滚、约束被手工
drop 掉做修复）都让第二层直接消失。两层都在时，攻击者要绕过写权限得同时
突破两道彼此独立的门。
"""
from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, text

from app.core.deps import WRITE_ROLES, is_read_only, normalize_role
from app.models import UserBook

CONSTRAINT = "ck_sys_user_book_role"


def _has_db_constraint(db) -> bool:
    return bool(db.execute(text("""
        SELECT 1 FROM pg_constraint
         WHERE conname = :n AND conrelid = 'sys_user_book'::regclass
    """), {"n": CONSTRAINT}).scalar())


@pytest.fixture
def 无角色约束(db):
    """把库里的角色 CHECK 摘掉，让「脏数据」能存在。

    这是为了单独验**应用层**那一道。库约束在场时非法值根本进不来，
    第二层直接生效 —— 于是第一层就成了永远跑不到、也永远不会被证伪的代码。
    而它恰恰是最需要被测的那道：库约束是后加的，存量库在它之前已经有过
    'viewer' 这种值（迁移对存量脏数据是 NOT VALID 挂的，拦不住旧行）。

    这里模拟的正是那个「约束不在场」的真实场景，而不是为了测试去改产品行为。
    """
    db.execute(text(f"ALTER TABLE sys_user_book DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))
    db.commit()
    try:
        yield
    finally:
        # 先清掉用例留下的脏行，否则把约束挂回去的那一步自己就被它挡下
        # （teardown 报错会把它变成一条 ERROR，用例到底过没过就说不清了）。
        db.execute(text(f"DELETE FROM sys_user_book WHERE role NOT IN ('OWNER','ACCOUNTANT','VIEWER')"))
        db.execute(text(f"""
            ALTER TABLE sys_user_book DROP CONSTRAINT IF EXISTS {CONSTRAINT};
            ALTER TABLE sys_user_book ADD CONSTRAINT {CONSTRAINT}
                CHECK (role IN ('OWNER', 'ACCOUNTANT', 'VIEWER'));
        """))
        db.commit()


@pytest.fixture
def linked(db, plain_user, book):
    """给普通用户在目标账套上装一个指定角色的授权。"""

    def _link(role) -> None:
        row = db.query(UserBook).filter_by(user_id=plain_user.id, book_id=book.id).one_or_none()
        if row is None:
            row = UserBook(user_id=plain_user.id, book_id=book.id, role=role)
            db.add(row)
        else:
            row.role = role
        db.commit()
        return row
    return _link


def _建科目(client, book, headers, code, name):
    # ``carry_forward`` 必须是 False：只有**损益类**科目才能被标成结转科目
    # （审计 P1-1，服务端会 422 拒掉资产科目带这个标记）。这个用例测的是
    # 「角色能不能写」，科目载荷里的那个标记与它无关。
    return client.post(f"/api/books/{book.id}/subjects", headers=headers, json={
        "code": code, "name": name, "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": False,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 71,
    })


# --------------------------------------------------------------------- 判定本身


@pytest.mark.parametrize("role,read_only", [
    ("VIEWER", True), ("viewer", True), (" Viewer ", True),
    # 原来这些**全部放行** —— 「只挡 VIEWER」的黑名单语义
    ("read_only", True), ("readonly", True), ("", True), (None, True),
    ("ADMIN", True), ("OWNER ", False), ("owner", False), ("ACCOUNTANT", False),
    ("accountant", False),
])
def test_归一化与白名单(role, read_only):
    """大小写与空白不该改变含义；但只有 OWNER/ACCOUNTANT 能写。"""
    assert is_read_only(role) is read_only


def test_归一化后的取值就是库里存的值():
    assert normalize_role(" owner ") == "OWNER"


# --------------------------------------------------------------------- 应用层


def test_小写viewer照样被拒写(client, db, plain_user, plain_auth, book, linked, 无角色约束):
    """**这条就是漏洞本身**：绕过 API 写入 'viewer' 之后，
    他必须仍然拿不到写权限。"""
    linked("viewer")
    r = _建科目(client, book, plain_auth, "2901", "只读新建")
    assert r.status_code == 403, f"小写 viewer 竟然能写：{r.status_code} {r.text}"


@pytest.mark.parametrize("role", ["read_only", "", "SOMETHING"])
def test_不认识的角色按只读处理(client, db, plain_user, plain_auth, book, linked,
                                 无角色约束, role):
    """fail-closed 的落点：配置写错的后果是「进不去」，不是「拿到全部权限」。"""
    linked(role)
    r = _建科目(client, book, plain_auth, "2902", "非法角色")
    assert r.status_code == 403, f"角色 {role!r} 竟然能写：{r.status_code}"


def test_非法角色的拒绝文案要说清是配置事故(client, db, plain_user, plain_auth, book,
                                          linked, 无角色约束):
    """和「你是只读」用同一句文案的话，管理员看到满屏只读提示会以为是自己配的。"""
    linked("read_only")
    r = _建科目(client, book, plain_auth, "2903", "非法角色")
    detail = r.json()["detail"]
    assert "read_only" in detail, detail
    assert "只读权限" not in detail, f"配置事故不该说成业务上的只读：{detail}"


def test_正常角色没有被收紧(client, db, plain_user, plain_auth, book, linked):
    """白名单不能误伤：OWNER / ACCOUNTANT 该能写的还得能写。"""
    for code, role in ((2904, "OWNER"), (2905, "ACCOUNTANT")):
        linked(role)
        r = _建科目(client, book, plain_auth, str(code), f"{role} 建的科目")
        assert r.status_code == 201, f"{role} 连建科目都不行：{r.status_code} {r.text}"


def test_只读仍然只读(client, db, plain_user, plain_auth, book, linked):
    """只读不是不能用 —— 权限模型整体崩掉的样子比多给一点权限更难查。"""
    linked("VIEWER")
    assert client.get(f"/api/books/{book.id}/vouchers", headers=plain_auth).status_code == 200


# --------------------------------------------------------------------- 数据库层


@pytest.mark.parametrize("role", ["viewer", "read_only", "", "ADMIN"])
def test_库里的白名单约束挡住非法角色(db, book, plain_user, role):
    """**P2-12 的根治项**：绕过 API 的写入路径由库这一层挡。

    没有它，上面那三条应用层用例就都只是「理论上」—— 因为值根本进不来。
    """
    if not _has_db_constraint(db):
        pytest.skip("这个库的 sys_user_book 上没有 ck_sys_user_book_role —— "
                    "它早于 0005_db_guardrails，属于没升级干净的存量库")
    with pytest.raises(Exception) as e:
        db.add(UserBook(user_id=plain_user.id, book_id=book.id, role=role))
        db.commit()
    assert "ck_sys_user_book_role" in str(e.value), str(e.value)[:200]
    db.rollback()


def test_模型声明与库里的约束是同一条():
    """ORM 与迁移分成两套名字的话，Alembic 下次 autogenerate 会同时报
    「库里有模型没有」和「模型有库里没有」—— 把一次改名伪装成两条变更。"""
    ck = [c for c in UserBook.__table__.constraints
          if isinstance(c, CheckConstraint) and c.name == CONSTRAINT]
    assert ck, f"模型上没声明 {CONSTRAINT} —— 新建库就没有这条护栏"
    sql = str(ck[0].sqltext)
    for role in ("OWNER", "ACCOUNTANT", "VIEWER"):
        assert f"'{role}'" in sql, f"约束漏了角色 {role}：{sql}"
    assert "role NOT IN" not in sql, f"必须是白名单语义：{sql}"


def test_模型声明与库里的约束表达一致(db):
    """表达式逐字对不上时，两边就是两条不同的护栏 —— 只挡得住一半的输入。

    比的是**语义**（约束哪一列、放行哪几个字面量）而不是字符串：PG 会把
    ``role IN ('A','B')`` 归一化打印成 ``role = ANY (ARRAY['A'::varchar, ...])``，
    逐字比必然误报。
    """
    if not _has_db_constraint(db):
        pytest.skip("这个库的 sys_user_book 上没有 ck_sys_user_book_role —— "
                    "它早于 0005_db_guardrails，属于没升级干净的存量库")
    db_sql = db.execute(text("""
        SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
         WHERE conname = :n AND conrelid = 'sys_user_book'::regclass
    """), {"n": CONSTRAINT}).scalar()
    ck = [c for c in UserBook.__table__.constraints if getattr(c, "name", "") == CONSTRAINT][0]
    model_sql = str(ck.sqltext)

    import re
    literals = lambda s: set(re.findall(r"'([^']+)'", s))  # noqa: E731
    assert literals(db_sql) == literals(model_sql), (
        f"模型与库放行的角色不一致：\n  库：{db_sql}\n  模型：{model_sql}")
    assert "role" in db_sql.lower(), db_sql
    assert "role" in model_sql.lower(), model_sql
    assert literals(model_sql) == {"OWNER", "ACCOUNTANT", "VIEWER"}, model_sql


def test_应用层与模型层的角色集合一致():
    """三处必须同口径。少一处就会出现「库里能写、判出来是只读」或反过来的裂缝。"""
    from app.api.system import ROLES

    ck = [c for c in UserBook.__table__.constraints if getattr(c, "name", "") == CONSTRAINT][0]
    sql = str(ck.sqltext)
    in_db = {r.strip().strip("'") for r in sql.split("IN (")[1].split(")")[0].split(",")}
    assert set(ROLES) == in_db == (WRITE_ROLES | {"VIEWER"}), (
        f"三处角色集合不一致：api={set(ROLES)} db={in_db} deps={set(WRITE_ROLES)}")
