"""汇总级父科目的「本年发生额」必须按**子树**判，不能只看它自己身上的分录。

**失效链路。** 应用层 ``api/subjects.py::update_subject`` 的
``current_year_activity`` 与库层 ``db_init.sql::fn_subject_guard`` 原来都只查
``Entry.subject_id = 该科目自己``。可是账簿引擎
``ledger/engine.py::_ancestor_map`` 把分录**上卷**到各级父科目，于是同一棵树上：

    22210101 进项税额（末级）      本年 141 条分录  → 改编码被挡住
    2221   应交税费（汇总级）      本年   0 条分录  → 放行

「儿子改不了、爹能改」，而两者在账上是同一笔钱 —— 2221 的本年发生额全部来自
22210101，用户在科目余额表、总账、报表里看到的就是这 141 条。改掉 2221 的编码，
按 2221 取数的报表公式会指向一个空科目，应交税费凭空归零，全程零报错。
这正是这道守卫存在的意义（账上已有的报告、明细账、导出都按编码引用科目）。

下面一律用 2221 → 222101 → 22210199 这条链：全表唯一的三层结构，
4-2-2 表里 1001/1002 已经是末级，撑不起「汇总级父科目」这个角色。

**为什么这样断言。** 判据必须是「这个科目**当前**管着的钱」，即自身 ∪ 全部后代。
所以父 / 子 / 孙三个深度各钉一条：只测子级的话，把实现写成「查直接下级」照样能过，
递归深度就没人管了。反过来也要钉一条「子树干净时父级仍可改编码」——
守卫一旦被改成「凡是父级都不许改编码」，就从一个 bug 变成另一个拦路虎。

**两侧都测。** 应用层 + 数据库触发器是这套护栏一贯的双保险（结账、凭证编辑、
期初不可变都是这么兜的）。只改一边等于没防住：绕过接口直接 ``UPDATE`` 就是
应用层哪天漏掉时的真实路径，见文件末尾的同源用例。

**年份一律取 ``date.today().year``。** 守卫判据是
``Period.year = EXTRACT(YEAR FROM CURRENT_DATE)``。写死 2026 的话，这些用例会
在跨年之后集体变成「本年度无发生额」，某天绿灯全过，实际什么都没钉住。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models import Entry, Subject


# --------------------------------------------------------------------- 小工具


def _current_year_period(db, book, periods):
    """当前系统年份里，这个账套的一个会计期间。

    ``book`` 夹具固定 ``start_year=2026``，跨年之后当前年份的期间还没铺上，
    所以缺的时候用应用自己的 ``ensure_periods`` 补 —— 让用例跟着系统年份走，
    而不是跟着夹具里那个写死的数字走。
    """
    from app.accounting.periods import ensure_periods

    year = dt.date.today().year
    rows = [p for p in periods.values() if p.year == year]
    if not rows:
        rows = ensure_periods(db, book, year)
        db.commit()
    assert rows, f"账套 {book.id} 没有 {year} 年的会计期间，造不出本年发生额"
    this_month = dt.date.today().month
    return next((p for p in rows if p.month == this_month), rows[0])


def _post_year_activity(db, book, periods, make_voucher, debit_subject_id,
                        amount: str = "1000.00"):
    """在**当前年份**的期间里给指定科目记一笔发生额（金额非零，零额不算发生额）。"""
    p = _current_year_period(db, book, periods)
    make_voucher(dt.date(p.year, p.month, 10),
                 dr=[(debit_subject_id, Decimal(amount), "本年发生额")],
                 cr=[("5001", Decimal(amount), "本年发生额")], posted=True)


def _direct_entries(db, subject_id) -> int:
    """这个科目**自己**身上的分录条数 —— 旧守卫查的正是这个。"""
    return db.scalar(
        select(func.count()).select_from(Entry).where(Entry.subject_id == subject_id)
    ) or 0


def _create_child(client, auth, book, parent_id, code, name) -> int:
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": code, "name": name, "category": "ASSET", "direction": "DEBIT",
        "parent_id": parent_id,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _patch(client, auth, book, subject_id, **fields):
    """部分字段 PATCH —— 走 ``exclude_unset``，发什么才改什么。"""
    return client.patch(f"/api/books/{book.id}/subjects/{subject_id}",
                        headers=auth, json=fields)


def _raw_update_code(db, subject_id, new_code):
    """绕开接口直接改编码 —— 库里只有触发器能挡它。"""
    db.execute(text("UPDATE subject SET code = :c WHERE id = :id"),
               {"c": new_code, "id": subject_id})
    db.commit()


# --------------------------------------------------------------------- 应用层


def test_父科目有上卷发生额时不能改编码(
    client, auth, db, book, subjects, periods, make_voucher
):
    """本次修复的核心断言：修复前这里是 HTTP 200。

    前置刻意写成「父科目自己身上 0 条分录」—— 旧守卫查的就是这个数字，
    它为 0 才解释得通为什么 2221 曾经能被放行。
    """
    parent = subjects["2221"]
    _post_year_activity(db, book, periods, make_voucher, subjects["22210101"].id)
    db.expire_all()
    assert _direct_entries(db, parent.id) == 0, (
        "前置：父科目自己没有直接分录，正是这个 0 让旧守卫漏判")

    r = _patch(client, auth, book, parent.id, code="2221X")
    assert r.status_code == 400, (
        f"汇总级父科目改编码被放行了：HTTP {r.status_code} {r.text} —— "
        "它的本年发生额全部来自下级，改掉编码会让按 2221 取数的报表归零")
    assert "本年度已有发生额" in r.json()["detail"], r.json()
    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221", "被拒之后编码不该变"


def test_子级有本年发生额时父科目仍然能改名(
    client, auth, db, book, subjects, periods, make_voucher
):
    """守卫只挡 code，不能把整个 PATCH 都堵死。

    报错文案自己写着「只能修改科目名称」—— 如果它也做不到了，
    用户就被逼着去改子科目编码，而下级同样是改不动的，死路。
    """
    parent = subjects["2221"]
    _post_year_activity(db, book, periods, make_voucher, subjects["22210101"].id)
    db.expire_all()
    new_name = "应交税费（本年有发生额）"

    r = _patch(client, auth, book, parent.id, name=new_name)
    assert r.status_code == 200, f"改名被守卫连坐挡住了：HTTP {r.status_code} {r.text}"
    db.expire_all()
    assert db.get(Subject, parent.id).name == new_name
    assert db.get(Subject, parent.id).code == "2221", "改名不该顺带动编码"


def test_子树没有本年发生额时父科目仍然能改编码(
    client, auth, db, book, subjects, periods
):
    """防过度拦截：守卫不能退化成「凡是父级都不许改编码」。

    没有下级发生额时 2221 的本年发生额就是 0，改编码是安全的 ——
    它和 1001 库存现金这些干净科目没有任何区别。
    """
    parent = subjects["2221"]
    assert _direct_entries(db, parent.id) == 0, "前置：父科目自己没记账"
    for child in db.scalars(
        select(Subject).where(Subject.parent_id == parent.id)
    ).all():
        assert _direct_entries(db, child.id) == 0, f"前置：下级 {child.code} 有分录"

    r = _patch(client, auth, book, parent.id, code="2221X")
    assert r.status_code == 200, f"干净的父科目被无端拦住：HTTP {r.status_code} {r.text}"
    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221X", "编码没真的改掉"


def test_孙级有本年发生额时祖父级也不能改编码(
    client, auth, db, book, subjects, periods, make_voucher
):
    """递归深度 > 1：只看「直接下级」的实现过不了这条。

    账套里本来只有 2221 → 222101 两层，孙级 22210199 是现挂的。
    顺序要紧：必须**先**挂孙科目再记账 —— 反过来的话，
    建子科目会被 _assert_no_own_balance 以「父科目已有分录」挡掉，
    测到的就不是这道守卫了。
    """
    parent = subjects["2221"]
    grandchild = _create_child(client, auth, book, subjects["222101"].id,
                               "22210199", "工行-基本户")
    _post_year_activity(db, book, periods, make_voucher, grandchild)
    db.expire_all()
    assert _direct_entries(db, parent.id) == 0, "前置：父科目自己没有直接分录"
    assert _direct_entries(db, subjects["222101"].id) == 0, "前置：中间层也没有直接分录"

    r = _patch(client, auth, book, parent.id, code="2221X")
    assert r.status_code == 400, (
        f"隔一层的发生额没被算进来：HTTP {r.status_code} {r.text} —— "
        "判据必须是整棵子树，不是直接下级")
    assert "本年度已有发生额" in r.json()["detail"], r.json()


# --------------------------------------------------------------------- 库层触发器


def test_裸UPDATE时触发器同样拦住有发生额的父科目(
    db, book, subjects, periods, make_voucher
):
    """绕开接口，触发器必须自己挡得住 —— 应用层哪天漏了就是这条路径。

    报错码也要钉：``check_violation``（SQLSTATE 23514）是这套守卫统一的
    fail-closed 口径，裸 PG 异常会被接口兜底成 500，分类错了排查会跑偏。
    """
    parent = subjects["2221"]
    _post_year_activity(db, book, periods, make_voucher, subjects["22210101"].id)
    db.expire_all()

    with pytest.raises(DBAPIError) as ei:
        _raw_update_code(db, parent.id, "2221X")
    err = ei.value.orig
    assert getattr(err, "sqlstate", None) == "23514", (
        f"期望 check_violation(23514)，实际 {getattr(err, 'sqlstate', None)}：{err}")
    assert "本年度已有发生额" in str(err), str(err)
    db.rollback()
    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221", "触发器抛错后编码不该变"


def test_裸UPDATE时触发器同样认孙级发生额(
    db, book, subjects, periods, make_voucher
):
    """库层的递归也必须到孙级 —— 两层各写各的，一层写浅了就漏。"""
    parent = subjects["2221"]
    grandchild = _make_child_directly(db, book, subjects["222101"].id,
                                      code="22210199", name="工行-基本户")
    _post_year_activity(db, book, periods, make_voucher, grandchild)
    db.expire_all()

    with pytest.raises(DBAPIError, match="本年度已有发生额"):
        _raw_update_code(db, parent.id, "2221X")
    db.rollback()
    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221"


def test_裸UPDATE在子树干净时允许改编码(
    db, book, subjects, periods
):
    """库层也不能过度拦截，否则所有报表导出脚本都会开始报错。"""
    parent = subjects["2221"]

    _raw_update_code(db, parent.id, "2221X")   # 不抛异常即为通过
    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221X"


def _make_child_directly(db, book, parent_id, code, name) -> int:
    """绕开接口直接挂孙科目（触发器用例里不需要走 HTTP 建科目这条线）。

    用 ORM 而不是接口，是为了让这条用例只测 ``fn_subject_guard`` 一件事：
    经接口建科目还会牵出 ``_assert_no_own_balance``、``is_leaf`` 派生等别的守卫。
    """
    s = Subject(
        # 年度必填：科目表每个年度各有一份（迁移 0032），
        # 父节点也在同一张表上（库层护栏 fn_subject_parent_year_guard）
        book_id=book.id, year=book.start_year,
        code=code, name=name, category="ASSET", direction="DEBIT",
        parent_id=parent_id, level=3, is_leaf=True, carry_forward=False,
        qty_calc=False, aux_calc=False, fx_calc=False, sort_no=0,
        active=True, ext_attrs={},
    )
    db.add(s)
    db.flush()
    parent = db.get(Subject, parent_id)
    parent.is_leaf = False
    db.commit()
    db.refresh(s)
    return s.id


# --------------------------------------------------------------------- 两层同源


def test_两层同源_接口挡住的场景裸UPDATE也挡得住(
    client, auth, db, book, subjects, periods, make_voucher
):
    """钉住「同口径」：接口 400 的每一种情形，触发器都必须自己拦得住。

    两层各写一份 SQL/Python 的判据，天长日久一定会漂 —— 一边改成子树、
    另一边还停在直接分录，或者反过来。应用层不会报错、库里也拦不住，
    于是又回到「儿子改不了爹能改」，而且**没有任何症状**。
    这条用例把两层放在同一个场景上跑，一次性比较两侧的判断。
    """
    parent = subjects["2221"]
    grandchild = _make_child_directly(db, book, subjects["222101"].id,
                                      code="22210199", name="工行-基本户")
    _post_year_activity(db, book, periods, make_voucher, grandchild)
    db.expire_all()

    r = _patch(client, auth, book, parent.id, code="2221X")
    assert r.status_code == 400, f"应用层没挡住：HTTP {r.status_code} {r.text}"

    with pytest.raises(DBAPIError, match="本年度已有发生额"):
        _raw_update_code(db, parent.id, "2221X")
    db.rollback()
    db.expire_all()

    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221", "两层都拦住了，编码却还是变了"


def test_两层同源_接口放行的场景裸UPDATE也放行(
    client, auth, db, book, subjects, periods
):
    """同口径的另一半：触发器不能比应用层更严。

    只测「都挡住」的话，触发器哪天被写成「凡是父级都不许改编码」也照样绿，
    而那会让所有按编码更新科目的脚本（期初导入、报表重导）集体失败。
    """
    parent = subjects["2221"]

    r = _patch(client, auth, book, parent.id, code="2221X")
    assert r.status_code == 200, f"应用层误拦：HTTP {r.status_code} {r.text}"

    db.expire_all()
    assert db.get(Subject, parent.id).code == "2221X", "接口返回 200 却没真的改掉"