"""账套磁贴的**个人**排序（PUT /api/books/order）。

这个特性的价值全在两件事上，所以测试也只钉这两件：

1. **排完真的存得住。** 不落库的拖拽等于没做 —— 刷新一次顺序就弹回去，
   用户试两下就再也不信这个功能了。
2. **只能排自己的。** 顺序写在 ``sys_user_book`` 上，是 (谁, 哪个账套)
   这一对的属性；接口要是照单全收别人的 id，就等于开了一个
   「拿任意 book_id 探权限」的口子。

其余几条（幂等、没排过的回落原序、序号稠密）防的是**半路坏掉**：
拖动是高频操作，重复提交和并发新建都得给出可预期的结果。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Book, UserBook


def _ids(client, auth):
    return [b["id"] for b in client.get("/api/books", headers=auth).json()]


def _more_books(db, admin_user, n: int, prefix: str = "序") -> list[Book]:
    """再造 n 个账套（挂在同一个用户下），用来凑够能看出先后的长度。"""
    from app.accounting.periods import create_periods

    out = []
    for i in range(n):
        b = Book(company_name=f"{prefix}公司{i}", name=f"{prefix}{i}", start_year=2026,
                 start_month=1)
        db.add(b)
        db.flush()
        create_periods(db, b)
        db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
        out.append(b)
    db.commit()
    for b in out:
        db.refresh(b)
    return out


# ------------------------------------------------------------------ 存得住


def test_排完刷新还在(client, auth, db, book, admin_user):
    _more_books(db, admin_user, 2)
    before = _ids(client, auth)
    assert len(before) >= 3, "样本不够排，看不出先后变化"

    flipped = list(reversed(before))
    r = client.put("/api/books/order", headers=auth, json={"book_ids": flipped})
    assert r.status_code == 200, r.text

    # 重新拉一次列表 —— 这一步才是重点：不是「当次返回对」，
    # 而是「下次进页面还是这个顺序」
    assert _ids(client, auth) == flipped


def test_没排过的时候顺序不变(client, auth, book, admin_user, db):
    """这个特性对不使用它的人必须完全不可见。"""
    _more_books(db, admin_user, 2, prefix="原序")
    got = _ids(client, auth)
    assert len(got) >= 3
    rows = client.get("/api/books", headers=auth).json()
    # 回落口径是 company_name / name，全 NULL 时就是这个顺序
    expect = [b["id"] for b in sorted(rows, key=lambda x: (x["company_name"], x["name"]))]
    assert got == expect


def test_只挪动一本_其余相对次序不变(client, auth, db, book, admin_user):
    _more_books(db, admin_user, 3, prefix="挪")
    before = _ids(client, auth)
    assert len(before) >= 4, "样本不够，看不出「其余」"
    moved, rest = before[2], before[:2] + before[3:]
    r = client.put("/api/books/order", headers=auth, json={"book_ids": [moved, *rest]})
    assert r.status_code == 200, r.text
    after = _ids(client, auth)
    assert sorted(after) == sorted(before), "挪动不该让账套凭空多一个少一个"
    assert after[0] == moved, "被挪的那本该到最前面"
    # 除了它，其余账套的相对先后必须原样保留 —— 拖一下就该只动一下
    assert [i for i in after if i != moved] == rest


def test_重复提交同一个顺序是幂等的(client, auth, db, book, admin_user):
    _more_books(db, admin_user, 2, prefix="幂")
    order = _ids(client, auth)
    for _ in range(3):
        r = client.put("/api/books/order", headers=auth, json={"book_ids": order})
        assert r.status_code == 200, r.text
    assert _ids(client, auth) == order


def test_序号稠密_不留空洞(client, auth, db, book, admin_user):
    """提交的子集也要能补齐：别人在这期间新建的账套要有位置。"""
    _more_books(db, admin_user, 2, prefix="稠")
    before = _ids(client, auth)
    client.put("/api/books/order", headers=auth, json={"book_ids": list(reversed(before))})

    later = _more_books(db, admin_user, 1, prefix="新来的")
    full = _ids(client, auth)
    # 新账套没有序号，沉在最后，而不是插到用户排好的布局中间
    assert full[-1] == later[0].id, "新账套不该插到已排好的磁贴中间"
    assert sorted(full) == sorted(before + [later[0].id])

    # 再排一次之后，序号必须是 0..n-1 的连续段 —— 有空洞的话，
    # 将来在中间插一本要重算全部，越攒越乱
    client.put("/api/books/order", headers=auth, json={"book_ids": full})
    links = db.scalars(select(UserBook).where(UserBook.user_id == _uid(db, auth, client))).all()
    orders = sorted(l.sort_order for l in links)
    assert orders == list(range(len(orders))), f"序号有空洞：{orders}"


def _uid(db, auth, client) -> int:
    from app.models import SysUser
    who = client.get("/api/auth/me", headers=auth).json()
    return db.scalar(select(SysUser.id).where(SysUser.username == who["username"]))


# ------------------------------------------------------------------ 只能排自己的


def test_别人的账套_id_一律拒掉(client, auth, db, book, admin_user, plain_user, plain_auth):
    """拿别人的 id 来排，是越权探测口，必须 400。"""
    alien = Book(company_name="别人公司", name="别人的账套", start_year=2026, start_month=1)
    db.add(alien)
    db.commit()
    db.refresh(alien)
    # 别人的账套，admin 用户自己并没有授权行
    assert db.scalar(select(UserBook).where(UserBook.user_id == admin_user.id,
                                            UserBook.book_id == alien.id)) is None

    r = client.put("/api/books/order", headers=auth,
                   json={"book_ids": [book.id, alien.id]})
    assert r.status_code == 400, r.text
    assert "不在你的列表" in r.text


def test_别人的顺序_你排不动(client, auth, db, book, admin_user, plain_user, plain_auth):
    """甲排过的顺序，乙看不到也改不动 —— 顺序是**每人一份**。

    这条钉的是加在 ``sys_user_book`` 而不是 ``book`` 上的那个决定：
    两本账套两人都有，乙看到的必须还是 company_name/name 的回落顺序。
    """
    from app.accounting.periods import create_periods

    b2 = Book(company_name="共享受理", name="乙的账套", start_year=2026, start_month=1)
    db.add(b2)
    db.flush()
    create_periods(db, b2)
    db.add(UserBook(user_id=admin_user.id, book_id=b2.id, role="OWNER"))
    # 乙对两本都有权（其中一本是只读），但都没有排过序
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.add(UserBook(user_id=plain_user.id, book_id=b2.id, role="ACCOUNTANT"))
    db.commit()

    mine = [b["id"] for b in client.get("/api/books", headers=auth).json()]
    flipped = list(reversed(mine))
    assert client.put("/api/books/order", headers=auth,
                      json={"book_ids": flipped}).status_code == 200
    assert _ids(client, auth) == flipped, "甲自己的顺序要真的生效"

    # 乙看到的仍是回落顺序：共享受理 < 测试公司，与甲怎么排无关
    other = [b["id"] for b in client.get("/api/books", headers=plain_auth).json()]
    assert set(other) == set(mine)
    assert other != flipped, "甲的顺序漏到了乙身上"
    # 而且甲那一行 sort_order 不会出现在乙的授权行上
    for link in db.scalars(select(UserBook).where(UserBook.user_id == plain_user.id)).all():
        assert link.sort_order is None, f"乙的授权行被写进了 sort_order={link.sort_order}"


# ------------------------------------------------------------------ 参数与边界


def test_重复_id_拒掉(client, auth, book):
    r = client.put("/api/books/order", headers=auth, json={"book_ids": [book.id, book.id]})
    assert r.status_code == 400, r.text
    assert "重复" in r.text


def test_空列表_等于不动(client, auth, db, book, admin_user):
    _more_books(db, admin_user, 1, prefix="空")
    before = _ids(client, auth)
    r = client.put("/api/books/order", headers=auth, json={"book_ids": []})
    assert r.status_code == 200, r.text
    assert _ids(client, auth) == before, "提交空列表不该把已有顺序清掉"


def test_没登录_401(client, book):
    r = client.put("/api/books/order", json={"book_ids": [book.id]})
    assert r.status_code == 401


def test_只读账号也能排自己那一列(client, db, book, plain_user, plain_auth):
    """个人视图偏好不该被写权限卡住 —— 它不碰任何账面数据。"""
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    r = client.put("/api/books/order", headers=plain_auth, json={"book_ids": [book.id]})
    assert r.status_code == 200, r.text
    assert _ids(client, plain_auth) == [book.id]


@pytest.mark.parametrize("body", [
    {},                              # 少了 book_ids
    {"ids": [1]},                    # 字段名写错
    {"book_ids": "1,2"},             # 该给数组
    {"book_ids": [1, 2] * 300},      # 一次几千个：那不是「排个序」
])
def test_参数不对_422(client, auth, body):
    r = client.put("/api/books/order", headers=auth, json=body)
    assert r.status_code == 422, r.text
