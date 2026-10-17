"""「以某张报表为基础新建」必须复制**本账套副本**，不是官方基线。

``create_template`` 按 ``base_code`` 找底稿，两行注释写着「本账套副本优先于同
code 的官方基线」，可排序写的是 ``.order_by(ReportTemplate.book_id.desc())``：

* PostgreSQL 的 ``DESC`` 默认 **NULLS FIRST**；
* 官方内置基线恰恰是 ``book_id IS NULL``（迁移 0020 把模板按账套隔离之后，
  出厂基线是唯一没有归属的那一份）。

两件事叠在一起，取到的就是**出厂基线** —— 与注释里声明的意图正好相反。
本项目实际 PostgreSQL 上实测过。

错在哪、用户看到的是什么：账套 fork 过利润表、改过行和公式（这正是设计模式
的全部意义），回到列表点「以利润表为基础新建」，得到的是**原版**。用户不会
怀疑底稿取错了 —— 新表渲染得好好的，只是他改过的那张表对新建毫无影响，
而公式里的科目编码往往还是老口径。要过好几天、金额对不上时才会回头查。

同一个口径在**列表去重**里本来就是对的（``templates`` 用 ``order_by(id)`` +
「副本后入覆盖基线」），所以这里也不该另立一套隐含规则：可见范围一样
（基线 + 本账套），优先级也一样（本账套副本优先）。

这个文件钉两件事：

* fork/改过之后新建，骨架逐格等于**本账套副本**，且**不等于**基线
  （只断前者会在「两边一样」时空过，所以两边都断）；
* 账套没 fork 过时，新建仍然复制基线 —— 闸不能反过来把基线也排除掉。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import ReportTemplate

BASE_CODE = "INCOME_SHEET"


@pytest.fixture
def baseline_id(db):
    """官方内置利润表（``book_id IS NULL``）。

    显式按 ``book_id IS NULL`` 取：某个账套 fork 之后库里会有同 code 的副本，
    只按 code 取可能拿到副本，测的就不是基线了。
    """
    tid = db.scalar(select(ReportTemplate.id).where(
        ReportTemplate.code == BASE_CODE, ReportTemplate.book_id.is_(None)))
    assert tid, "内置利润表模板没建出来，种子数据有问题"
    return tid


def _design(client, auth, template_id, book_id):
    r = client.get(f"/api/reports/{template_id}/design?book_id={book_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cells_by_addr(payload):
    """把 design 的 cells 摊成「地址 → 公式」，顺序信息直接抹掉。

    列表顺序是接口的实现细节（design 里有专门一段在排它），直接整包 == 比会
    假红；这里要断的是「逐格相等」，不是「排列相同」。
    """
    return {(c["row_no"], c["col_no"]): c["formula"] for c in payload["cells"]}


def _fork_and_mark(client, auth, baseline_id, book_id):
    """fork 一份副本并**把它改成一眼可辨的样子**。

    只 fork 不改的话，副本与基线逐格相同，「取到哪一份」根本测不出来 ——
    这正是这个缺陷能活到今天的原因：功能测试全绿，只有真正改过模板的
    账套才会踩到。
    """
    forked = client.post(f"/api/reports/templates/{baseline_id}/fork?book_id={book_id}",
                         headers=auth)
    assert forked.status_code == 201, forked.text
    copy_id = forked.json()["id"]
    assert copy_id != baseline_id

    # 改行标签：副本有一个基线没有的行，形状上就分得开。
    r = client.post(f"/api/reports/templates/{copy_id}/rows?book_id={book_id}",
                    headers=auth, json={"row_no": 1, "label": "副本专属行"})
    assert r.status_code in (200, 201), r.text
    # 改一格公式：内容上再分一道。取不到基线有的格子就跳过（有就断，没有
    # 上面那道形状断言仍然成立）。
    base_cells = _cells_by_addr(_design(client, auth, baseline_id, book_id))
    if base_cells:
        addr = next(iter(sorted(base_cells)))
        r = client.put(f"/api/reports/{copy_id}/cells?book_id={book_id}", headers=auth,
                       json=[{"row_no": addr[0], "col_no": addr[1], "formula": "1+1"}])
        assert r.status_code == 200, r.text
    return copy_id


def test_新建复制的是本账套的副本而不是官方基线(client, auth, book, baseline_id):
    """核心断言：fork 出来的副本行标签/公式与基线不同，新建的必须跟着副本走。

    先断「新建的骨架等于本账套副本」，再断「不等于基线」：只断前者的话，
    两边恰好相同时（还没改过副本的账套）这条用例会绿，而缺陷照样在。
    """
    copy_id = _fork_and_mark(client, auth, baseline_id, book.id)
    copy_before = _design(client, auth, copy_id, book.id)
    baseline = _design(client, auth, baseline_id, book.id)
    assert copy_before != baseline, "副本与基线长得一样，后面两条断言就没有分辨力了"

    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "新建的利润表", "base_code": BASE_CODE})
    assert r.status_code == 201, r.text
    created = _design(client, auth, r.json()["id"], book.id)

    assert created["rows"] == copy_before["rows"], (
        f"行不是复制自本账套副本 {copy_id}，而是别处："
        f"{[x['label'] for x in created['rows']][:3]}")
    assert _cells_by_addr(created) == _cells_by_addr(copy_before), (
        "单元格公式不是复制自本账套副本")
    # 关键的反面：不能是基线。
    assert created["rows"] != baseline["rows"], "新建的表复制自官方基线（副本优先被反了）"
    assert _cells_by_addr(created) != _cells_by_addr(baseline), (
        "新建的表复制自官方基线（副本优先被反了）")


def test_基线是NULL所以裸DESC确实会取到基线(client, auth, db, book, baseline_id):
    """把「为什么必须显式写排序」钉成一条会失败的断言，而不是一句注释。

    缺陷成立需要两个事实同时为真，缺一个就只是一段可疑的代码：

    1. 官方基线的 ``book_id`` 是 NULL（迁移 0020 之后，出厂基线是唯一没有
       归属的那一份）；
    2. PostgreSQL 的 ``DESC`` 把 NULL 排在最前（SQL 标准允许 NULLS FIRST 与
       LAST 两种，PG 选 FIRST）。

    两条都成立时，``.order_by(book_id.desc())`` 必然先给出基线。这条用例把
    裸 DESC 的结果断言成「基线在前」—— 哪天有人把排序简化回去、或换数据库
    让 NULLS 的默认位置变了，这里会先红，注释里那句话就不再是空话。
    """
    copy_id = _fork_and_mark(client, auth, baseline_id, book.id)
    db.expire_all()

    naive = db.scalars(
        select(ReportTemplate).where(
            ReportTemplate.code == BASE_CODE,
            (ReportTemplate.book_id.is_(None)) | (ReportTemplate.book_id == book.id))
        .order_by(ReportTemplate.book_id.desc())
    ).all()
    assert db.get(ReportTemplate, baseline_id).book_id is None, "基线不再是 NULL 归属"
    assert naive[0].id == baseline_id, (
        "裸 DESC 不再把基线顶到最前 —— NULL 的排序位置变了，"
        "create_template 里的排序理由要重新评估")
    assert naive[-1].id == copy_id

    # 正确的口径：本账套副本在前、基线在后（与列表去重「副本覆盖基线」一致）。
    fixed = db.scalars(
        select(ReportTemplate).where(
            ReportTemplate.code == BASE_CODE,
            (ReportTemplate.book_id.is_(None)) | (ReportTemplate.book_id == book.id))
        .order_by(ReportTemplate.book_id.is_(None), ReportTemplate.book_id.desc())
    ).all()
    assert [t.id for t in fixed] == [copy_id, baseline_id], [t.id for t in fixed]


def test_没fork过的账套仍然复制基线(client, auth, db, book, baseline_id):
    """闸不能反过来把基线也排除掉 —— 基线是新账套唯一的起手式。

    这里也是「NULLS LAST 而不是只要非空」的理由：可见范围里没有本账套副本时，
    基线必须还能被选到（否则「以利润表为基础新建」在没 fork 过的账套里直接
    建出一张空表，而空表不报错，只是渲染出来一片 0）。
    """
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "起手式", "base_code": BASE_CODE})
    assert r.status_code == 201, r.text
    created = _design(client, auth, r.json()["id"], book.id)
    baseline = _design(client, auth, baseline_id, book.id)

    assert created["rows"] == baseline["rows"], "没 fork 时复制到的不是基线行"
    assert _cells_by_addr(created) == _cells_by_addr(baseline), "没 fork 时复制到的不是基线公式"
    assert any(c["formula"] for c in created["cells"]), "复制过来的是一张空表"


def test_别的账套的副本不会被当成本账套的底稿(client, auth, db, book, baseline_id):
    """可见范围外的副本连候选都不该进 —— 取的是「本账套那份」，不是「最新的那份」。

    隔离在这里体现为：别家 fork 过之后，本账套新建仍然复制自己的（没有就复制
    基线），不会因为别家的副本 id 更大就跟着走。
    """
    tag = uuid.uuid4().hex[:8]
    other = client.post("/api/books", headers=auth,
                        json={"company_name": f"别家{tag}", "name": f"别家{tag}",
                              "start_year": 2026})
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    _fork_and_mark(client, auth, baseline_id, other_id)

    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "本账套的表", "base_code": BASE_CODE})
    assert r.status_code == 201, r.text
    created = _design(client, auth, r.json()["id"], book.id)
    baseline = _design(client, auth, baseline_id, book.id)
    assert _cells_by_addr(created) == _cells_by_addr(baseline), (
        "复制到了别家账套的副本")
