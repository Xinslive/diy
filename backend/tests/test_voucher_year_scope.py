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
7. **年度字段不许带来 N+1**（打印层一次序列化 5000 张，逐张查库就是 5000 条）
"""
from __future__ import annotations

from contextlib import contextmanager

import sqlalchemy
from sqlalchemy import select

from app.core.db import engine as app_engine
from app.models import Period, Subject


@contextmanager
def _statements():
    """收集本次调用期间真正发出去的 SQL（写法照 test_perf_accounting_paths.py）。"""
    stmts: list[str] = []
    handler = lambda conn, cursor, statement, params, ctx, many: stmts.append(statement)
    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        yield stmts
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)


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


# --------------------------------------------------------------------- 7. 年度不许带来 N+1


def _print_query_count(client, auth, book, period) -> int:
    """打印一个期间的凭证，看发出去多少条 SQL。"""
    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                       params={"period_id": period.id}, headers=auth)
    assert r.status_code == 200, r.text[:300]
    return len(stmts)


def test_序列化凭证时一个查询都不发(db, book, make_voucher):
    """**序列化函数自己不查库** —— 年度由调用方一次取好传进来。

    这条必须在一个**新会话**里测：同一个会话里 Period 早就在 identity map
    里了（建凭证时加载过），``db.get(Period, …)`` 命中缓存不发 SQL —— 那样
    测出来永远是 0 条，守卫就成了一句空话。换一个新会话，identity map 是空的，
    「调用方没提前取好」的情形才成立。

    与当初审计发现的严重 2 同一个形状：那里的科目没一次取好，满月的账就是
    5000 次同样的查询。
    """
    from app.api.vouchers import _book_subjects, _serialize
    from app.core.db import SessionLocal
    from app.models import Voucher

    v = make_voucher("2026-06-10", dr=[("1002", 100, "x")], cr=[("5001", 100, "x")])
    db.commit()

    with SessionLocal() as s:
        vv = s.get(Voucher, v.id)
        _ = vv.entries          # 分录先加载好，别把懒加载算进来
        subs = _book_subjects(s, book.id)
        with _statements() as stmts:
            out = _serialize(s, vv, subs)
        assert not stmts, (
            f"序列化一张凭证发了 {len(stmts)} 条查询：{stmts} —— "
            "年度要在序列化里查库，就是逐张一条的 N+1")
        assert "year" not in out, (
            "调用方没给期间索引时就不该有 year 这个键（不知道就干脆没有，"
            "不留一个 None 让人以为「这一张没有年度」）")


def test_打印一个期间的凭证时查询数不随凭证数增长(client, auth, db, book, periods,
                                                subjects, make_voucher):
    """**年度字段不许在序列化里逐张查库。**

    打印层一次最多序列化 5000 张凭证（``page_size=5000``），而 ``_serialize``
    是**逐张**调的 —— 它要是自己去按 id 查期间，满月的账就是 5000 条同样的
    SELECT，与当初审计发现的严重 2（科目没一次取好，满月的账 5000 次同样的
    查询）是同一个坑。

    修法：年度由调用方**一次取好**传进来（``_book_periods``），序列化函数里
    一个查询都不发。所以这里的断言是「凭证数翻十倍，查询数不跟着涨」。
    """
    period = periods[3]
    for i in range(2):
        make_voucher(f"2026-04-{10 + i:02d}", dr=[("1002", 100 + i, "x")],
                     cr=[("5001", 100 + i, "x")])
    db.commit()
    small = _print_query_count(client, auth, book, period)

    for i in range(18):
        make_voucher(f"2026-04-{12 + i:02d}", dr=[("1002", 200 + i, "x")],
                     cr=[("5001", 200 + i, "x")])
    db.commit()
    big = _print_query_count(client, auth, book, period)

    assert big - small <= 5, (
        f"凭证从 2 张涨到 20 张，查询数从 {small} 涨到 {big} —— "
        "序列化里在逐张查库（N+1）")


def test_凭证接口的年度查询不随凭证数增长(client, auth, db, book, periods,
                                          subjects, make_voucher):
    """列表接口同理：年度由**一次**取好的期间索引给出，不是逐张查。"""
    period = periods[4]
    for i in range(2):
        make_voucher(f"2026-05-{10 + i:02d}", dr=[("1002", 100 + i, "x")],
                     cr=[("5001", 100 + i, "x")])
    db.commit()
    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/vouchers", headers=auth)
    assert r.status_code == 200, r.text[:300]
    small = len(stmts)

    for i in range(18):
        make_voucher(f"2026-05-{12 + i:02d}", dr=[("1002", 200 + i, "x")],
                     cr=[("5001", 200 + i, "x")])
    db.commit()
    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/vouchers", headers=auth)
    assert r.status_code == 200, r.text[:300]
    big = len(stmts)

    assert big - small <= 3, (
        f"凭证从 2 张涨到 20 张，列表接口的查询数从 {small} 涨到 {big} —— "
        "年度在逐张查库（N+1）")


def test_建凭证与改凭证的回执也带年度(client, auth, db, book, subjects):
    """回执（POST/PUT 的响应）也要带年度 —— 前端建完凭证就地接着录，
    那时页面上用的科目表得是**这一张所属年度**那张。"""
    payload = {
        "voucher_date": "2026-07-15",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "100.00", "summary": "回执也要带年度"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "100.00", "summary": "回执也要带年度"},
        ],
        "remark": "", "attachments": 0,
    }
    r = client.post(f"/api/books/{book.id}/vouchers", json=payload, headers=auth)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert r.json()["year"] == 2026, f"建凭证的回执没有年度：{r.json()}"

    payload["voucher_date"] = "2026-07-16"
    r2 = client.put(f"/api/books/{book.id}/vouchers/{vid}", json=payload, headers=auth)
    assert r2.status_code == 200, r2.text
    assert r2.json()["year"] == 2026, f"改凭证的回执没有年度：{r2.json()}"


def test_拆分凭证时两张都带年度(client, auth, db, book, subjects):
    """拆出来的两张也要带年度 —— 它们各自还是那张凭证所属年度的凭证。

    分录要**借贷交替**：拆分要求前后两半各自平（这里在第 2 行后切），
    而 make_voucher 造出来的顺序是「先借后贷」，切不出两半都平的刀口。
    """
    entries = [
        {"subject_id": subjects["1002"].id, "direction": "DEBIT",
         "amount": "100.00", "summary": "拆"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "100.00", "summary": "拆"},
        {"subject_id": subjects["1002"].id, "direction": "DEBIT",
         "amount": "200.00", "summary": "拆"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": "200.00", "summary": "拆"},
    ]
    r = client.post(f"/api/books/{book.id}/vouchers",
                    json={"voucher_date": "2026-08-10", "entries": entries,
                          "remark": "", "attachments": 0}, headers=auth)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]

    r2 = client.post(f"/api/books/{book.id}/vouchers/{vid}/split",
                     json={"at_line": 2}, headers=auth)
    assert r2.status_code == 200, r2.text
    d = r2.json()
    assert d["original"]["year"] == 2026, d["original"]
    assert d["created"]["year"] == 2026, d["created"]
