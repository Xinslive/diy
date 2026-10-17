"""API 层性能整改的守卫（审计 2026-10-14：严重 1 / 严重 2 / 严重 5、中等 4）。

这一轮改的五件事，钉的是**结构与往返次数**，不是「跑得快」—— 压测跑一次就没有了，
而下面每一条都是「改回去就会红」的机械判据：

1. **两个上传端点必须是同步 ``def``。** 它们曾是全仓仅有的两个 ``async def``
   端点，而 ``async def`` 端点直接跑在事件循环线程上：同步 openpyxl 与同步 psycopg
   Session 一进去，这个 worker 上的所有请求一起排队，而 CPU 占用并不高
   （表现是「后端整体无响应」）。FastAPI 只会把同步 ``def`` 丢进线程池。
2. **打印导出不能每张凭证查一次科目。** P3-12 早在列表接口修过同一个坑
   （一次取好、逐张复用），打印层漏了，单请求最多 5000 条同样的 SELECT。
   这里用「真正发出去的 SQL」计数来钉，与 ``test_ledger_query_count.py`` 同一种写法。
3. **``neighbour`` 带筛选时不能翻页收齐整个结果集**，且返回体的
   ``prev_id / next_id / scope / index / total`` 必须与旧实现逐字一致
   —— ``_old_neighbour`` 是旧那段代码的逐字复刻，当契约基准用。
4. **一个写请求里 ``sys_user_book`` 只查一次。** 复用是**请求内**的：
   跨请求缓存会让「改密 / 停用 / 换角色」不能立刻生效，所以除了计数，
   还要有一条用例盯着「改完角色下一个请求就生效」。
5. **HTML 打印页要有行数上限**，且与 Excel 通道共用同一个常量与异常
   （``monkeypatch`` 的是 ``app.printing.excel.MAX_EXPORT_ROWS``：
   如果打印层自己另立了一个数字，这条会直接红）。

**为什么自带一个建凭证的小工具**（``mk_voucher``）而不用 ``conftest`` 的
``make_voucher``：这批用例要的是「接口层发几条 SQL、返回体长什么样」，
数据怎么进库与被测对象无关；而服务层的建凭证路径正由另一条整改线并行修改，
一旦那边中途改了快照逻辑，这批守卫就会跟着变红 —— 守的是 API 层，
就不该被别的层半途而废的改动绊倒。这里直接插 ORM 行（借贷配平由库里的
延迟约束触发器兜着），字段口径与 ``app/models/accounting.py`` 一致。
"""
from __future__ import annotations

import inspect
import io
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy
from fastapi.routing import APIRoute

#: 全仓唯一允许存在的异步端点。新增异步端点的人必须在这里显式豁免并写清理由，
#: 而不是让它默认混进来 —— ``async def`` 端点拿到的 ``db`` 是**同步** Session，
#: 在里面跑任何 DB 或文件解析都会原地堵死事件循环。
ALLOWED_ASYNC_ROUTES: set[str] = set()

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------- 小工具


def _all_routes() -> list[APIRoute]:
    """摊平 app 的路由表（``include_router`` 会多包一层）。"""
    from app.main import app

    out: list[APIRoute] = []
    seen: set[int] = set()
    stack = list(app.routes)
    while stack:
        r = stack.pop()
        if id(r) in seen:
            continue
        seen.add(id(r))
        if isinstance(r, APIRoute):
            out.append(r)
            continue
        for attr in ("routes", "original_router"):
            inner = getattr(r, attr, None)
            inner = getattr(inner, "routes", None) if inner is not None else None
            stack.extend(inner or [])
    return out


def _route(method: str, suffix: str) -> APIRoute:
    for r in _all_routes():
        if method in r.methods and r.path.endswith(suffix):
            return r
    raise AssertionError(f"没找到路由 {method} ...{suffix}")


@contextmanager
def _statements():
    """收集这段时间里真正发出去的 SQL。计数的是语句，不是「调用了几次」。"""
    stmts: list[str] = []
    handler = lambda conn, cursor, statement, params, ctx, many: stmts.append(statement)
    from app.core.db import engine as app_engine

    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        yield stmts
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)


def _count(stmts: list[str], needle: str) -> int:
    return sum(1 for s in stmts if needle in s)


def _xlsx(header: list, rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _voucher_xlsx(n: int) -> bytes:
    """n 张配平凭证，每张两行。"""
    rows = []
    for i in range(n):
        rows.append([f"2026-01-{i + 1:02d}", f"记-{i}", f"收租金 {i}", "1002", 100, None])
        rows.append([None, None, f"收租金 {i}", "5001", None, 100])
    return _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], rows)


def _upload_import(client, auth, book, data: bytes, name="导入.xlsx"):
    return client.post(f"/api/books/{book.id}/vouchers/import", headers=auth,
                       files={"file": (name, data, XLSX_MIME)})


def _upload_openings(client, auth, book, data: bytes, name="期初.xlsx"):
    return client.post(f"/api/books/{book.id}/openings/parse", headers=auth,
                       files={"file": (name, data, XLSX_MIME)})


@pytest.fixture
def mk_voucher(db, book):
    """建凭证的小工厂（直接插 ORM 行，见模块 docstring）。"""
    from sqlalchemy import func, select

    from app.models import Entry, Period, Subject, Voucher

    def _mk(day: str, *, summary: str = "x", amount=100,
            dr: str = "1002", cr: str = "5001", remark: str = ""):
        d = date.fromisoformat(day)
        period = db.scalar(select(Period).where(
            Period.book_id == book.id, Period.year == d.year, Period.month == d.month))
        assert period is not None, f"这个账套没有 {d.year}-{d.month} 期间"
        seq = (db.scalar(select(func.count()).select_from(Voucher)
                         .where(Voucher.period_id == period.id)) or 0) + 1
        v = Voucher(book_id=book.id, period_id=period.id, voucher_no=f"记-{seq:03d}",
                    voucher_date=d, status="UNPOSTED", source="MANUAL",
                    remark=remark or summary, attachments=0)
        db.add(v)
        db.flush()
        codes = dict(db.execute(
            select(Subject.code, Subject.id).where(Subject.book_id == book.id)).all())
        for line_no, (code, direction) in enumerate(
                [(dr, "DEBIT"), (cr, "CREDIT")], start=1):
            db.add(Entry(voucher_id=v.id, line_no=line_no, subject_id=codes[code],
                         direction=direction, amount=Decimal(str(amount)), summary=summary))
        db.commit()          # 借贷配平由库里的延迟约束触发器在提交时把关
        db.refresh(v)
        return v

    return _mk


# ===================================================================== 严重 1
# 两个上传端点改同步：结构断言 + 功能不许退化


def test_上传入口不再是异步端点():
    """``async def`` 端点会把同步 openpyxl / 同步 Session 跑在事件循环线程上。"""
    for suffix in ("/vouchers/import", "/openings/parse"):
        endpoint = _route("POST", suffix).endpoint
        assert not inspect.iscoroutinefunction(endpoint), (
            f"{suffix} 又变回 async def 了：事件循环会被同步的 openpyxl 与同步 "
            "Session 堵死，一个 20MB 导入能让整个 worker 无响应")


def test_全仓没有漏进来的异步端点():
    others = [f"{sorted(r.methods)[0]} {r.path}" for r in _all_routes()
              if inspect.iscoroutinefunction(r.endpoint)
              and f"{sorted(r.methods)[0]} {r.path}" not in ALLOWED_ASYNC_ROUTES]
    assert not others, (
        "这些端点是 async def，而它们拿到的 db 是同步 Session：\n"
        + "\n".join(others)
        + "\n改成同步 def（FastAPI 会丢进线程池），或者确实需要异步时，"
          "把路径加进 ALLOWED_ASYNC_ROUTES 并在这里写清理由。")


def test_凭证导入改同步后仍然能导入(client, auth, book):
    r = _upload_import(client, auth, book, _voucher_xlsx(3))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3, body
    assert body["ok_count"] == 3, body
    assert body["errors"] == [], body
    assert len(body["vouchers"]) == 3, body


def test_凭证导入暂存批次进得了历史列表(client, auth, book):
    """端点只改了同步性，「暂存待确认」的契约不能因此断掉。"""
    r = _upload_import(client, auth, book, _voucher_xlsx(2))
    assert r.status_code == 200, r.text
    batch_id = r.json()["batch_id"]

    items = client.get(f"/api/books/{book.id}/vouchers/import", headers=auth).json()
    assert batch_id in [i["id"] for i in items], items


def test_期初解析改同步后仍然能解析(client, auth, book):
    data = _xlsx(["科目编码", "科目名称", "方向", "期初余额"],
                 [["1002", "银行存款", "借", 100],
                  ["5001", "主营业务收入", "贷", 100]])
    r = _upload_openings(client, auth, book, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 2, body
    assert body["balanced"] is True, body


def test_同步版容量闸没松_凭证导入超限仍413(client, auth, book, monkeypatch):
    """改同步不是为了绕过闸：``size`` 预检与 +1 超限检测都还在。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_bytes", 2048, raising=False)
    data = _voucher_xlsx(30)
    assert len(data) > 2048, "前置：这份表要超过上限"

    r = _upload_import(client, auth, book, data)
    assert r.status_code == 413, f"超限上传被放行了：{r.status_code} {r.text}"
    assert "上限" in r.json()["detail"]


def test_同步版容量闸没松_期初解析超限仍413(client, auth, book, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_bytes", 2048, raising=False)
    data = _xlsx(["科目编码", "科目名称", "方向", "期初余额"],
                 [["1002", "银行存款", "借", 100]] * 40)
    assert len(data) > 2048, "前置：这份表要超过上限"

    r = _upload_openings(client, auth, book, data)
    assert r.status_code == 413, f"超限上传被放行了：{r.status_code} {r.text}"


def test_期初解析超行数上限仍413(client, auth, book, monkeypatch):
    """第二、三道闸（解压体积、行数）改同步后也没松。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_rows", 10, raising=False)
    r = _upload_openings(client, auth, book,
                         _xlsx(["科目编码", "科目名称", "方向", "期初余额"],
                               [["1002", "银行存款", "借", 100]] * 40))
    assert r.status_code == 413, f"超行数的表被放行了：{r.status_code} {r.text}"
    assert "行" in r.json()["detail"]


def test_异步读入口仍在且与同步版同一把尺():
    """公共工具不能因为端点改同步就被删掉：异步签名保留，闸是同一份实现。"""
    from app.core import uploads

    assert inspect.iscoroutinefunction(uploads.read_upload), \
        "uploads.read_upload 的 async 契约不能动（accounting.importer 在转出它）"
    assert not inspect.iscoroutinefunction(uploads.read_upload_sync)

    class _Fake:
        size = 8
        file = io.BytesIO(b"12345678")

    assert uploads.read_upload_sync(_Fake()) == b"12345678"


# ===================================================================== 严重 2
# 打印导出的 N+1


def test_凭证打印页不按凭证张数查科目(client, auth, book, periods, mk_voucher):
    """8 张凭证 ⇒ 科目只查一次（原来每张一条：5000 张就是 5000 条）。"""
    for i in range(8):
        mk_voucher(f"2026-01-{i + 1:02d}", amount=100 + i)

    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                       params={"period_id": periods[1].id}, headers=auth)

    assert r.status_code == 200, r.text
    assert _count(stmts, "FROM subject") <= 2, (
        f"科目表被查了 {_count(stmts, 'FROM subject')} 次（8 张凭证），"
        "打印层又在逐张查科目了：\n"
        + "\n".join(s for s in stmts if "FROM subject" in s))


def test_凭证xlsx导出不按凭证张数查科目(client, auth, book, periods, mk_voucher):
    for i in range(8):
        mk_voucher(f"2026-01-{i + 1:02d}", amount=100 + i)

    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/print/vouchers.xlsx",
                       params={"period_id": periods[1].id}, headers=auth)

    assert r.status_code == 200, r.text
    assert _count(stmts, "FROM subject") <= 2, (
        f"导出时科目表被查了 {_count(stmts, 'FROM subject')} 次：\n"
        + "\n".join(s for s in stmts if "FROM subject" in s))


def test_打印页的科目名称仍然填得上(client, auth, book, periods, mk_voucher):
    """复用 ``subs`` 不能把序列化内容改掉 —— 科目要照旧出现在页面上。

    这里断言的是**科目名称**，不是编码：科目列按需求改成只印
    「一级/二级/.../末级」这条路径，不再印编码（见 _voucher_table_html）。
    这条用例真正守的是「科目数据仍然从复用出来的 subs 里取到了」，
    守的是数据来源，不是印出来的具体字样。
    """
    mk_voucher("2026-01-05")
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id}, headers=auth)
    assert r.status_code == 200, r.text
    assert "银行存款" in r.text, r.text[:2000]


# ===================================================================== 严重 5
# neighbour：不为「前后各一张」收齐整个结果集


def _old_neighbour(db, book_id: int, voucher_id: int, **filters) -> dict:
    """**旧实现逐字复刻**，当返回体契约的基准用。

    它就是被改掉的那段代码：按 200 一页把筛选结果翻到收齐 → reverse → index。
    """
    from app.accounting import vouchers as svc

    ids: list[int] = []
    total = 0
    for page in range(1, 101):
        rows, total = svc.query_vouchers(db, book_id, page=page, page_size=200, **filters)
        ids.extend(r.id for r in rows)
        if len(ids) >= total or not rows:
            break
    ids.reverse()
    try:
        i = ids.index(voucher_id)
    except ValueError:
        return {"prev_id": None, "next_id": None, "scope": "filtered",
                "index": -1, "total": len(ids)}
    return {"prev_id": ids[i - 1] if i > 0 else None,
            "next_id": ids[i + 1] if i < len(ids) - 1 else None,
            "scope": "filtered", "index": i, "total": len(ids)}


def test_筛选路径的返回体与旧实现逐字一致(client, db, auth, book, mk_voucher):
    a = mk_voucher("2026-05-31", summary="16 a")
    b = mk_voucher("2026-06-30", summary="16 b")
    c = mk_voucher("2026-07-31", summary="16 c")
    mk_voucher("2026-06-15", summary="别的")          # 干扰项，筛选时本该被排掉

    for v in (a, b, c):
        got = client.get(f"/api/books/{book.id}/vouchers/{v.id}/neighbour",
                         params={"keyword": "16"}, headers=auth)
        assert got.status_code == 200, got.text
        want = _old_neighbour(db, book.id, v.id, keyword="16")
        assert got.json() == want, f"返回体变了：{got.json()} != {want}"


def test_金额筛选的返回体与旧实现逐字一致(client, db, auth, book, mk_voucher):
    small = mk_voucher("2026-05-31", amount=100)
    mid = mk_voucher("2026-06-30", amount=500)
    big = mk_voucher("2026-07-31", amount=900000)

    for v in (small, mid, big):
        got = client.get(f"/api/books/{book.id}/vouchers/{v.id}/neighbour",
                         params={"min_amount": "500", "max_amount": "1000"}, headers=auth)
        assert got.status_code == 200, got.text
        want = _old_neighbour(db, book.id, v.id, min_amount=500, max_amount=1000)
        assert got.json() == want, f"返回体变了：{got.json()} != {want}"


def test_筛选路径不再翻页收齐结果集(client, auth, book, mk_voucher, monkeypatch):
    """结构性判据：带筛选的分支**一次都不许**调 ``query_vouchers``。

    旧实现靠它按 200 一页把结果集翻到收齐（最多 100 轮、约 200 条 SQL，
    两万个 ORM 对象进 identity map，只为执行一次 ``ids.index()``）。
    """
    from app.accounting import vouchers as svc

    a = mk_voucher("2026-05-31", summary="16 a")
    b = mk_voucher("2026-06-30", summary="16 b")

    def _boom(*_args, **_kwargs):
        raise AssertionError("带筛选的 neighbour 又去翻页收齐整个结果集了")

    monkeypatch.setattr(svc, "query_vouchers", _boom)
    r = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour",
                   params={"keyword": "16"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"prev_id": None, "next_id": b.id, "scope": "filtered",
                        "index": 0, "total": 2}


def test_筛选路径的SQL条数是常数(client, auth, book, mk_voucher):
    """一次取凭证 + 一次条件聚合 + 前后各一条 = 4 条业务查询，与结果集大小无关。

    连同依赖解析（取当前用户、取授权、取账套）一共 7 条。旧实现在同样场景下
    是「3 + 每页 2 条」，页数随结果集线性增长 —— 100 页封顶就是 200 条。
    """
    for i in range(6):
        mk_voucher(f"2026-0{i + 1}-15", summary=f"16 {i}")
    target = mk_voucher("2026-06-20", summary="16 收尾")

    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/vouchers/{target.id}/neighbour",
                       params={"keyword": "16"}, headers=auth)
    assert r.status_code == 200, r.text
    business = [s for s in stmts
                if "voucher" in s or "entry" in s]
    assert len(business) <= 4, (
        f"一次「前一张/后一张」发了 {len(business)} 条业务查询：\n" + "\n".join(business))


def test_不在结果集里返回负一索引(client, auth, book, mk_voucher):
    """这张被当前筛选条件挡在外面 —— 不能瞎指一张邻票。"""
    mk_voucher("2026-05-31", summary="16")
    gone = mk_voucher("2026-06-30", summary="别的")

    r = client.get(f"/api/books/{book.id}/vouchers/{gone.id}/neighbour",
                   params={"keyword": "16"}, headers=auth).json()
    assert r == {"prev_id": None, "next_id": None, "scope": "filtered",
                 "index": -1, "total": 1}, r


def test_结果集第一张的index是零不是负一(client, auth, book, mk_voucher):
    """第一张 prev=None，但它**在**结果集里 —— index 必须是 0 而不是 -1。

    这正是「不在结果集里」不能拿「前后都查不到」来判断的原因。
    """
    a = mk_voucher("2026-05-31", summary="16 a")
    b = mk_voucher("2026-06-30", summary="16 b")

    r = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour",
                   params={"keyword": "16"}, headers=auth).json()
    assert (r["prev_id"], r["next_id"], r["index"], r["total"]) == (None, b.id, 0, 2), r


def test_筛选条件与列表口径一致(client, db, auth, book, mk_voucher):
    """翻的是「列表筛出来的那个集合」：``total`` 必须等于列表的 count。"""
    from app.accounting import vouchers as svc

    for i in range(4):
        mk_voucher(f"2026-0{i + 1}-20", summary=f"16 {i}")
    mk_voucher("2026-05-11", summary="不含关键字")
    v = mk_voucher("2026-06-20", summary="16 收尾")

    got = client.get(f"/api/books/{book.id}/vouchers/{v.id}/neighbour",
                     params={"keyword": "16"}, headers=auth).json()
    _rows, list_total = svc.query_vouchers(db, book.id, keyword="16", page_size=1)
    assert got["total"] == list_total == 5, (got, list_total)


def test_无筛选时仍按同期间翻且不带索引(client, auth, book, mk_voucher):
    a = mk_voucher("2026-01-05")
    b = mk_voucher("2026-01-10")
    mk_voucher("2026-02-03")

    r = client.get(f"/api/books/{book.id}/vouchers/{a.id}/neighbour", headers=auth).json()
    assert r == {"prev_id": None, "next_id": b.id, "scope": "period",
                 "index": None, "total": None}, r


def test_筛选路径的index按日期排序而不是凭证号(client, auth, book, mk_voucher):
    """倒着录凭证（先录日期靠后的）时，index 仍按**时间正序**算。

    旧实现复用了 ``query_vouchers`` 的默认排序（期间 + 凭证号倒序）再 reverse，
    于是同期间里「后录的凭证号更大」会把日期顺序整个倒过来，与 docstring 写的
    「两种情况下顺序都是时间正序」、也与不带筛选那条路（``tuple_(date, id)``）
    对不上。现在两条路是同一个口径。
    """
    late = mk_voucher("2026-01-20", summary="16 先录的")   # 凭证号 记001
    early = mk_voucher("2026-01-05", summary="16 后录的")  # 凭证号 记002

    first = client.get(f"/api/books/{book.id}/vouchers/{early.id}/neighbour",
                       params={"keyword": "16"}, headers=auth).json()
    assert (first["prev_id"], first["next_id"], first["index"]) == (None, late.id, 0), first
    second = client.get(f"/api/books/{book.id}/vouchers/{late.id}/neighbour",
                        params={"keyword": "16"}, headers=auth).json()
    assert (second["prev_id"], second["next_id"], second["index"]) == (early.id, None, 1), second


# ===================================================================== 中等 4
# 写请求的授权查询：请求内复用，不是跨请求缓存


def test_一个写请求只查一次账套授权(client, auth, book):
    """``get_my_book`` 与 ``require_book_write`` 发的是同一条查询，别发两次。"""
    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/vouchers/period-check", headers=auth)
    assert r.status_code == 200, r.text
    assert _count(stmts, "FROM sys_user_book") == 1, (
        f"一个请求里查了 {_count(stmts, 'FROM sys_user_book')} 次账套授权：\n"
        + "\n".join(s for s in stmts if "FROM sys_user_book" in s))


def test_模板写接口只查一次账套授权(client, auth, book):
    """打印模板那几个写接口挂了三个依赖（get_my_book + get_book_link + require_book_write），
    原来一个请求查三遍。"""
    with _statements() as stmts:
        r = client.get(f"/api/books/{book.id}/print/templates", headers=auth)
    assert r.status_code == 200, r.text
    assert _count(stmts, "FROM sys_user_book") == 1, (
        f"查了 {_count(stmts, 'FROM sys_user_book')} 次：\n"
        + "\n".join(s for s in stmts if "FROM sys_user_book" in s))


def test_改了角色下一个请求立刻生效(client, db, book, plain_user, plain_auth):
    """复用必须**只在请求内**。

    跨请求缓存过的最危险后果：管理员把只读账号提成会计，对方仍然要等缓存过期
    才能写 —— 而「停用 / 改密 / 换角色必须立刻生效」是这个系统的硬要求。
    """
    from app.models import UserBook

    link = UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER")
    db.add(link)
    db.commit()

    url = f"/api/books/{book.id}/vouchers/period-check"
    assert client.get(url, headers=plain_auth).status_code == 403

    link.role = "OWNER"
    db.commit()
    assert client.get(url, headers=plain_auth).status_code == 200, \
        "改了角色之后请求没有重新查授权 —— 这是跨请求缓存"

    link.role = "VIEWER"
    db.commit()
    assert client.get(url, headers=plain_auth).status_code == 403


def test_没有账套授权仍然是403(client, auth, db, book):
    """共用解析逻辑不能把「没有授权」这条路径放行。"""
    from app.models import Book

    other = Book(company_name="别家公司", name="别人的账套",
                 start_year=2026, start_month=1)
    db.add(other)
    db.commit()
    db.refresh(other)

    r = client.get(f"/api/books/{other.id}/vouchers", headers=auth)
    assert r.status_code == 403, r.text


def test_VIEWER仍然不能写(client, db, book, plain_user, plain_auth):
    """共用解析逻辑不能把写门禁绕过去：角色判定拿的是同一条 link。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()

    r = client.get(f"/api/books/{book.id}/vouchers/period-check", headers=plain_auth)
    assert r.status_code == 403, r.text


# ===================================================================== HTML 打印行数上限


def test_余额表打印超上限返回413(client, auth, book, periods, monkeypatch):
    from app.printing import excel as xl

    monkeypatch.setattr(xl, "MAX_EXPORT_ROWS", 1)
    r = client.get(f"/api/books/{book.id}/print/balance.html",
                   params={"period_id": periods[1].id}, headers=auth)
    assert r.status_code == 413, f"超行数的打印页被放行了：{r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "超过单次打印上限" in detail, detail
    assert f"{xl.MAX_EXPORT_ROWS:,}" in detail, detail


def test_总账打印超上限返回413(client, auth, book, periods, mk_voucher, monkeypatch):
    from app.printing import excel as xl

    mk_voucher("2026-01-05")
    monkeypatch.setattr(xl, "MAX_EXPORT_ROWS", 1)
    r = client.get(f"/api/books/{book.id}/print/general-ledger.html",
                   params={"period_id": periods[1].id}, headers=auth)
    assert r.status_code == 413, f"超行数的打印页被放行了：{r.status_code} {r.text}"


def test_明细账打印超上限返回413(client, auth, book, periods, subjects,
                                mk_voucher, monkeypatch):
    from app.printing import excel as xl

    for i in range(3):
        mk_voucher(f"2026-01-0{i + 5}")
    monkeypatch.setattr(xl, "MAX_EXPORT_ROWS", 1)
    r = client.get(f"/api/books/{book.id}/print/detail-ledger.html",
                   params={"period_id": periods[1].id,
                           "subject_id": subjects["1002"].id}, headers=auth)
    assert r.status_code == 413, f"超行数的打印页被放行了：{r.status_code} {r.text}"


def test_正常规模的打印页不被误伤(client, auth, book, periods, mk_voucher):
    """上限取自 Excel 通道那个常量（50000），而不是另立一个小数字。"""
    mk_voucher("2026-01-05")
    r = client.get(f"/api/books/{book.id}/print/balance.html",
                   params={"period_id": periods[1].id}, headers=auth)
    assert r.status_code == 200, r.text
    assert "<html" in r.text.lower()


def test_预览通道的413不被改写成422(client, auth, book, periods, monkeypatch):
    """设计器预览会捕获 HTTPException 转成 422 —— 行数超限必须原样放行。"""
    from app.printing import excel as xl

    monkeypatch.setattr(xl, "MAX_EXPORT_ROWS", 1)
    r = client.post(f"/api/books/{book.id}/print/preview", headers=auth,
                    json={"scope": "BALANCE", "content": {"title": {"text": "草稿"}},
                          "period_id": periods[1].id})
    assert r.status_code == 413, f"预览把 413 改写成了 {r.status_code}：{r.text}"
    assert "超过单次打印上限" in r.json()["detail"]


def test_预览通道的参数错误仍然是422(client, auth, book):
    """加行数闸不能顺手把预览原有的报错口径改了。"""
    r = client.post(f"/api/books/{book.id}/print/preview", headers=auth,
                    json={"scope": "BALANCE", "content": {"columns": []}})
    assert r.status_code == 422, r.text
