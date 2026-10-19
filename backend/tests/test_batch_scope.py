"""批量记账/反记账的账套归属与无效 ID（审计 P1-01 + P1-08）。

P1-01：``post_vouchers`` / ``unpost_vouchers`` 只按 ``Voucher.id.in_(ids)`` 查，
       路由层拿到的 book 压根没往下传 —— 拿 A 账套的接口就能把 B 账套的
       凭证状态改掉。审计动态复现：HTTP 200，状态真的变了。

P1-08：同一段代码查不到就访问 ``vouchers[0]``，传不存在的 ID → IndexError
       → HTTP 500。

这两条修在一起：查询强制带 book_id，且 ID 集合解析不完整就**整批拒绝**。
整批拒绝而不是「跳过越界的、其余照成功」，是因为部分成功最难排查 ——
用户以为 3 张都记上了，实际 2 张，报错还被成功计数盖住。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.accounting.vouchers import post_vouchers, unpost_vouchers
from app.models import Book, UserBook, Voucher
from app.seed.chart_of_accounts import install_chart


@pytest.fixture
def other_book(db, admin_user):
    """第二个账套，供越权场景使用。"""
    import uuid

    from app.accounting.periods import create_periods

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


def _voucher_in(db, book, admin_user, subjects_by_book, day="2026-01-05"):
    """在指定账套建一张凭证。

    ``subjects_by_book`` 两种形态都收：``subjects`` 夹具给的是 {code: Subject}，
    别的账套则要自己查出来是列表。统一成 code -> Subject 再用。
    """
    from datetime import date as _date

    from app.accounting.vouchers import create_voucher

    values = (subjects_by_book.values() if isinstance(subjects_by_book, dict)
              else subjects_by_book)
    subs = {s.code: s for s in values}
    return create_voucher(
        db, book_id=book.id, user=admin_user,
        voucher_date=_date.fromisoformat(day),
        entries=[
            {"subject_id": subs["1002"].id, "direction": "DEBIT", "amount": "100", "summary": "x"},
            {"subject_id": subs["5001"].id, "direction": "CREDIT", "amount": "100", "summary": "x"},
        ], commit=True)


def test_跨账套记账被拒(db, book, other_book, admin_user, subjects):
    """A 账套的接口 + B 账套的凭证 ID → 拒绝，B 的凭证状态不变。"""
    from app.models import Subject

    other_subs = db.scalars(select(Subject).where(Subject.book_id == other_book.id)).all()
    v = _voucher_in(db, other_book, admin_user, other_subs)

    with pytest.raises(ValueError, match="不属于当前账套"):
        post_vouchers(db, book.id, [v.id], admin_user)
    db.rollback()
    db.refresh(v)
    assert v.status == "UNPOSTED", "跨账套凭证被记上了 —— 这是数据完整性破口"


def test_跨账套反记账也被拒(db, book, other_book, admin_user, subjects):
    """记账和反记账必须一样严 —— 只堵一半等于没堵。"""
    from app.models import Subject

    other_subs = db.scalars(select(Subject).where(Subject.book_id == other_book.id)).all()
    v = _voucher_in(db, other_book, admin_user, other_subs)
    post_vouchers(db, other_book.id, [v.id], admin_user)
    assert db.get(Voucher, v.id).status == "POSTED"

    with pytest.raises(ValueError, match="不属于当前账套"):
        unpost_vouchers(db, book.id, [v.id], admin_user)
    db.rollback()
    assert db.get(Voucher, v.id).status == "POSTED", "别人账套的凭证被反记账了"


def test_不存在的凭证ID被拒且不是500(db, book, admin_user):
    """传不存在的 ID → ValueError（路由转 400），不是 IndexError → 500。"""
    with pytest.raises(ValueError, match="凭证不存在"):
        post_vouchers(db, book.id, [999999], admin_user)
    db.rollback()

    with pytest.raises(ValueError, match="凭证不存在"):
        unpost_vouchers(db, book.id, [999999], admin_user)
    db.rollback()


def test_批次里混了一个越权ID就整批拒绝(db, book, other_book, admin_user, subjects):
    """1 张自己账套的 + 1 张别人账套的 → 全部不记，不做部分成功。"""
    from app.models import Subject

    own = _voucher_in(db, book, admin_user, subjects)
    other_subs = db.scalars(select(Subject).where(Subject.book_id == other_book.id)).all()
    stolen = _voucher_in(db, other_book, admin_user, other_subs)

    with pytest.raises(ValueError, match="不属于当前账套"):
        post_vouchers(db, book.id, [own.id, stolen.id], admin_user)
    db.rollback()

    db.refresh(own)
    assert own.status == "UNPOSTED", "部分成功：自己那张被记上了，用户却收到报错"
    db.refresh(stolen)
    assert stolen.status == "UNPOSTED"


def test_重复ID不会重复计数(db, book, make_voucher, admin_user):
    """[id, id, id] 应当只算一张 —— 去重后再比数量，否则计数会虚高。"""
    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    assert post_vouchers(db, book.id, [v.id, v.id, v.id], admin_user) == 1


def test_审计日志的book_id是请求的账套(db, book, make_voucher, admin_user):
    """审计记录的归属不能靠「批次里第一张是谁」。"""
    from app.models import AuditLog

    v = make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    post_vouchers(db, book.id, [v.id], admin_user)

    row = db.scalars(select(AuditLog).where(
        AuditLog.action == "POST", AuditLog.entity == "voucher").order_by(AuditLog.id.desc())
    ).first()
    assert row.book_id == book.id


# --------------------------------------------------------------------- HTTP 层


def test_跨账套记账的HTTP返回4xx(client, auth, db, book, other_book, admin_user, subjects):
    """接口层同样要拦住：状态不变，且返回 4xx 而不是 200。"""
    from app.models import Subject

    other_subs = db.scalars(select(Subject).where(Subject.book_id == other_book.id)).all()
    v = _voucher_in(db, other_book, admin_user, other_subs)

    r = client.post(f"/api/books/{book.id}/vouchers/post", headers=auth, json={"ids": [v.id]})
    assert 400 <= r.status_code < 500, f"返回了 {r.status_code}：{r.text}"
    db.expire_all()
    assert db.get(Voucher, v.id).status == "UNPOSTED"


def test_不存在的ID的HTTP不是500(client, auth, book):
    """P1-08 的验收：4xx，且能区分「不存在」。"""
    r = client.post(f"/api/books/{book.id}/vouchers/post", headers=auth, json={"ids": [999999]})
    assert r.status_code == 400, f"应该是 400，实际 {r.status_code}：{r.text}"
    assert "不存在" in r.json()["detail"]

    r = client.post(f"/api/books/{book.id}/vouchers/unpost", headers=auth, json={"ids": [999999]})
    assert r.status_code == 400, f"应该是 400，实际 {r.status_code}：{r.text}"
