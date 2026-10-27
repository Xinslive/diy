"""报表写接口的**请求体校验**与**门禁顺序**（审计 P3-10 / P3-13，报告 §3.10、§3.13）。

**§3.10：加行/加列的请求体没有类型 → 畸形字段回 500 而不是 422。**

``add_row`` / ``add_col`` 以前收的是**裸 ``dict``**，请求体里没有任何
字段级校验。于是 ``indent="abc"`` 一路走到 ``int(payload.get("indent", 0)
or 0)``，抛出一个没人接的 ``ValueError`` —— 而 ``app/main.py`` 只注册了
``DBAPIError`` 处理器，于是回一句**脱敏的 500**：明明是参数写错，却告诉
调用方「服务器坏了」。同一文件的 ``update_row`` / ``update_col`` 用带类型
的 Pydantic 模型，畸形字段回的是 422。

**§3.13：授权门在函数体里 → 只读账号发畸形请求体拿到的是 422 而不是 403。**

FastAPI 的顺序是「**先解依赖，再校验请求体**」。门禁写在函数体第一行时，
一个只读账号 + 一个畸形请求体，拿到的是 **422**（字段校验）而不是
**403**（授权）。这是一条**授权状态码探针**：调用方能靠状态码区分
「我被拒了」与「我请求体写错了」—— 它本来连自己有没有权限都不该知道。

仓库自订的标准写在 ``app/api/system.py:49-64``：「门禁进依赖图之后，
FastAPI 在**校验请求体之前**就会先求依赖」。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ReportCol, ReportRow, ReportTemplate, UserBook


@pytest.fixture
def tpl(client, auth, book):
    """一张归属 ``book`` 的报表模板（写接口只认归属当前账套的那份）。"""
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "入参校验测试表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def viewer(client, db, book, plain_user, plain_auth):
    """给 ``plain_user`` 一个**只读**授权（``VIEWER``）。"""
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    return plain_auth


# --------------------------------------------------------------------- §3.10


@pytest.mark.parametrize("payload", [
    {"row_no": "abc"},
    {"indent": "abc"},
    {"row_no": 1.5},
    {"indent": [1, 2]},
])
def test_加行的畸形字段回422而不是500(client, auth, book, tpl, payload):
    """``int("abc")`` 以前是个没人接的 ``ValueError`` → 脱敏的 500。"""
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=auth, json=payload)
    assert r.status_code == 422, (
        f"拿到 {r.status_code}（应当是 422）：畸形字段不该变成 500。"
        f"响应体 {r.text[:300]}")
    assert "detail" in r.json()


@pytest.mark.parametrize("payload", [
    {"col_no": "abc"},
    {"width": "abc"},
    {"col_no": 1.5},
    {"width": [1, 2]},
])
def test_加列的畸形字段回422而不是500(client, auth, book, tpl, payload):
    """同上：``int(payload.get("width") or 140)`` 以前会炸 500。"""
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}",
                    headers=auth, json=payload)
    assert r.status_code == 422, (
        f"拿到 {r.status_code}（应当是 422）：畸形字段不该变成 500。"
        f"响应体 {r.text[:300]}")
    assert "detail" in r.json()


def test_加行加列的合法请求仍然照常工作(client, auth, db, book, tpl):
    """**收紧校验不能把功能也堵了** —— 合法请求必须照样成功。"""
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": 2, "label": "合法新增行", "indent": 1})
    assert r.status_code == 200, r.text
    assert r.json()["row_no"] == 2

    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 9, "label": "合法新增列",
                                        "kind": "BALANCE_CLOSING", "width": 160})
    assert r.status_code == 201, r.text
    assert r.json()["col_no"] == 9

    db.expire_all()
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == tpl,
                                              ReportRow.row_no == 2))
    assert row is not None and row.label == "合法新增行"
    assert row.indent == 1
    col = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl,
                                              ReportCol.col_no == 9))
    assert col is not None and col.label == "合法新增列"
    assert col.kind == "BALANCE_CLOSING"
    assert col.width == 160


def test_加行加列的字段名与语义没有变(client, auth, db, book, tpl):
    """前端 ``ReportView.vue`` 只发 ``{row_no, label}`` / ``{col_no, label}``。

    字段名与默认值必须保持原样：``row_no`` 不给就插到最后一行之后，
    ``label`` 不给就是空标签，``kind`` 不给就沿用左边那一列的口径，
    ``width`` 不给就是 140。
    """
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": 3, "label": "只有行号和名字"})
    assert r.status_code == 200, r.text
    assert r.json()["row_no"] == 3

    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 9, "label": "只有列号和名字"})
    assert r.status_code == 201, r.text
    db.expire_all()
    col = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl,
                                              ReportCol.col_no == 9))
    assert col is not None
    assert col.width == 140, f"默认列宽变了：{col.width}"
    # 第 8 列是 BALANCE_OPENING（左边最近的数值列）→ 新列沿用它的口径。
    assert col.kind == "BALANCE_OPENING", f"默认口径变了：{col.kind}"


def test_加行的负数行号仍然是400(client, auth, book, tpl):
    """``row_no`` 合法但为负 —— 这是**业务**错误，仍然是 400（不是 422）。"""
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": -1})
    assert r.status_code == 400, f"拿到 {r.status_code}：{r.text[:200]}"


def test_加列的列号1仍然是400(client, auth, book, tpl):
    """第 1 列是固定的「项目」列 —— 插不进去，仍然是 400。"""
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 1})
    assert r.status_code == 400, f"拿到 {r.status_code}：{r.text[:200]}"


def test_加列的未知口径仍然是400(client, auth, book, tpl):
    """口径是**枚举**：不认识的值是业务错误，400 + 一句能照着改的话。"""
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 9, "kind": "NOT_A_KIND"})
    assert r.status_code == 400, f"拿到 {r.status_code}：{r.text[:200]}"
    assert "取数口径" in r.json()["detail"]


# --------------------------------------------------------------------- §3.13


def test_只读账号发畸形请求体拿到403而不是422(client, viewer, book, tpl):
    """**这条就是审计报告 §3.13 的复现。**

    门禁在依赖图里 → 403 先于请求体校验落地。
    """
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=viewer, json={"indent": "abc"})
    assert r.status_code == 403, (
        f"拿到 {r.status_code}：授权状态码变成了探针（应当 403）。"
        f"响应体 {r.text[:300]}")
    assert "422" not in str(r.status_code)


def test_只读账号发畸形列请求体拿到403而不是422(client, viewer, book, tpl):
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}",
                    headers=viewer, json={"width": "abc"})
    assert r.status_code == 403, (
        f"拿到 {r.status_code}：授权状态码变成了探针（应当 403）。"
        f"响应体 {r.text[:300]}")


def test_只读账号导入模板的畸形请求体拿到403而不是422(client, viewer, book):
    """``POST /api/reports/templates/import`` —— 审计报告点名的那条路径。"""
    r = client.post(f"/api/reports/templates/import?book_id={book.id}",
                    headers=viewer, json={})
    assert r.status_code == 403, (
        f"拿到 {r.status_code}：审计报告 §3.13 的复现路径（应当 403）。"
        f"响应体 {r.text[:300]}")


def test_只读账号预览导入的畸形请求体拿到403而不是422(client, viewer, book):
    """预览那条路由的请求体是 multipart 文件 —— 缺文件以前是 422。"""
    r = client.post(f"/api/reports/templates/import/preview?book_id={book.id}",
                    headers=viewer)
    assert r.status_code == 403, (
        f"拿到 {r.status_code}：审计报告 §3.13 的复现路径（应当 403）。"
        f"响应体 {r.text[:300]}")


def test_只读账号改公式的畸形请求体拿到403而不是422(client, viewer, book, tpl):
    """改公式那条路由的请求体是 ``list[CellIn]``，缺字段以前是 422。"""
    r = client.put(f"/api/reports/{tpl}/cells?book_id={book.id}", headers=viewer, json=[])
    assert r.status_code == 403, (
        f"拿到 {r.status_code}：授权状态码变成了探针（应当 403）。"
        f"响应体 {r.text[:300]}")


def test_合法请求体的只读账号仍然是403(client, viewer, book, tpl):
    """**顺序调了不能把语义调松**：合法请求体下，门禁的结论不变。"""
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=viewer, json={"row_no": 2, "label": "只读账号偷偷加的行"})
    assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:200]}"


def test_只读账号没有留下任何结构改动(client, viewer, db, book, tpl):
    """被拒的请求不该改动任何数据 —— 也不该产生审计。"""
    from app.models import AuditLog

    before = len(db.scalars(select(AuditLog).where(
        AuditLog.book_id == book.id)).all())
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}",
                    headers=viewer, json={"row_no": 2, "label": "偷偷加的行"})
    assert r.status_code == 403
    db.expire_all()
    rows = db.scalars(select(ReportRow).where(ReportRow.template_id == tpl)).all()
    assert not any(x.label == "偷偷加的行" for x in rows), "被拒的请求居然加了一行"
    after = len(db.scalars(select(AuditLog).where(
        AuditLog.book_id == book.id)).all())
    assert after == before, "被拒的请求留下了审计"


def _report_write_routes():
    """``/api/reports/`` 下的所有写路由（摊平 ``include_router`` 的包装层）。"""
    from fastapi.routing import APIRoute

    from app.main import app

    routes: list[APIRoute] = []
    seen: set[int] = set()
    stack = list(app.routes)
    while stack:
        r = stack.pop()
        if id(r) in seen:
            continue
        seen.add(id(r))
        if isinstance(r, APIRoute):
            routes.append(r)
            continue
        for attr in ("routes", "original_router"):
            inner = getattr(r, attr, None)
            inner = getattr(inner, "routes", None) if inner is not None else None
            stack.extend(inner or [])
    return [r for r in routes
            if r.path.startswith("/api/reports/")
            and (set(r.methods or ()) & {"POST", "PUT", "PATCH", "DELETE"})]


def _dep_calls(route) -> set:
    """这条路由**直接**声明的依赖函数集合（递归）。"""
    calls: set = set()

    def walk(dep) -> None:
        for d in dep.dependencies or []:
            if d.call is not None:
                calls.add(d.call)
            walk(d)

    walk(route.dependant)
    return calls


def test_门禁在依赖图里而不是只在函数体里():
    """静态守卫：门禁必须**挂在依赖图**上。

    只写在函数体里的门禁，FastAPI 会先校验请求体 —— 那正是 §3.13 的
    根因。这条断言看的是路由的**依赖表**（不是函数体），所以函数体里
    那句「第二层」留着也不会让这条变红。

    ``persist`` 走的是另一道门（``require_book_write``：把一次求值结果
    落库，不改口径），它不属于这一族 —— 与
    ``tests/test_viewer_matrix.py::test_报表写接口都过了它自己那道门``
    同一个口径。
    """
    from app.api.reports import require_owned_template, require_template_write
    from app.core.deps import require_book_write

    gated = {require_template_write, require_owned_template}
    missing = []
    for r in _report_write_routes():
        calls = _dep_calls(r)
        if calls & gated:
            continue
        if require_book_write in calls:
            continue
        missing.append(f"{sorted(set(r.methods))[0]:6} {r.path}")
    assert not missing, (
        "这些报表写接口没把门禁挂进依赖图 —— 请求体校验会先于授权落地，"
        "只读账号 + 畸形请求体拿到 422 而不是 403：\n" + "\n".join(missing))


def test_门禁依赖仍然挂在函数体里当第二层():
    """**门禁不能只挂依赖图。**

    函数体里那一句是第二层：有人在依赖注入上做了手脚时仍然有效，而且
    它让每条路由**自己**说清「这条要门禁」。仓库自订标准见
    ``app/api/system.py:49-64``（「函数体里的 ``_require_admin(user)``
    **保留**」）。
    """
    import ast
    import inspect
    import textwrap

    from app.core.deps import require_book_write

    missing = []
    for r in _report_write_routes():
        if require_book_write in _dep_calls(r):
            continue   # 走的是另一道门（require_book_write）
        try:
            source = textwrap.dedent(inspect.getsource(r.endpoint))
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            missing.append(f"{sorted(set(r.methods))[0]:6} {r.path}（读不出源码）")
            continue
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        if called & {"_require_template_write", "_require_owned_template"}:
            continue
        missing.append(f"{sorted(set(r.methods))[0]:6} {r.path}")
    assert not missing, (
        "这些报表写接口的函数体里没有门禁调用 —— 依赖图那一层可以被绕开：\n"
        + "\n".join(missing))
