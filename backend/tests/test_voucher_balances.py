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
    make_voucher("2026-01-10", dr=[("100201", 800, "存")],
                 cr=[("600101", 800, "存")], posted=True)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["100201"].id)]) == Decimal("800")


def test_资产科目出现贷方余额时是负数(client, auth, db, book, periods, subjects, make_voucher):
    """用户点名的那条：1001 这类科目余额本该在借方，一旦落在贷方就显示成负的。

    这条钉的是「按符号判方向，不按科目属性」：1002 是资产，
    按科目属性判的话这里会被显示成正的 800，跟试算平衡自相矛盾。
    """
    # 收预收款：贷 100201（银行存款），钱在贷方
    make_voucher("2026-01-10", dr=[("6051", 800, "x")],
                 cr=[("100201", 800, "收预收")], posted=True)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["100201"].id)]) == Decimal("-800")


def test_负债科目的正常贷方余额同样是负数(client, auth, db, book, periods, subjects, make_voucher):
    """负债有余额本来就落在贷方，所以它一直是负的 —— 符号说的是「在哪一边」，不是「好不好」。

    把这条和上一条并排钉住，是为了防止有人把负号理解成「异常/红字」，
    哪天给负债余额也改成正数显示。
    """
    make_voucher("2026-01-10", dr=[("100201", 800, "x")],
                 cr=[("2001", 800, "借入")], posted=True)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["2001"].id)]) == Decimal("-800")
    assert Decimal(b[str(subjects["100201"].id)]) == Decimal("800")  # 资产这边是正的


def test_零余额就是零不是负零(client, auth, db, book, periods, subjects):
    """没动过的科目返回 "0"，不是 "-0" —— 前端靠它判断「不显示」。"""
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["100201"].id)]) == 0


def test_余额与科目余额表口径一致(client, auth, db, book, periods, subjects, make_voucher):
    """同一个期间、同一批数据，两个页面的期末余额必须逐分对上。

    钉的是「不许出现第二套余额计算」：录入页的余额是为了方便，不是为了另立一套口径。
    """
    make_voucher("2026-03-10", dr=[("100201", 1234.56, "x")],
                 cr=[("600101", 1234.56, "x")], posted=True)
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

    它会边填边自动保存：算进去的话，往 1002 里填 500，余额就 +500，
    看着像「刚存的已经进账了」，其实是在跟自己说话。剔掉之后这个数在录入全程是稳的。
    """
    v = make_voucher("2026-02-10", dr=[("100201", 5000, "收")],
                     cr=[("600101", 5000, "收")], posted=False)
    sid_ = str(subjects["100201"].id)

    # 不传 voucher_id：这张在账上，余额含它
    assert Decimal(_balances(client, auth, book, periods[2])[sid_]) == Decimal("5000")
    # 传 voucher_id：剔掉它，回到录入前的余额
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=v.id)[sid_]) == 0


def test_剔除只影响本张不影响别的凭证(client, auth, db, book, periods, subjects, make_voucher):
    """闸门不能开过头：另一张凭证的分录照常计入。"""
    make_voucher("2026-02-10", dr=[("100201", 700, "a")],
                 cr=[("600101", 700, "a")], posted=False)
    other = make_voucher("2026-02-11", dr=[("100201", 300, "b")],
                         cr=[("600101", 300, "b")], posted=False)
    sid_ = str(subjects["100201"].id)
    assert Decimal(_balances(client, auth, book, periods[2], voucher_id=other.id)[sid_]) \
        == Decimal("700")


# ------------------------------------------------------------------ 口径三：含未记账


def test_默认含未记账凭证(client, auth, db, book, periods, subjects, make_voucher):
    """辅助记账看的是「这个人眼下记到哪儿了」。

    月底集中记账之前，账上几乎全是未记账凭证；默认排除的话余额整月都是空的，
    这个提示就白做了。
    """
    make_voucher("2026-01-10", dr=[("100201", 900, "x")],
                 cr=[("600101", 900, "x")], posted=False)
    assert Decimal(_balances(client, auth, book, periods[1])[str(subjects["100201"].id)]) \
        == Decimal("900")


# ------------------------------------------------------------------ 口径四：期间 / 归属


def test_按期间取余额(client, auth, db, book, periods, subjects, make_voucher):
    """余额是「到哪一期为止」，不是「这一期发生多少」：1 月那笔在 3 月也还在。"""
    make_voucher("2026-01-10", dr=[("100201", 400, "x")],
                 cr=[("600101", 400, "x")], posted=False)
    sid_ = str(subjects["100201"].id)
    assert Decimal(_balances(client, auth, book, periods[1])[sid_]) == Decimal("400")
    assert Decimal(_balances(client, auth, book, periods[3])[sid_]) == Decimal("400")


def test_上级科目一并给出(client, auth, db, book, periods, subjects, make_voucher):
    """凭证只挂在末级上，但排凭证时非末级也可能被临时选上去看一眼余额。"""
    make_voucher("2026-01-10", dr=[("100201", 600, "x")],
                 cr=[("600101", 600, "x")], posted=False)
    b = _balances(client, auth, book, periods[1])
    assert Decimal(b[str(subjects["1002"].id)]) == Decimal("600")


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
        entries=[{"subject_id": subs["100201"].id, "direction": "DEBIT",
                  "amount": "400", "summary": "x"},
                 {"subject_id": subs["600101"].id, "direction": "CREDIT",
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
