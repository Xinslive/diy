"""凭证录入页每行科目格的余额。

用户在批注里点名要的东西：**纯数字**，借正贷负，辅助记账用，
只在凭证可编辑时出现。这里把四条口径分别钉死 ——
「除本张以外」这件事最容易在以后被当成多余逻辑删掉，所以钉得最重。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Book, UserBook


@pytest.fixture
def other_book(db, admin_user):
    """另一个账套 —— 用来验「别家的凭证 id 传上来会怎样」。"""
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    b = Book(company_name="别家公司", name=f"别的账套{uuid.uuid4().hex[:6]}",
             start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


def _balances(client, auth, book, period, **params):
    r = client.get(f"/api/books/{book.id}/ledger/voucher-balances",
                   params={"period_id": period.id, **params}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["balances"]


# ------------------------------------------------------------------ 口径一：借正贷负


def test_借方余额是正数(client, auth, db, book, periods, subjects, make_voucher):
    """1001 这类资产科目：钱在借方，就显示成正数。"""
    make_voucher("2026-01-10", dr=[("1001", 800, "存")],
                 cr=[("5001", 800, "存")], posted=True)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["1001"].id)]) == Decimal("800")


def test_资产科目出现贷方余额时是负数(client, auth, db, book, periods, subjects, make_voucher):
    """用户点名的那条：1001 这类科目余额本该在借方，一旦落在贷方就显示成负的。

    这条钉的是「按符号判方向，不按科目属性」：1001 是资产，
    按科目属性判的话这里会被显示成正的 800，跟试算平衡自相矛盾。
    """
    # 收预收款：贷 1001（库存现金），钱在贷方
    make_voucher("2026-01-10", dr=[("5051", 800, "x")],
                 cr=[("1001", 800, "收预收")], posted=True)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["1001"].id)]) == Decimal("-800")


def test_负债科目的正常贷方余额同样是负数(client, auth, db, book, periods, subjects, make_voucher):
    """负债有余额本来就落在贷方，所以它一直是负的 —— 符号说的是「在哪一边」，不是「好不好」。

    把这条和上一条并排钉住，是为了防止有人把负号理解成「异常/红字」，
    哪天给负债余额也改成正数显示。
    """
    make_voucher("2026-01-10", dr=[("1001", 800, "x")],
                 cr=[("2001", 800, "借入")], posted=True)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["2001"].id)]) == Decimal("-800")
    assert Decimal(b[str(subjects["1001"].id)]) == Decimal("800")  # 资产这边是正的


def test_零余额就是零不是负零(client, auth, db, book, periods, subjects):
    """没动过的科目返回 "0"，不是 "-0" —— 前端靠它判断「不显示」。"""
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["1001"].id)]) == 0


def test_余额与科目余额表口径一致(client, auth, db, book, periods, subjects, make_voucher):
    """同一个期间、同一批数据，两个页面的期末余额必须逐分对上。

    钉的是「不许出现第二套余额计算」：录入页的余额是为了方便，不是为了另立一套口径。
    """
    make_voucher("2026-03-10", dr=[("1001", 1234.56, "x")],
                 cr=[("5001", 1234.56, "x")], posted=True)
    r = client.get(f"/api/books/{book.id}/ledger/balance",
                   params={"period_id": periods[3].id}, headers=auth)
    rows = {row["subject_id"]: row for row in r.json()["rows"]}

    b = _balances(client, auth, book, periods[3])
    for sid, row in rows.items():
        sign = Decimal("-1") if row["direction"] == "贷" else Decimal("1")
        assert Decimal(b[str(sid)]) == sign * Decimal(str(row["closing"])), row["code"]


# ------------------------------------------------------------------ 口径二：除本张以外


def test_本张凭证不计入自己的余额(client, auth, db, book, periods, subjects, make_voucher):
    """正在编辑的这张要被剔掉。

    它会边填边自动保存：算进去的话，往 1001 里填 500，余额就 +500，
    看着像「刚存的已经进账了」，其实是在跟自己说话。剔掉之后这个数在录入全程是稳的。
    """
    v = make_voucher("2026-02-10", dr=[("1001", 5000, "收")],
                     cr=[("5001", 5000, "收")], posted=False)
    sid_ = str(subjects["1001"].id)

    # 不传 voucher_id：这张在账上，余额含它
    assert Decimal(_balances(client, auth, book, periods[2])[sid_]) == Decimal("5000")
    # 传 voucher_id：剔掉它，回到录入前的余额
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v.id)[sid_]) == 0


def test_剔除只影响本张不影响别的凭证(client, auth, db, book, periods, subjects, make_voucher):
    """闸门不能开过头：另一张凭证的分录照常计入。"""
    make_voucher("2026-02-10", dr=[("1001", 700, "a")],
                 cr=[("5001", 700, "a")], posted=False)
    other = make_voucher("2026-02-11", dr=[("1001", 300, "b")],
                         cr=[("5001", 300, "b")], posted=False)
    sid_ = str(subjects["1001"].id)
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=other.id)[sid_]) \
        == Decimal("700")


# ------------------------------------------------------------------ 口径三：时点余额


def test_月中那张看到的是它之前的账不是月底的账(client, auth, db, book, periods, subjects, make_voucher):
    """余额是**时点**的：这一张记进去之前，账上已经有多少。

    用户 2026-10-25：「目前是固定显示期末余额」。
    三张各借 100：第一张前面是 0，第二张前面是 100，第三张前面是 200 ——
    而月底的账是 300。三张都看到 300 才是「固定显示期末余额」的那个毛病。

    这里返回的是**基数**（这张之前），这张自己的分录由前端按行补回去 ——
    所以第一张是 0，页面上显示的是 0 + 本张自己那 100 = 100。
    """
    v1 = make_voucher("2026-02-10", dr=[("1001", 100, "a")],
                      cr=[("5001", 100, "a")], posted=False)
    v2 = make_voucher("2026-02-11", dr=[("1001", 100, "b")],
                      cr=[("5001", 100, "b")], posted=False)
    v3 = make_voucher("2026-02-12", dr=[("1001", 100, "c")],
                      cr=[("5001", 100, "c")], posted=False)
    sid_ = str(subjects["1001"].id)
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v1.id)[sid_]) == Decimal("0")
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v2.id)[sid_]) == Decimal("100")
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v3.id)[sid_]) == Decimal("200")
    # 不指明位置 → 整期口径：这一期记完还剩多少
    assert Decimal(_balances(client, auth, book, periods[2])[sid_]) == Decimal("300")


def test_插在中间的新凭证看到的是它前面的账(client, auth, db, book, periods, subjects, make_voucher):
    """一张**还没落库**、插在某张中间的新凭证，位置由参照凭证定。

    插在参照**后面** → 往后挪一号，前面只有参照那张；
    插在参照**前面** → 占参照的号（参照自己顺延一位），前面什么都没有。
    """
    v1 = make_voucher("2026-02-10", dr=[("1001", 100, "a")],
                      cr=[("5001", 100, "a")], posted=False)
    make_voucher("2026-02-11", dr=[("1001", 100, "b")],
                 cr=[("5001", 100, "b")], posted=False)
    sid_ = str(subjects["1001"].id)
    after = _balances(client, auth, book, periods[2],
                      insert_after=v1.id, insert_position="after")
    assert Decimal(after[sid_]) == Decimal("100"), "插在后面时，前面只有参照那张"
    before = _balances(client, auth, book, periods[2],
                       insert_after=v1.id, insert_position="before")
    assert Decimal(before[sid_]) == Decimal("0"), "插在前面时，前面什么都没有"


def test_别的期间的凭证不参与位置计算(client, auth, db, book, periods, subjects, make_voucher):
    """位置是「**本期**里的第几张」：下个月那张号更大，但不能被减掉。

    减错了会得到比期初还小的数（负数），一眼就看出不对。
    """
    v1 = make_voucher("2026-02-10", dr=[("1001", 100, "a")],
                      cr=[("5001", 100, "a")], posted=False)
    make_voucher("2026-03-10", dr=[("1001", 500, "b")],
                 cr=[("5001", 500, "b")], posted=False)
    sid_ = str(subjects["1001"].id)
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v1.id)[sid_]) == Decimal("0")


def test_位置只认本期的那张凭证(client, auth, db, book, periods, subjects, make_voucher):
    """别期（或别账套）的凭证 id 传上来只能当没传。

    跟「跨账套那张被忽略」是同一个道理：位置算错了，基数就整块错。
    """
    v1 = make_voucher("2026-02-10", dr=[("1001", 100, "a")],
                      cr=[("5001", 100, "a")], posted=False)
    other_period = make_voucher("2026-03-10", dr=[("1001", 500, "b")],
                                cr=[("5001", 500, "b")], posted=False)
    sid_ = str(subjects["1001"].id)
    # 拿 3 月那张的 id 去问 2 月 → 位置不明 → 退回整期口径（2 月的期末 = 100）
    assert Decimal(_balances(client, auth, book, periods[2],
                              voucher_id=other_period.id)[sid_]) == Decimal("100")
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v1.id)[sid_]) == Decimal("0")


# ------------------------------------------------------------------ 口径四：含未记账


def test_默认含未记账凭证(client, auth, db, book, periods, subjects, make_voucher):
    """辅助记账看的是「这个人眼下记到哪儿了」。

    月底集中记账之前，账上几乎全是未记账凭证；默认排除的话余额整月都是空的，
    这个提示就白做了。
    """
    make_voucher("2026-01-10", dr=[("1001", 900, "x")],
                 cr=[("5001", 900, "x")], posted=False)
    assert Decimal(_balances(client, auth, book, periods[1])[str(subjects["1001"].id)]) \
        == Decimal("900")


# ------------------------------------------------------------------ 口径五：期间 / 归属


def test_按期间取余额(client, auth, db, book, periods, subjects, make_voucher):
    """余额是「到哪一期为止」，不是「这一期发生多少」：1 月那笔在 3 月也还在。"""
    make_voucher("2026-01-10", dr=[("1001", 400, "x")],
                 cr=[("5001", 400, "x")], posted=False)
    sid_ = str(subjects["1001"].id)
    assert Decimal(_balances(client, auth, book, periods[1])[sid_]) == Decimal("400")
    assert Decimal(_balances(client, auth, book, periods[3])[sid_]) == Decimal("400")


def test_上级科目一并给出(client, auth, db, book, periods, subjects, make_voucher):
    """凭证只挂在末级上，但排凭证时非末级也可能被临时选上去看一眼余额。

    上级取 1101 短期投资：1002 在 4-2-2 表里已是末级，没有下级可卷。
    """
    make_voucher("2026-01-10", dr=[("110101", 600, "x")],
                 cr=[("5001", 600, "x")], posted=False)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["1101"].id)]) == Decimal("600")


def test_跨账套的凭证id被忽略而不是报错(client, auth, db, book, other_book, periods, admin_user):
    """别家账的凭证 id 传上来只能当没传：宁可少剔一张，也不能报错或跨账套取数。"""
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart
    from app.accounting.vouchers import create_voucher
    from app.models import Subject
    from datetime import date

    subs = {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == other_book.id)).all()}
    v = create_voucher(
        db, book_id=other_book.id, user=admin_user, voucher_date=date(2026, 1, 10),
        entries=[{"subject_id": subs["1001"].id, "direction": "DEBIT",
                  "amount": "400", "summary": "x"},
                 {"subject_id": subs["5001"].id, "direction": "CREDIT",
                  "amount": "400", "summary": "x"}],
        voucher_no=None, source="MANUAL", remark="", commit=False)
    db.commit()
    db.refresh(v)

    b = _balances(client, auth, book, periods[1], voucher_id=v.id)
    # 本账套一张凭证都没有，余额仍然是 0（别家那 400 没串过来）
    assert all(Decimal(x) == 0 for x in b.values()), "别家账的余额串进来了"


def test_需要登录(client, db, book, periods):
    r = client.get(f"/api/books/{book.id}/ledger/voucher-balances",
                   params={"period_id": periods[1].id})
    assert r.status_code == 401, f"未登录竟然拿到了余额：{r.status_code}"


def test_不存在账套取不到(client, auth, db):
    r = client.get("/api/books/99999999/ledger/voucher-balances", headers=auth)
    assert r.status_code in (403, 404), f"别人的/不存在的账套返回了 {r.status_code}"
