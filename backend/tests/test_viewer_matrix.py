"""VIEWER 写权限全量矩阵（P1-02）。

审计报告 P1-02：``close_period`` / ``unclose_period`` / ``year_end`` /
``set_openings`` 只挂了 ``get_my_book``（查「有没有授权」，不查角色），
于是只读账号能封账、解封账、年度结转、篡改期初 —— 动态复现 HTTP 200。

这类问题的麻烦不在修一条，而在**保证以后不再漏一条**。所以这里写两条：

1. ``test_每个账套写接口都挂了写依赖`` —— 遍历 FastAPI 路由表，逐个检查写接口
   有没有挂 ``require_book_write``。以后新增写接口忘了挂，这条直接红。
   它守的是「别忘了加」。
2. ``test_VIEWER_调用每个账套写接口都被拒`` —— 真发 HTTP 请求，用 VIEWER 把
   矩阵挨个打一遍，断言全是 403。它守的是「加了还得真的管用」。

单点断言（「结账返回 403」）只覆盖那一条；这两条一起才是防线。
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.core.deps import require_book_write

# 不属于「账套写权限」范畴的写接口，各有各的门禁，单独看待：
#   /api/auth/*         登录注册，本来就对匿名开放
#   /api/system/*       管理员专属（require_admin）
#   POST /api/books     建自己的账套，建完自动 OWNER
#   .../print/preview   只渲染 HTML 不落库，只读账号本来就该能预览
#     （见 tests/test_print_template.py::test_只读账套也能预览）
#   .../openings/parse  只解析成 preview 返回，不落库；落库走 set_openings
#   .../organize/preview  只算计划不执行，执行走 /organize
#
# **豁免的是「写权限」，不是「别的所有维度」。** 清单里的两条 parse/preview
# 接口曾经只因为「不落库」就被整体放行 —— 于是它们的**容量**维度没人管：
# ``openings/parse`` 是全项目唯一「一个请求能把进程内存吃光」的入口
# （``raw = await file.read()`` 无上限 + 整个工作表一次性物化成二维列表），
# 而只读账号就能打（审计 P1-4）。
#
# 所以上传入口的容量守卫**另立一条**，按容量维度覆盖全部 UploadFile 路由，
# 与这份写权限清单互不代替：``tests/test_upload_capacity_matrix.py``。
_EXEMPT_EXACT = {
    "/api/books",
    "/api/books/{book_id}/print/preview",
    "/api/books/{book_id}/openings/parse",
    "/api/books/{book_id}/vouchers/organize/preview",
    # 账套磁贴的**个人**排序。写的是「我这个人的磁贴怎么排」，
    # 不碰凭证、科目、期初，也影响不到别的用户 —— 只读同事打开自己的
    # 账套列表，同样有权把它排顺眼。挂 require_book_write 反而是错的。
    # 边界由 tests/test_book_order.py 单独钉：它只能排自己的账套，
    # 别人的 id 一律 400。
    "/api/books/order",
}
_EXEMPT_PREFIXES = ("/api/auth/", "/api/system/")
# MCP 密钥的自助面（/api/mcp-tokens）：写的是**本人名下的凭据行**
# （mcp_token），**不碰任何一本账的数据** —— 与 /api/auth/me（改自己的
# 姓名与口令）是同一类：个人资源，不动别的账。
#
# **为什么只读账号也能签**：那枚密钥能读什么，100% 由 `sys_user_book`
# 决定（见 app/mcp/auth.py）—— 只读账号铸出来的密钥，读到的**仍然只有
# 它本来就能读的那些账**，不会多一分。而且它的生命周期跟着账号走：
# 撤授权，下一次调用就被挡；停用账号，令牌立刻作废。
#
# 所以挂 `require_book_write` 反而是错的：那是「能不能改这本账」的判据，
# 而这里根本不改账。边界由 tests/test_mcp_self_service.py 单独钉 ——
# 那里钉的是「只能动自己的」「只读账号铸出来的密钥只看得见它该看得见的」。
_EXEMPT_PREFIXES = _EXEMPT_PREFIXES + ("/api/mcp-tokens",)
_REPORTS_PREFIX = "/api/reports/"
# 报表写接口走的是**另一道更严的门**：``reports._require_template_write``
# 要求「显式给出 book_id」且「该账套角色是 OWNER」，比 require_book_write
# （OWNER + ACCOUNTANT）更严。所以它整段豁免出上面那张矩阵 ——
# 但**豁免的是"写权限"，不是"门禁本身"**，见下面那条静态断言。
_REPORTS_GATES = frozenset({"_require_template_write", "_require_owned_template"})


def _all_routes() -> list[APIRoute]:
    """摊平 app 的路由表。

    注意 ``include_router`` 放进来的不是裸 APIRoute，而是一层 ``_IncludedRouter``
    包装（FastAPI 新版本才有的内部结构）。只认 ``.routes`` 会一条写接口都遍历不到 ——
    那时这条用例会「通过」，但其实什么都没检查，是最坏的那种假绿。
    这里两种形态都认。
    """
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


def _dep_calls(route: APIRoute) -> set:
    """这条路由**直接**声明的依赖函数集合。"""
    calls: set = set()

    def walk(dep) -> None:
        for d in dep.dependencies or []:
            if d.call is not None:
                calls.add(d.call)
            walk(d)

    walk(route.dependant)
    return calls


def test_每个账套写接口都挂了写依赖():
    """新增写接口忘了挂 require_book_write —— 这条用例会红。"""
    missing = []
    for r in _all_routes():
        if not (set(r.methods or ()) & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        path = r.path
        if path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIXES) \
                or path.startswith(_REPORTS_PREFIX):
            continue
        if require_book_write not in _dep_calls(r):
            missing.append(f"{sorted(set(r.methods))[0]:6} {path}")
    assert not missing, (
        "这些写接口没挂 require_book_write，VIEWER 能直接调用：\n" + "\n".join(missing))


def _gate_calls(route) -> set:
    """这条路由的**函数体里**调用了哪些宿主函数名。

    用 AST 而不是正则：正则会把注释里的名字也算进去，而注释里提到一个
    门禁函数名再正常不过 —— 那不是"这个路由有门禁"的证据。

    读不出来（源码不可得 / 语法异常）时返回空集，于是那条断言会红 ——
    **宁可误红，也不要静默放过**。
    """
    import ast
    import inspect
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(route.endpoint))
    except (OSError, TypeError):
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    called: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


def test_报表写接口都过了它自己那道门():
    """报表写接口必须调用 ``_require_template_write`` / ``_require_owned_template``
    或挂 ``require_book_write``（审查报告 §6 的结构性缺口）。

    **为什么这条要单独写。** 报表那一整段前缀被豁免出上面那张矩阵，
    理由是"报表走自己那道门"。但那条理由**没有任何测试在守**：
    15 个报表写路由里只有 3 个在动态矩阵的用例列表里（persist / cells /
    templates），其余 11 个（fork / import / copy / rows 增改删 / cols 增改删）
    **既不在静态矩阵里，也没有行为用例**。

    于是"给 reports.py 新增一个写路由、忘了调门禁"这件事**没有任何测试会红** ——
    这正是 ``core/deps.py:194-201`` 记录的那次历史漏挂（close_period /
    unclose_period / year_end / set_openings 曾只挂 get_my_book）的同一种形态，
    只是换了个文件。

    这条断言守的是「别忘了加」，与上面那条守同一个性质。
    """
    missing = []
    for r in _all_routes():
        if not (set(r.methods or ()) & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        if not r.path.startswith(_REPORTS_PREFIX):
            continue
        if require_book_write in _dep_calls(r):
            continue
        if _gate_calls(r) & _REPORTS_GATES:
            continue
        missing.append(f"{sorted(set(r.methods))[0]:6} {r.path}")
    assert not missing, (
        "这些报表写接口没调用 _require_template_write / _require_owned_template，"
        "也没挂 require_book_write —— 任何登录用户都能改报表口径：\n"
        + "\n".join(missing))


def _make_voucher_as(client, headers) -> tuple[int, int]:
    """以管理员身份建一张凭证，返回 (凭证 id, 账套 id)。

    凭证必须由**能写的人**造：VIEWER 自己建凭证本来就被 403 挡着。
    矩阵里那些 ``{voucher_id}`` 路径参数需要真实存在、能被解析出来的 id，
    否则 404 会把 403 的断言糊弄过去 —— 测的就不是权限了。
    """
    r = client.post("/api/books", headers=headers,
                    json={"company_name": "矩阵测试公司", "name": "矩阵测试账套",
                          "start_year": 2026})
    assert r.status_code == 201, r.text
    book_id = r.json()["id"]

    subs = client.get(f"/api/books/{book_id}/subjects/flat", headers=headers).json()
    dr = next(s for s in subs if s["category"] == "ASSET")
    cr = next(s for s in subs if s["category"] in ("LIABILITY", "REVENUE"))

    r = client.post(f"/api/books/{book_id}/vouchers", headers=headers, json={
        "voucher_date": "2026-01-15",
        "entries": [
            {"subject_id": dr["id"], "direction": "DEBIT", "amount": "100.00", "summary": "矩阵"},
            {"subject_id": cr["id"], "direction": "CREDIT", "amount": "100.00", "summary": "矩阵"},
        ],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"], book_id


def test_VIEWER_调用每个账套写接口都被拒(client, db, book, plain_user, plain_auth, auth):
    """VIEWER 对本账套的写操作必须全部 403 —— 一个都不能过。"""
    from app.models import UserBook

    vid, _other_book_id = _make_voucher_as(client, auth)
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()

    periods = client.get(f"/api/books/{book.id}/periods", headers=plain_auth).json()
    pid = periods[0]["id"]

    cases = [
        ("期初写入", "POST", f"/api/books/{book.id}/periods", []),
        ("结账", "POST", f"/api/books/{book.id}/periods/{pid}/close", None),
        ("反结账", "POST", f"/api/books/{book.id}/periods/{pid}/unclose",
         {"revert_posting": True}),
        ("结转损益", "POST", f"/api/books/{book.id}/periods/{pid}/carry-forward", {}),
        ("年度结转", "POST", f"/api/books/{book.id}/year-end", {"from_year": 2026}),
        ("建凭证", "POST", f"/api/books/{book.id}/vouchers",
         {"voucher_date": "2026-01-15", "entries": []}),
        ("改凭证", "PUT", f"/api/books/{book.id}/vouchers/{vid}",
         {"voucher_date": "2026-01-15", "entries": []}),
        ("删凭证", "DELETE", f"/api/books/{book.id}/vouchers/{vid}", None),
        ("批量记账", "POST", f"/api/books/{book.id}/vouchers/post", {"ids": [vid]}),
        ("批量反记账", "POST", f"/api/books/{book.id}/vouchers/unpost", {"ids": [vid]}),
        ("批量删除", "POST", f"/api/books/{book.id}/vouchers/batch-delete", {"ids": [vid]}),
        ("拆分凭证", "POST", f"/api/books/{book.id}/vouchers/{vid}/split", {"at_line": 1}),
        ("整理凭证", "POST", f"/api/books/{book.id}/vouchers/organize", {}),
        ("提交导入", "POST", f"/api/books/{book.id}/vouchers/import/999999/commit", None),
        ("作废导入", "POST", f"/api/books/{book.id}/vouchers/import/999999/discard", None),
        ("新建科目", "POST", f"/api/books/{book.id}/subjects",
         {"code": "9999", "name": "只读新建", "category": "ASSET", "direction": "DEBIT"}),
        ("改账套", "PUT", f"/api/books/{book.id}", {"name": "只读改名"}),
        # 改「结转损益冲到哪个科目」= 改账务口径，只读账号不许改
        ("设本年利润", "PUT",
         f"/api/books/{book.id}/years/2026/profit-subject", {"code": "3103"}),
        ("新建打印模板", "POST", f"/api/books/{book.id}/print/templates",
         {"scope": "VOUCHER", "name": "只读模板", "content": {}}),
        ("报表落库", "POST",
         f"/api/reports/1/persist?book_id={book.id}&period_id={pid}", None),
        ("改报表公式", "PUT", f"/api/reports/1/cells?book_id={book.id}", []),
        ("新建报表模板", "POST", f"/api/reports/templates?book_id={book.id}",
         {"name": "只读报表"}),
    ]

    leaked = []
    for label, method, path, body in cases:
        kwargs = {"headers": plain_auth}
        if body is not None:
            kwargs["json"] = body
        r = client.request(method, path, **kwargs)
        if r.status_code != 403:
            leaked.append(f"{label:10} {method:6} {path} -> {r.status_code}")
    assert not leaked, "VIEWER 竟然能写：\n" + "\n".join(leaked)


def test_ACCOUNTANT_写权限没有被收紧(client, db, book, plain_user, plain_auth):
    """收紧只针对 VIEWER。会计该能做的还得能做，否则等于把账锁死。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()

    r = client.post(f"/api/books/{book.id}/subjects", headers=plain_auth,
                    json={"code": "1998", "name": "会计建的科目", "category": "ASSET",
                          "direction": "DEBIT"})
    assert r.status_code == 201, f"会计连建科目都不行：{r.status_code} {r.text}"


def test_VIEWER_仍然能读(client, db, book, plain_user, plain_auth):
    """只读不是不能用。读路径必须照常работа —— 否则就是权限模型整体崩了。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()

    for path in (f"/api/books/{book.id}",
                 f"/api/books/{book.id}/periods",
                 f"/api/books/{book.id}/subjects",
                 f"/api/books/{book.id}/vouchers"):
        r = client.get(path, headers=plain_auth)
        assert r.status_code == 200, f"只读账号读不了 {path}：{r.status_code} {r.text}"
