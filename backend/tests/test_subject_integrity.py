"""科目删除与改父的完整性（审计 P1-05 + P1-06）。

P1-05：PeriodOpening.subject_id 上是 ON DELETE CASCADE，而删科目前只查了
       Entry 引用和下级科目，**没查期初**。于是「只被期初引用、没有凭证
       引用」的科目一删，期初记录随之消失，余额凭空漂移。
       审计实测：2 条期初删成 1 条，余额 0 → -100，无报错无审计线索。

P1-06：新建科目时校验了父科目归属，更新路径**没有** ——
       拿着另一个账套的科目 id 改父，HTTP 200，该科目随即从本账套的
       科目树里消失，变成 UI 上不可见、库里还在、还被分录引用的孤儿节点。
       同时没有环路检测（父节点不能是自身或后代），没有维护父节点 is_leaf。

年度结转自动生成的期初**大量处于「有余额、未记账」这个状态**，
所以 P1-05 不是边角情况。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (Book, Entry, Period, PeriodOpening, Subject, UserBook,
                        Voucher)


@pytest.fixture
def other_book(db, admin_user):
    """第二个账套，供跨账套挂接场景使用。"""
    import uuid

    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    tag = uuid.uuid4().hex[:8]
    b = Book(company_name=f"别家公司{tag}", name=f"别家账套{tag}", start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


def _payload(**kw):
    base = {"code": "1999", "name": "测试科目", "category": "ASSET", "direction": "DEBIT"}
    base.update(kw)
    return base


def _seed_opening(db, book, subject, amount="100.00"):
    """给某科目造一条期初记录。"""
    first = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 1))
    o = PeriodOpening(book_id=book.id, period_id=first.id,
                      subject_id=subject.id, amount=Decimal(amount))
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


# ---------------------------------------------------------------- P1-05


def test_有期初引用的科目不能删(client, auth, db, book, subjects):
    """只有期初、没有凭证引用 —— 原来这一条恰恰能删掉，删完余额就没了。"""
    from app.models import Subject

    s = db.get(Subject, subjects["1001"].id)
    _seed_opening(db, book, s, "500.00")
    n_before = db.scalar(select(PeriodOpening).where(PeriodOpening.subject_id == s.id)
                         .with_only_columns(__import__("sqlalchemy").func.count()))
    assert n_before == 1

    r = client.delete(f"/api/books/{book.id}/subjects/{s.id}", headers=auth)

    assert r.status_code == 400, f"应当拒绝删除，实际 {r.status_code}：{r.text}"
    assert "期初" in r.json()["detail"]
    db.expire_all()
    assert db.get(Subject, s.id) is not None, "科目被删了"
    n_after = db.scalar(select(PeriodOpening).where(PeriodOpening.subject_id == s.id)
                        .with_only_columns(__import__("sqlalchemy").func.count()))
    assert n_after == n_before, "期初记录被动过"


def test_期初金额没被级联抹掉(client, auth, db, book, subjects):
    """审计记录的原始形态：余额 0 → -100。"""
    from app.models import Subject

    s = db.get(Subject, subjects["1001"].id)
    _seed_opening(db, book, s, "100.00")
    client.delete(f"/api/books/{book.id}/subjects/{s.id}", headers=auth)
    db.expire_all()

    left = db.scalars(select(PeriodOpening).where(PeriodOpening.subject_id == s.id)).all()
    assert len(left) == 1, "期初被级联抹掉了 —— 余额会凭空漂移"
    assert left[0].amount == Decimal("100.00")


def test_数据库层也拦一道(db, book, subjects):
    """绕过应用层直接删，有期初引用时数据库也必须拒绝。

    应用层会先挡，但护栏的意义就在「应用层哪天漏了」——
    这套系统的结账、凭证编辑、期初不可变都是靠触发器兜底的，同一思路。
    """
    from app.models import Subject
    from sqlalchemy.exc import DBAPIError

    s = db.get(Subject, subjects["1001"].id)
    _seed_opening(db, book, s, "100.00")

    with pytest.raises(DBAPIError):
        db.execute(
            __import__("sqlalchemy").delete(Subject).where(Subject.id == s.id))
        db.commit()
    db.rollback()


def test_没有期初的科目仍可删(client, auth, db, book, subjects):
    """收紧不能误伤：真正没被任何东西引用的科目照删。"""
    from app.models import Subject

    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(code="1997", name="干净的科目"))
    assert r.status_code == 201, r.text
    sid_ = r.json()["id"]

    r = client.delete(f"/api/books/{book.id}/subjects/{sid_}", headers=auth)
    assert r.status_code == 200, f"无引用科目不该被拦：{r.status_code} {r.text}"
    db.expire_all()
    assert db.get(Subject, sid_) is None


def test_停用是期初科目的正确出路(client, auth, db, book, subjects):
    """报错文案指向的出路必须真的可行。"""
    from app.models import Subject

    s = db.get(Subject, subjects["1001"].id)
    _seed_opening(db, book, s, "100.00")

    r = client.patch(f"/api/books/{book.id}/subjects/{s.id}", headers=auth,
                     json=_payload(code=s.code, name=s.name, category=s.category,
                                   direction=s.direction, active=False))
    assert r.status_code == 200, f"停用被拒：{r.status_code} {r.text}"
    db.expire_all()
    assert db.get(Subject, s.id).active is False


# ---------------------------------------------------------------- P1-06


def test_跨账套设置父科目被拒(client, auth, db, book, other_book, subjects):
    """P1-06 的原始场景：HTTP 200，科目变孤儿。"""
    from app.models import Subject

    victim = db.get(Subject, subjects["1012"].id)
    foreign_parent = db.scalar(select(Subject).where(Subject.book_id == other_book.id).limit(1))

    r = client.patch(f"/api/books/{book.id}/subjects/{victim.id}", headers=auth,
                     json=_payload(code=victim.code, name=victim.name,
                                   category=victim.category, direction=victim.direction,
                                   parent_id=foreign_parent.id))
    assert r.status_code == 400, f"跨账套挂接被放行了：{r.status_code} {r.text}"
    assert "上级科目不存在" in r.json()["detail"]
    db.expire_all()
    assert db.get(Subject, victim.id).parent_id == victim.parent_id, "父节点真被改掉了"


def test_改科目科目不会从本账套树里消失(client, auth, db, book, other_book, subjects):
    """孤儿节点 = UI 上看不见但账还在，最难排查。直接查列表来证明。"""
    from app.models import Subject

    victim = db.get(Subject, subjects["1012"].id)
    foreign_parent = db.scalar(select(Subject).where(Subject.book_id == other_book.id).limit(1))
    client.patch(f"/api/books/{book.id}/subjects/{victim.id}", headers=auth,
                 json=_payload(code=victim.code, name=victim.name,
                               category=victim.category, direction=victim.direction,
                               parent_id=foreign_parent.id))
    db.expire_all()

    tree = client.get(f"/api/books/{book.id}/subjects", headers=auth).json()

    def ids(nodes):
        out = set()
        for n in nodes:
            out.add(n["id"])
            out |= ids(n["children"])
        return out

    assert victim.id in ids(tree), "科目从本账套的科目树里消失了（成了孤儿）"


def test_父节点设为自身被拒(client, auth, db, book, subjects):
    from app.models import Subject

    s = db.get(Subject, subjects["1001"].id)
    r = client.patch(f"/api/books/{book.id}/subjects/{s.id}", headers=auth,
                     json=_payload(code=s.code, name=s.name, category=s.category,
                                   direction=s.direction, parent_id=s.id))
    assert r.status_code == 400
    assert "不能是它自己" in r.json()["detail"]


def test_父节点设为自己的下级被拒(client, auth, db, book, subjects):
    """环路：2221 -> 222101 -> 22210101，再把 2221 的父设成 22210101。

    必须用全表唯一的三层链：换成 1002/1012 这种平级科目就构不成环，
    改父本身是合法的（返回 200），这条护栏就测不到了。
    """
    from app.models import Subject

    top = db.get(Subject, subjects["2221"].id)
    grandchild = db.get(Subject, subjects["22210101"].id)

    r = client.patch(f"/api/books/{book.id}/subjects/{top.id}", headers=auth,
                     json=_payload(code=top.code, name=top.name, category=top.category,
                                   direction=top.direction, parent_id=grandchild.id))
    assert r.status_code == 400
    assert "循环" in r.json()["detail"]
    db.expire_all()
    assert db.get(Subject, top.id).parent_id == top.parent_id


def test_合法改父后父节点is_leaf跟着变(client, auth, db, book, subjects):
    """is_leaf 是冗余字段，错了会让期初录到汇总级科目上 —— 那是账面错误。"""
    from app.models import Subject

    new_parent = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                             json=_payload(code="1900", name="新的一级科目",
                                           is_leaf=True)).json()["id"]
    child = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                        json=_payload(code="1905", name="新的一级下的明细",
                                      is_leaf=True)).json()["id"]
    db.expire_all()
    assert db.get(Subject, new_parent).is_leaf is True

    r = client.patch(f"/api/books/{book.id}/subjects/{child}", headers=auth,
                     json=_payload(code="1905", name="新的一级下的明细", is_leaf=True,
                                   parent_id=new_parent))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Subject, new_parent).is_leaf is False, "父节点仍有下级，is_leaf 应为 False"


def test_移出下级后父节点恢复is_leaf(client, auth, db, book, subjects):
    from app.models import Subject

    new_parent = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                             json=_payload(code="1900", name="容器",
                                           is_leaf=True)).json()["id"]
    child = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                        json=_payload(code="1905", name="明细", is_leaf=True,
                                      parent_id=new_parent)).json()["id"]
    db.expire_all()
    assert db.get(Subject, new_parent).is_leaf is False

    r = client.patch(f"/api/books/{book.id}/subjects/{child}", headers=auth,
                     json=_payload(code="1905", name="明细", is_leaf=True, parent_id=None))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Subject, new_parent).is_leaf is True, "下级都移走了，父节点仍是叶子"


def test_改父后level跟随(client, auth, db, book, subjects):
    from app.models import Subject

    new_parent = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                             json=_payload(code="1900", name="一级容器", is_leaf=False)
                             ).json()["id"]
    child = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                        json=_payload(code="1905", name="二级明细", is_leaf=True)).json()["id"]
    db.expire_all()
    assert db.get(Subject, child).level == 1

    r = client.patch(f"/api/books/{book.id}/subjects/{child}", headers=auth,
                     json=_payload(code="1905", name="二级明细", is_leaf=True,
                                   parent_id=new_parent))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Subject, child).level == 2, "改父之后级次没跟着变"


def test_新建科目的父校验没被改弱(client, auth, db, book, other_book):
    """复用同一个校验函数后，新建路径必须保持原有强度。"""
    foreign_parent = db.scalar(select(Subject).where(Subject.book_id == other_book.id).limit(1))
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(code="1902", name="跨账套挂接", parent_id=foreign_parent.id))
    assert r.status_code == 400
    assert "上级科目不存在" in r.json()["detail"]


def test_不存在的父科目被拒(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth,
                    json=_payload(code="1903", name="挂到虚空", parent_id=999999))
    assert r.status_code == 400
