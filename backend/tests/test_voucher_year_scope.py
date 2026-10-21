"""凭证的**年度作用域**：一张凭证属于哪一年，这一页就得用哪一年的科目表。

**用户报的那件事**：在当前年度用关键词搜到以前年度的凭证，点开却看不到
凭证里的科目（2026-10-22）。

根因不在凭证数据上，而在「凭证页去取哪张科目表」。科目表每个会计年度
各有一份（迁移 0032），**同一个编码在两个年度是两个 id、两行记录**。
凭证页若按顶栏那一年去取科目表，往年凭证上的 ``subject_id`` 在新年度
那张表里**一条都对不上** —— 界面上按 id 找科目名，找不到就是空的，
而且没有任何报错（格子看上去只是「没填科目」）。

所以凭证必须**自己带着它的年度**出来（``year`` 字段），前端照它取表。
下面几条用例把这套口径钉住：

1. 凭证详情带出自己所属的会计年度
2. 跨年凭证的年度是**它自己**那一年的，不是顶栏那一年
3. **拿错年度就找得到不了一条科目**（这条是「看不到科目」的机制本身）
4. 列表接口逐张带出年度（列表要跨年检索，前端得据此取表）
5. 插入凭证的预览带出参照凭证那一年的年度（新凭证落在参照那一期里）
6. 停用科目照样在那张表里（老凭证指着它，格子里得有名字）
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import Period, Subject


def _chart_ids(db, book_id: int, year: int) -> set[int]:
    """某一年那张科目表里的 id 全集。"""
    return set(db.scalars(
        select(Subject.id).where(
            Subject.book_id == book_id, Subject.year == year)).all())


def _setup_next_year(db, book, year: int = 2027):
    """把账套开到下一年：12 个期间 + 一份**独立**的科目表。

    科目表是**复制**出来的（年度结转就是这么干的），所以两年的编码一样、
    id 全不一样 —— 那正是「拿错年度就什么都找不到」的由来。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    ensure_periods(db, book, year)
    copy_chart(db, book.id, book.start_year, year)
    db.commit()


# --------------------------------------------------------------------- 1. 详情带年度


def test_凭证详情带出自己所属的会计年度(client, auth, db, book, make_voucher):
    v = make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()

    r = client.get(f"/api/books/{book.id}/vouchers/{v.id}", headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["year"] == 2026, f"凭证详情没有带出年度：{d}"
    assert d["period_id"], d


# --------------------------------------------------------------------- 2. 跨年凭证


def test_跨年凭证的年度是它自己那一年的(client, auth, db, book, make_voucher):
    """2027 年的凭证说 2027 —— 不是账套的启用年度，也不是服务器日历年。"""
    _setup_next_year(db, book, 2027)
    v = make_voucher("2027-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()

    d = client.get(f"/api/books/{book.id}/vouchers/{v.id}", headers=auth).json()
    assert d["year"] == 2027, f"跨年凭证的年度报成了 {d.get('year')}"


# --------------------------------------------------------------------- 3. 机制本身


def test_拿错年度的科目表就找得到不了一条科目(db, book, make_voucher):
    """**这条是用户报的那个现象的机制本身。**

    两年的科目表编码相同、id 完全不同。于是「按顶栏那一年取表，再拿表里的
    id 去认往年凭证的科目」—— 一条都对不上，界面上的科目格就全是空的，
    而且**不报任何错**（那串 id 在表里确实不存在，只是没人提这件事）。

    所以凭证页必须按凭证自己那一年的表去取 —— 上一条用例钉的就是它。
    """
    _setup_next_year(db, book, 2027)
    v = make_voucher("2027-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()

    used = {e.subject_id for e in v.entries}
    assert used, "前置条件不成立：这张凭证没有分录"

    y26 = _chart_ids(db, book.id, 2026)
    y27 = _chart_ids(db, book.id, 2027)
    assert used <= y27, "2027 年的凭证应当挂在 2027 年那张表上"
    assert not (used & y26), (
        "2026 与 2027 的科目 id 竟然有交集 —— 那样的话「拿错年度」就复现不出来，"
        "这条用例失去意义")


# --------------------------------------------------------------------- 4. 列表


def test_列表逐张带出各自的年度(client, auth, db, book, make_voucher):
    """列表是跨年检索的入口（搜关键词找以前年度的凭证），每行都得说清自己是哪一年。"""
    _setup_next_year(db, book, 2027)
    a = make_voucher("2026-01-10", dr=[("1001", 100, "旧年")], cr=[("5001", 100, "x")])
    b = make_voucher("2027-01-10", dr=[("1001", 100, "新年")], cr=[("5001", 100, "x")])
    db.commit()

    r = client.get(f"/api/books/{book.id}/vouchers", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    years = {i["id"]: i["year"] for i in items}
    assert years[a.id] == 2026, f"2026 那张的年度报成了 {years[a.id]}"
    assert years[b.id] == 2027, f"2027 那张的年度报成了 {years[b.id]}"


# --------------------------------------------------------------------- 5. 插入位


def test_插入凭证的预览带出参照凭证那一年的年度(client, auth, db, book, make_voucher):
    """新凭证是**插在参照凭证那一期里**的（后端要求同一期间）。

    参照在往年时，这张新凭证要挂的科目属于**往年那张表** —— 预览里不带年度，
    前端就照顶栏那一年取表，录进去的科目挂在错的表上，保存时被服务端拒。
    """
    _setup_next_year(db, book, 2027)
    v = make_voucher("2027-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()

    r = client.get(f"/api/books/{book.id}/vouchers/insert-preview",
                   params={"after_id": v.id, "position": "before"}, headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["year"] == 2027, f"插入预览没有带出参照那一年：{d}"
    assert d["period_id"] == v.period_id, d


# --------------------------------------------------------------------- 6. 停用科目


def test_停用科目照样在那张表里_老凭证指着它(client, auth, db, book, subjects,
                                        make_voucher):
    """停用的语义是「不让再选它」，不是「删掉它」。

    老凭证还指着它，格子里就得有名字 —— 所以凭证页取表时要带
    ``include_inactive=true``，否则同一个症状（科目格是空的）又来了。
    """
    v = make_voucher("2026-01-10", dr=[("1002", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()
    s = subjects["1002"]
    s.active = False
    db.commit()

    r = client.get(f"/api/books/{book.id}/subjects",
                   params={"year": 2026, "include_inactive": True}, headers=auth)
    assert r.status_code == 200, r.text

    def _has(nodes):
        for n in nodes:
            if n["id"] == s.id:
                return True
            if _has(n.get("children") or []):
                return True
        return False

    assert _has(r.json()), "停用科目从科目表里消失了 —— 老凭证的科目格会变空"
    # 不带 include_inactive 时它确实不出现（停用就是不让再选）
    r2 = client.get(f"/api/books/{book.id}/subjects",
                    params={"year": 2026}, headers=auth)
    assert r2.status_code == 200, r2.text
    assert not _has(r2.json()), "停用科目本不该出现在可选项里"
