"""is_leaf 必须是服务端派生的（审计 P1-01），移动子树要重算级次（P3-01）。

**P1-01 的漏洞形状：把有下级的科目标成末级，就能往汇总科目上记账。**

``SubjectIn.is_leaf`` 过去是客户端可提交字段，而更新路由的字段循环里
无条件执行 ``s.is_leaf = payload.is_leaf``。于是普通账套写入者提交一次 PATCH，
就能把「1002 银行存款」这个有下级的汇总科目标成末级；此后

  * 凭证服务端校验（``accounting/vouchers._validate_entries``）放行，
  * 期初写入（``api/books.set_openings``）也放行，

分录真的挂到了汇总科目上。而明细账、科目余额表都按末级出账，
这笔钱在账上凭空消失。前端不给选不算防线 —— 接口能被直接调。

现在 is_leaf 只由服务端从「有没有下级」算，客户端说了不算，
数据库层还有一道触发器兜底。

**P3-01 是同一处代码的另一半：** 改父时只把被移动的那一个科目的 level
改成「新父级次 + 1」，它下面的下级和更深的后代一律留在旧级次上。
测试里原有的改父用例搬的都是单个叶子科目，正好绕开了「搬整棵子树」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.models import Period, Subject


def _payload(**kw):
    base = {"code": "1999", "name": "测试科目", "category": "ASSET", "direction": "DEBIT"}
    base.update(kw)
    return base


def _mk(client, auth, book, code, name, **kw) -> int:
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(code=code, name=name, **kw))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _patch(client, auth, db, book, sid, **kw):
    """改一个科目。除 kw 里显式给的名字外，其余字段一律沿用库里的现值。"""
    s = db.get(Subject, sid)
    assert s is not None and s.book_id == book.id
    return client.patch(
        f"/api/books/{book.id}/subjects/{sid}", headers=auth,
        json=_payload(code=s.code, name=kw.pop("name", s.name),
                      category=s.category, direction=s.direction, **kw),
    )


def _level(db, book_id, sid) -> int:
    s = db.get(Subject, sid)
    assert s is not None and s.book_id == book_id
    return s.level


def _is_leaf(db, book_id, sid) -> bool:
    s = db.get(Subject, sid)
    assert s is not None and s.book_id == book_id
    return s.is_leaf


def _find(nodes, code):
    for n in nodes:
        if n["code"] == code:
            return n
        hit = _find(n["children"], code)
        if hit:
            return hit
    return None


# --------------------------------------------------------------------- P1-01


def test_有下级的科目不许被客户端标成末级(client, auth, db, book):
    """审计的原始场景：PATCH is_leaf=true，HTTP 200，之后分录就挂得上汇总级。"""
    parent = _mk(client, auth, book, "1900", "被伪装的汇总科目")
    _mk(client, auth, book, "1905", "它的明细", parent_id=parent)
    db.expire_all()
    assert _is_leaf(db, book.id, parent) is False, "前置：父科目确实有下级"

    r = _patch(client, auth, db, book, parent, is_leaf=True)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _is_leaf(db, book.id, parent) is False, (
        "汇总科目被客户端标成了末级 —— 凭证与期初两道关口都会放行")


def test_伪装成末级之后凭证接口仍然拒绝(client, auth, db, book, subjects, make_voucher):
    """不只是标志本身：真正要挡住的是「分录挂到汇总科目上」。

    借方必须挂在一条真明细上：凭证校验先判借贷平衡，再判科目，
    只挂一边的话报出来的是「借贷不平衡」，那道断言就名不副实了。
    """
    parent = _mk(client, auth, book, "1900", "汇总科目")
    _mk(client, auth, book, "1905", "明细", parent_id=parent)
    _patch(client, auth, db, book, parent, is_leaf=True)
    db.expire_all()

    with pytest.raises(ValueError, match="不是末级科目"):
        make_voucher("2026-01-05",
                     dr=[(subjects["1002"].id, Decimal("100.00"), "真明细")],
                     cr=[(parent, Decimal("100.00"), "往汇总级记账")])


def test_伪装成末级之后期初接口仍然拒绝(client, auth, db, book, subjects):
    parent = _mk(client, auth, book, "1900", "汇总科目")
    _mk(client, auth, book, "1905", "明细", parent_id=parent)
    _patch(client, auth, db, book, parent, is_leaf=True)
    db.expire_all()

    p1 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 1))
    r = client.post(f"/api/books/{book.id}/periods?period_id={p1.id}", headers=auth, json=[
        {"subject_id": parent, "amount": "100"},
        {"subject_id": subjects["4001"].id, "amount": "-100"},
    ])
    assert r.status_code == 400, f"期初录到了汇总科目上：{r.status_code} {r.text}"
    assert "明细" in r.json()["detail"]


def test_客户端不能把叶子科目标成汇总级(client, auth, db, book):
    """另一个方向同样由服务端说了算：不能让一个没有下级的科目变成
    「不能记账的汇总级」—— 那等于让它从凭证面板里凭空消失。"""
    sid = _mk(client, auth, book, "1902", "普通明细")
    r = _patch(client, auth, db, book, sid, is_leaf=False)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _is_leaf(db, book.id, sid) is True


def test_新建时提交的is_leaf也不作数(client, auth, db, book):
    """前端新增表单固定传 is_leaf=true，服务端本来就该无视它；
    传 false 也不能让一个刚建出来的科目变成汇总级。"""
    sid = _mk(client, auth, book, "1903", "刚建出来的", is_leaf=False)
    db.expire_all()
    assert _is_leaf(db, book.id, sid) is True


def test_数据库层也拦一道(db, book):
    """绕过应用层直接 UPDATE，触发器必须拒绝这个自相矛盾的状态。

    护栏的意义就在「应用层哪天漏了」—— 这套系统的结账、凭证编辑、
    期初不可变都是靠触发器兜底的，同一思路。
    """
    parent = Subject(book_id=book.id, year=book.start_year, code="1904",
                     name="库里的汇总科目",
                     category="ASSET", direction="DEBIT", level=1,
                     is_leaf=False, carry_forward=False, qty_calc=False,
                     aux_calc=False, fx_calc=False, sort_no=0, active=True,
                     ext_attrs={})
    db.add(parent)
    db.flush()
    db.add(Subject(book_id=book.id, year=book.start_year, code="190401",
                   name="它的明细",
                   category="ASSET", direction="DEBIT", parent_id=parent.id,
                   level=2, is_leaf=True, carry_forward=False, qty_calc=False,
                   aux_calc=False, fx_calc=False, sort_no=0, active=True,
                   ext_attrs={}))
    db.commit()

    with pytest.raises(DBAPIError):
        db.get(Subject, parent.id).is_leaf = True
        db.commit()
    db.rollback()
    db.expire_all()
    assert _is_leaf(db, book.id, parent.id) is False


def test_删除最后一个子节点后父节点恢复末级(client, auth, db, book):
    """删掉之后父节点必须能重新记进账 —— 否则它会永远停在「不能选」的状态。"""
    parent = _mk(client, auth, book, "1900", "容器")
    child = _mk(client, auth, book, "1905", "唯一明细", parent_id=parent)
    db.expire_all()
    assert _is_leaf(db, book.id, parent) is False

    r = client.delete(f"/api/books/{book.id}/subjects/{child}", headers=auth)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _is_leaf(db, book.id, parent) is True, "下级都删光了，父节点仍是汇总级"


def test_删掉根科目的最后一个下级后根恢复末级(client, auth, db, book):
    """树根没有 id 可以更新，只能整体重算 —— 这是最容易被漏掉的一种。"""
    root = _mk(client, auth, book, "1900", "顶级科目")
    child = _mk(client, auth, book, "1905", "它的明细", parent_id=root)
    db.expire_all()
    assert _is_leaf(db, book.id, root) is False

    r = client.delete(f"/api/books/{book.id}/subjects/{child}", headers=auth)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _is_leaf(db, book.id, root) is True, "根节点没有随下级一起恢复末级"


# --------------------------------------------------------------------- P3-01


def test_移动整棵子树要重算所有后代的级次(client, auth, db, book):
    """原来只改被移动的那个节点，后代留在旧级次上 —— 树画出来是错的。

    这是审计点名的那条：科目列表、账簿行、明细账都返回持久化的 level，
    前端据此缩进，所以错的不只是显示。
    """
    deep_parent = _mk(client, auth, book, "1900", "一级容器")
    mid = _mk(client, auth, book, "1905", "二级容器", parent_id=deep_parent)
    leaf = _mk(client, auth, book, "1902", "三级明细", parent_id=mid)
    db.expire_all()
    assert [_level(db, book.id, sid)
            for sid in (deep_parent, mid, leaf)] == [1, 2, 3]

    # 搬到第二层：整棵子树的所有级次都要 +1
    new_root = _mk(client, auth, book, "1903", "另一个一级科目")
    r = _patch(client, auth, db, book, deep_parent, parent_id=new_root)
    assert r.status_code == 200, r.text
    db.expire_all()

    got = [_level(db, book.id, sid) for sid in (deep_parent, mid, leaf)]
    assert got == [2, 3, 4], f"后代级次没跟着重算：{got}"


def test_往上移回根层后代级次也要回落(client, auth, db, book):
    """反方向同样要算：只看「变深了」会漏掉「变浅了」。"""
    mid = _mk(client, auth, book, "1905", "二级容器")
    leaf = _mk(client, auth, book, "1902", "三级明细", parent_id=mid)
    db.expire_all()
    assert [_level(db, book.id, sid) for sid in (mid, leaf)] == [1, 2]

    r = _patch(client, auth, db, book, mid, parent_id=None)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _level(db, book.id, mid) == 1
    assert _level(db, book.id, leaf) == 2, "子树移到根层后后代还留在旧级次"


def test_移动后is_leaf与实际子级关系一致(client, auth, db, book):
    """level 修了，is_leaf 不能同时被弄坏 —— 两者是同一次派生。"""
    parent = _mk(client, auth, book, "1900", "容器")
    child = _mk(client, auth, book, "1905", "明细", parent_id=parent)
    other = _mk(client, auth, book, "1902", "另一个容器")
    db.expire_all()

    r = _patch(client, auth, db, book, child, parent_id=other)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _is_leaf(db, book.id, parent) is True, "下级移走了，它却还是汇总级"
    assert _is_leaf(db, book.id, other) is False
    assert _level(db, book.id, child) == 2


def test_移动后科目树返回的级次正确(client, auth, db, book):
    """从接口返回的树上核对，而不是只看库里的列 ——
    前端缩进、明细账行、导出读的都是这份 level。"""
    a = _mk(client, auth, book, "1900", "A")
    b = _mk(client, auth, book, "1905", "B", parent_id=a)
    _mk(client, auth, book, "1902", "C", parent_id=b)
    deep = _mk(client, auth, book, "1903", "D")
    db.expire_all()

    r = _patch(client, auth, db, book, a, parent_id=deep)
    assert r.status_code == 200, r.text
    db.expire_all()

    tree = client.get(f"/api/books/{book.id}/subjects", headers=auth).json()
    d = _find(tree, "1903")
    a_node = _find(d["children"], "1900")
    b_node = _find(a_node["children"], "1905")
    assert (d["level"], a_node["level"], b_node["level"],
            b_node["children"][0]["level"]) == (1, 2, 3, 4)
