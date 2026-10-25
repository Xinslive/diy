"""「必须是 OWNER」不能有两套实现（审计 P3-12，报告 §3.12）。

同一句判定，在本仓有**两种写法**：

* ``app/api/books.py``（:399、:432）、``app/core/deps.py``（:176）用
  ``normalize_role(link.role)`` —— 大小写与首尾空白都吃掉；
* ``app/api/reports.py``（:131）、``app/api/printing.py``（:622）用
  **原始字面量** ``link.role != "OWNER"`` —— 大小写敏感。

而 ``app/models/system.py`` 早就写明「大小写归一化交给应用层
（``deps.normalize_role``）做」，库级 CHECK 只是拦下**新写入**的脏值
（``ck_sys_user_book_role`` 是 validated 的）。

于是**存量库里**那些大小写不规整的角色（``'owner'`` / ``'OWNER '`` /
``'OWNER\\n'``）在 books.py 放行、在 reports/printing 被拒：

    role repr      deps.require_write   reports/printing OWNER   books.py OWNER
    'OWNER'        ALLOW                 ALLOW                     ALLOW
    'owner'        ALLOW                 DENY                      ALLOW
    'OWNER '       ALLOW                 DENY                      ALLOW
    'OWNER\\n'      ALLOW                 DENY                      ALLOW

方向是 **fail-closed**（是「合法用户被误拒」，不是越权），但口径分裂本身
就是 bug —— 一个人换个入口就改了权限。

所以这里钉的是「**三边对同一份数据给出同一个答案**」。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api.printing import _require_global_template_write
from app.api.reports import _require_template_write
from app.core.deps import normalize_role
from app.models import UserBook

#: 各种「大小写/空白不规范」的 OWNER 写法。存量库里可能有这些值。
_OWNER_SPELLINGS = ["OWNER", "owner", "Owner", "OWNER ", "OWNER\n", " owner "]

#: 明确不是 OWNER 的角色 —— 这几个必须被拒（fail-closed 不许松）。
#: ``None`` 不在这里：``sys_user_book.role`` 是 NOT NULL，压根存不进去。
_NOT_OWNER = ["VIEWER", "ACCOUNTANT", "viewer", ""]

_CONSTRAINT = "ck_sys_user_book_role"
_CONSTRAINT_DDL = (
    "ALTER TABLE sys_user_book ADD CONSTRAINT ck_sys_user_book_role "
    "CHECK (role IN ('OWNER', 'ACCOUNTANT', 'VIEWER'))")


@contextmanager
def _role_row(db, user_id, book_id, role):
    """临时造一条**指定角色**的授权行。

    **为什么合法角色也要临时插。** 断言要的是「同一个人的同一个账套、
    换个角色会怎样」，所以每次都要从**干净状态**开始：先删掉上一轮留下
    的行，再插这一轮要的行。

    **为什么不合规的角色必须动 DDL。** ``ck_sys_user_book_role`` 是
    validated 的 CHECK 约束：``role='owner'`` 会被它挡下（sqlstate
    23514）。而这正是这条缺陷的形状 —— **新写入路径碰不到这个状态**，
    想复现「存量库里有这种行」，只能先摘掉约束、插进去、再装回去。

    约束在 ``finally`` 里装回去：这条用例无论成败，库都必须回到
    「有约束」的状态 —— 后面那些用例还要靠它拦住脏数据。
    """
    standard = role in ("OWNER", "ACCOUNTANT", "VIEWER")
    if not standard:
        db.execute(text(f"ALTER TABLE sys_user_book DROP CONSTRAINT {_CONSTRAINT}"))
        db.commit()
    try:
        link = UserBook(user_id=user_id, book_id=book_id, role=role)
        db.add(link)
        db.commit()
        yield link
    finally:
        db.rollback()
        db.execute(text(
            "DELETE FROM sys_user_book WHERE user_id = :u AND book_id = :b AND role = :r"),
            {"u": user_id, "b": book_id, "r": role})
        db.commit()
        if not standard:
            db.execute(text(_CONSTRAINT_DDL))
            db.commit()


# --------------------------------------------------------------------- 单元层


@pytest.mark.parametrize("role", _OWNER_SPELLINGS)
def test_报表侧的门禁认得各种OWNER写法(db, book, plain_user, role):
    """``app/api/reports.py`` 的 OWNER 判定。

    用的必须是**非管理员**：两处门禁开头都有 ``if user.is_admin: return`` ——
    拿管理员去测，测的就只是那条捷径。
    """
    with _role_row(db, plain_user.id, book.id, role):
        _require_template_write(db, plain_user, book.id)


@pytest.mark.parametrize("role", _NOT_OWNER)
def test_报表侧的门禁仍然拒绝非OWNER(db, book, plain_user, role):
    with _role_row(db, plain_user.id, book.id, role):
        with pytest.raises(HTTPException) as exc:
            _require_template_write(db, plain_user, book.id)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("role", _OWNER_SPELLINGS)
def test_打印侧的门禁认得各种OWNER写法(db, book, plain_user, role):
    """``app/api/printing.py`` 的 OWNER 判定。"""
    with _role_row(db, plain_user.id, book.id, role) as link:
        _require_global_template_write(link, plain_user)


@pytest.mark.parametrize("role", _NOT_OWNER)
def test_打印侧的门禁仍然拒绝非OWNER(db, book, plain_user, role):
    with _role_row(db, plain_user.id, book.id, role) as link:
        with pytest.raises(HTTPException) as exc:
            _require_global_template_write(link, plain_user)
        assert exc.value.status_code == 403


def test_三边的判定口径一致(db, book, plain_user):
    """**一条就够**：同一份角色，三边必须给同一个答案。

    这条是这条缺陷的核心断言 —— 以前 reports/printing 与 books 会对
    ``'owner'`` 给出相反的答案。
    """
    for role in _OWNER_SPELLINGS + _NOT_OWNER:
        with _role_row(db, plain_user.id, book.id, role) as link:

            def verdict(fn):
                try:
                    fn()
                    return "ALLOW"
                except HTTPException as exc:
                    return f"DENY:{exc.status_code}"

            reports = verdict(lambda: _require_template_write(db, plain_user, book.id))
            printing = verdict(lambda: _require_global_template_write(link, plain_user))
            # books.py 的口径 = ``normalize_role(role) != "OWNER"``（它就是这么写的）
            books = ("ALLOW" if normalize_role(role) == "OWNER" else "DENY:403")

            assert reports == printing == books, (
                f"角色 {role!r} 三边判定不一致：reports={reports} "
                f"printing={printing} books={books}")


def test_归一化之后与原始字面量不再各说各话():
    """``normalize_role`` 必须吃掉大小写与首尾空白。"""
    for role in _OWNER_SPELLINGS:
        assert normalize_role(role) == "OWNER", f"{role!r} 归一化后不是 OWNER"
    # 认不出来的角色一律按「不是 OWNER」处理（fail-closed）
    assert normalize_role(None) == ""
    assert normalize_role("") == ""
    assert normalize_role("VIEWER") == "VIEWER"


# --------------------------------------------------------------------- 端到端


@pytest.mark.parametrize("role", ["owner", "OWNER "])
def test_存量的大小写不规范角色在报表侧不再被误拒(client, db, book, plain_user,
                                                 plain_auth, role):
    """端到端：报表建模板（要求 OWNER）不再因为大小写被拒。"""
    with _role_row(db, plain_user.id, book.id, role):
        r = client.post(f"/api/reports/templates?book_id={book.id}", headers=plain_auth,
                        json={"name": "存量角色建表", "base_code": "BALANCE_SHEET"})
        assert r.status_code == 201, (
            f"拿到 {r.status_code}：角色 {role!r} 被误拒了（合法用户被当成了非 OWNER）。"
            f"响应体 {r.text[:300]}")


@pytest.mark.parametrize("role", ["owner", "OWNER "])
def test_存量的大小写不规范角色在打印侧不再被误拒(client, db, book, plain_user,
                                                 plain_auth, role):
    """端到端：新建**账套级**打印模板（要求 OWNER）不再因为大小写被拒。"""
    with _role_row(db, plain_user.id, book.id, role):
        r = client.post(f"/api/books/{book.id}/print/templates", headers=plain_auth,
                        json={"scope": "VOUCHER", "name": "存量角色建模板", "content": {}})
        assert r.status_code == 201, (
            f"拿到 {r.status_code}：角色 {role!r} 被误拒了。"
            f"响应体 {r.text[:300]}")


@pytest.mark.parametrize("role", ["owner", "OWNER "])
def test_存量的大小写不规范角色在账套侧照常能改名(client, db, book, plain_user,
                                                 plain_auth, role):
    """端到端：改账套名（要求 OWNER）与 books.py 的口径一致。"""
    with _role_row(db, plain_user.id, book.id, role):
        r = client.put(f"/api/books/{book.id}", headers=plain_auth,
                       json={"name": f"改名成功{role.strip()}"})
        assert r.status_code == 200, (
            f"拿到 {r.status_code}：角色 {role!r} 被误拒了。"
            f"响应体 {r.text[:300]}")


def test_只读角色在报表侧仍然被拒(client, db, book, plain_user, plain_auth):
    """**归一化不许变成放行。** VIEWER 还是 VIEWER。"""
    with _role_row(db, plain_user.id, book.id, "VIEWER"):
        r = client.post(f"/api/reports/templates?book_id={book.id}", headers=plain_auth,
                        json={"name": "只读角色建表", "base_code": "BALANCE_SHEET"})
        assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:300]}"


def test_会计角色在报表侧仍然被拒(client, db, book, plain_user, plain_auth):
    """报表口径只认 OWNER —— 会计也不行（与打印侧同一个口径）。"""
    with _role_row(db, plain_user.id, book.id, "ACCOUNTANT"):
        r = client.post(f"/api/reports/templates?book_id={book.id}", headers=plain_auth,
                        json={"name": "会计角色建表", "base_code": "BALANCE_SHEET"})
        assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:300]}"


def test_约束装回去了(db):
    """**自检**：上面几条用例动过 DDL，用完必须把约束装回去。"""
    rows = db.execute(text(
        "SELECT conname FROM pg_constraint "
        f"WHERE conname = '{_CONSTRAINT}' AND contype = 'c'")).all()
    assert rows, f"{_CONSTRAINT} 被测试用例摘掉之后没装回去"
