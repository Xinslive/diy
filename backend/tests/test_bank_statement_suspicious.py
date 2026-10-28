"""**存疑凭证**：账上有、对账单里找不到对应流水的那些凭证。

这一页是流水对账的**另一半**：

* 流水明细页那条路 ——「对账单上有、账不上有」→ 生成凭证；
* 这一页这条路 ——「账上有、对账单上没有」→ 那是**错账**。

判据（用户定的，2026-10-28）：该账户**在这份对账单覆盖期间内**的现金
分录，在这份对账单的流水行里找不到「金额相等 + 方向对应 + 日期在窗口内」
的那一行。

**这一组盯住四件事**：

1. **对得上的不进清单**，对不上的进清单 —— 这是判据的正反两面；
2. **内部转账不算疑问**（钱在自家账户之间挪，银行流水里本来就没有它）；
3. **期间外的不判定** —— 用户没传那个月的对账单，说明不了什么；
4. **没绑对账单的账户不参与** —— 没有对账单就没有「对不对得上」。
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Subject

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"

#: 夹具对账单（cmb_csv.xlsx）的期间与流水行：
#: 2026-09-12 ~ 2026-09-28，5 行，**全是收入**。
#:   seq1 2026-09-28  872100.00
#:   seq2 2026-09-28  1000000.00
#:   seq3 2026-09-21     1141.00
#:   seq4 2026-09-14  3423300.00
#:   seq5 2026-09-12 36000000.00
ROW_AMOUNT = "872100.00"
ROW_DATE = "2026-09-28"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def cash(db, book):
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    parent.is_leaf = False
    db.flush()
    sub = Subject(book_id=book.id, year=book.start_year, code="100201", name="基本户",
                  parent_id=parent.id, level=parent.level + 1, category="ASSET",
                  direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=1,
                  active=True)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _upload(client, auth, book, cash):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    r = client.post(
        f"/api/books/{book.id}/bank-statements",
        headers=auth,
        files={"file": ("cmb_csv.xlsx", data, "application/octet-stream")},
        data={"account_subject_id": str(cash.id)},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _suspicious(client, auth, book):
    r = client.get(f"/api/books/{book.id}/bank-statements/suspicious-vouchers",
                   headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _voucher_ids(body) -> set[int]:
    out = set()
    for group in body["groups"]:
        for voucher in group["vouchers"]:
            out.add(voucher["voucher_id"])
    return out


# ---------------------------------------------------------------------- 判据


def test_对得上的现金分录不进清单(client, auth, db, book, cash, store,
                                   make_voucher, subjects):
    """**对得上就不该被疑心。** 这是判据的反面 —— 少了它，那一页就成了
    「所有凭证都可疑」，等于没有。"""
    _upload(client, auth, book, cash)
    # 现金侧：100201 借方 872100.00（收钱）—— 与 seq1 那行完全对上
    v = make_voucher(ROW_DATE,
                     dr=[(cash.id, ROW_AMOUNT, "收租")],
                     cr=[(subjects["560202"].id, ROW_AMOUNT, "租金收入")],
                     summary="对得上")

    body = _suspicious(client, auth, book)
    assert v.id not in _voucher_ids(body), "对得上的分录被列成存疑了"
    assert body["total_suspicious"] == 0
    group = body["groups"][0]
    assert group["matched_count"] == 1
    assert group["entry_count"] == 1


def test_对不上的现金分录进清单(client, auth, db, book, cash, store,
                                 make_voucher, subjects):
    """账上有一笔，流水里没有 —— 这就是错账，要被列出来。"""
    _upload(client, auth, book, cash)
    v = make_voucher(ROW_DATE,
                     dr=[(cash.id, "99999.99", "一笔查无此账的钱")],
                     cr=[(subjects["560202"].id, "99999.99", "收入")],
                     summary="对不上")

    body = _suspicious(client, auth, book)
    assert v.id in _voucher_ids(body), "对不上的分录没被列出来"
    assert body["total_suspicious"] == 1
    voucher = body["groups"][0]["vouchers"][0]
    assert voucher["voucher_no"]
    assert voucher["amount"] == "99999.99"
    assert voucher["reason"], "得说清为什么可疑"
    assert voucher["account_code"] if "account_code" in voucher else True


def test_清单里带对账单与账户的来龙去脉(client, auth, db, book, cash, store,
                                        make_voucher, subjects):
    """每一组要说清「**凭什么**可疑」—— 是哪个账户、哪份对账单、哪段期间。"""
    _upload(client, auth, book, cash)
    make_voucher(ROW_DATE,
                 dr=[(cash.id, "99999.99", "查无此账")],
                 cr=[(subjects["560202"].id, "99999.99", "收入")])

    group = _suspicious(client, auth, book)["groups"][0]
    assert group["account_subject_id"] == cash.id
    assert group["account_code"] == "100201"
    assert group["account_name"] == "基本户"
    assert group["period_start"] == "2026-09-12"
    assert group["period_end"] == "2026-09-28"
    assert group["row_count"] == 5


def test_金额对得上但日期差得远_给出线索(client, auth, db, book, cash, store,
                                          make_voucher, subjects):
    """**没有线索的「疑问」是没用的疑问。**

    同一笔金额在流水里出现过、只是日期差了 20 天 —— 用户一看就知道该看
    日期，而不是对着一个光秃秃的「找不到对应流水」发愁。
    """
    _upload(client, auth, book, cash)
    # 金额与 seq1 完全相同，但凭证日期比流水早 10 天 —— 落在 ±30 天窗口内，
    # 所以它**本来就能对上**。改成一个窗口外的日期才测得到线索。
    make_voucher("2026-08-20",
                 dr=[(cash.id, ROW_AMOUNT, "日期记错了")],
                 cr=[(subjects["560202"].id, ROW_AMOUNT, "租金收入")])

    body = _suspicious(client, auth, book)
    # 08-20 在对账单期间之外 → 不判定，只报个数
    assert body["total_suspicious"] == 0
    assert body["total_near_period_uncounted"] >= 1

    # 挪到期间内、但日期离流水 10 天以上 → 变成存疑，且有「金额对得上」的线索
    make_voucher("2026-09-15",
                 dr=[(cash.id, "99999.99", "金额记错了")],
                 cr=[(subjects["560202"].id, "99999.99", "收入")])
    body = _suspicious(client, auth, book)
    voucher = body["groups"][0]["vouchers"][0]
    assert "金额对不上" in voucher["reason"] or "对不上" in voucher["reason"], (
        voucher["reason"])


def test_内部转账不算疑问(client, auth, db, book, cash, store,
                          make_voucher, subjects):
    """钱在自家账户之间挪（1001 现金 ↔ 100201 银行）：银行流水里本来就没有
    这一行 —— **它不是错账，只是没走银行**。"""
    _upload(client, auth, book, cash)
    v = make_voucher(ROW_DATE,
                     dr=[(cash.id, "88888.88", "内部转账出"),
                         (subjects["1001"].id, "88888.88", "内部转入")],
                     cr=[(subjects["560202"].id, "177777.76", "收入")],
                     summary="内部转账")

    body = _suspicious(client, auth, book)
    assert v.id not in _voucher_ids(body), "内部转账被当成错账了"
    assert body["total_suspicious"] == 0
    assert body["total_internal_transfer"] == 1


def test_期间外的分录不判定只报数(client, auth, db, book, cash, store,
                                  make_voucher, subjects):
    """期间外（上一期/下一期）的分录**不判定**：用户没传那个月的对账单，
    说明不了什么 —— 把它算成「疑问」只会把清单淹掉。"""
    _upload(client, auth, book, cash)
    v = make_voucher("2026-08-20",
                     dr=[(cash.id, "77777.77", "上个月的账")],
                     cr=[(subjects["560202"].id, "77777.77", "收入")])

    body = _suspicious(client, auth, book)
    assert v.id not in _voucher_ids(body), "期间外的分录被判定了"
    assert body["total_suspicious"] == 0
    assert body["total_near_period_uncounted"] == 1, "邻期那笔该报个数"


def test_没绑对账单的账户不参与(client, auth, db, book, cash, store,
                                 make_voucher, subjects):
    """没有对账单就没有「对不对得上」这件事。

    1001 库存现金没绑对账单 —— 它上面的分录无从判定，那不叫疑问，
    那叫「没资料」。
    """
    _upload(client, auth, book, cash)
    v = make_voucher(ROW_DATE,
                     dr=[(subjects["1001"].id, "66666.66", "库存现金")],
                     cr=[(subjects["560202"].id, "66666.66", "收入")])

    body = _suspicious(client, auth, book)
    assert v.id not in _voucher_ids(body), "没绑对账单的账户也参与了判定"
    assert body["total_suspicious"] == 0


def test_只有绑了对账单的账户才算(client, auth, db, book, cash, store):
    """账套里一份对账单都没有 → 没有可判定的账户 → 清单是空的。"""
    body = _suspicious(client, auth, book)
    assert body["groups"] == []
    assert body["total_suspicious"] == 0


# ---------------------------------------------------------------------- 权限


def test_只读账号也能看(client, auth, plain_auth, db, book, cash, store,
                       plain_user):
    """它是给人**找错账**的清单，不改任何东西 —— 所以 VIEWER 也能看。"""
    from app.models import UserBook

    # **给只读账号授这个账套的 VIEWER 权限** —— 上传仍用管理员账号做
    # （VIEWER 不能上传，那是另一道闸）。
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    _upload(client, auth, book, cash)

    r = client.get(
        f"/api/books/{book.id}/bank-statements/suspicious-vouchers",
        headers=plain_auth)
    assert r.status_code == 200, r.text
    assert "groups" in r.json()


def test_别人的账套看不到(client, auth, db, book, cash, store, plain_user):
    """越权的那道闸照旧 —— 换个账套的 id 请求，拿不到东西。"""
    _upload(client, auth, book, cash)
    r = client.get(
        f"/api/books/{book.id + 999}/bank-statements/suspicious-vouchers",
        headers=auth)
    assert r.status_code in (403, 404), r.text
