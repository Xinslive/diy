"""报表模板读接口的登录校验（审计 P3-09）。

审计原判 C 级「待产品确认」：报表模板跨账套共享，所以「设计上就是公共的」
也是一种解读。**本仓库给出的结论是：不是公共的，应当要求登录。**

理由三条，都跟「跨账套共享」不冲突：

1. 「跨账套共享」说的是**同一次部署内部**多个账套用同一套模板，
   不是「对互联网公开」。同一个部署里的所有账套背后都是同一批会计和同一套
   内部报表口径，把它匿名放出去等于把这套口径公开。
2. ``/design`` 返回的是完整的行、列、单元格公式 —— 资产负债表的科目排列
   和取数公式本身就是内部经营信息。即便一张报表是空的，结构也说明了这家
   单位用什么口径算账。
3. 前端不存在合法的匿名消费方：报表设计页在应用外壳里，进来之前必然已经
   登录。加这道校验对现有流程零影响，只挡掉不在设计意图内的调用。

所以这里按「需要登录」实现，并留一条静态守卫（``test_报表接口全部要求登录``）：
以后谁再加一条不带 ``get_current_user`` 的报表接口，这条直接红。
审计同时建议「无论结论如何，补上匿名不可写的验证」，也在下面锁住。
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.deps import get_current_user
from app.models import ReportTemplate

_REPORTS_PREFIX = "/api/reports"


# ------------------------------------------------------------------ 夹具

@pytest.fixture
def tpl_id(db, book):
    """拿一张真实存在的内置模板 id。

    用真实 id 是为了让「匿名 200」这条复现有意义：拿不存在的 id 时，
    鉴权没过会返回 401、过了才返回 404，两条路径容易混。
    """
    _ = book  # 写接口那条用例要用写权限，这里先把账套准备好
    tid = db.scalar(select(ReportTemplate.id).where(ReportTemplate.code == "BALANCE_SHEET"))
    assert tid, "内置资产负债表模板没建出来，种子数据有问题"
    return tid


# ------------------------------------------------------- 静态守卫：漏挂就红

def _all_routes() -> list[APIRoute]:
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
    calls: set = set()

    def walk(dep) -> None:
        for d in dep.dependencies or []:
            if d.call is not None:
                calls.add(d.call)
            walk(d)

    walk(route.dependant)
    return calls


def test_报表接口全部要求登录():
    """任何 ``/api/reports`` 接口都必须挂着 get_current_user。

    不按「读/写」分类 —— 分类清单迟早会跟实际路由对不上（新增接口忘了登记，
    于是被漏检）。这里只守一条底线：这个前缀下不许出现匿名接口。
    """
    missing = []
    for r in _all_routes():
        if not r.path.startswith(_REPORTS_PREFIX):
            continue
        if get_current_user not in _dep_calls(r):
            missing.append(f"{sorted(set(r.methods))[0]:6} {r.path}")
    assert not missing, (
        "这些报表接口匿名可调：\n" + "\n".join(missing))


# ---------------------------------------------- 动态：匿名读不到模板结构

def test_匿名读不到模板列表(client, db, tpl_id):
    r = client.get("/api/reports/templates")
    assert r.status_code == 401, f"匿名拿到 {r.status_code}，响应体 {r.text[:200]}"


def test_匿名读不到模板设计结构(client, db, tpl_id):
    """这条就是审计里的复现：未带令牌 → 200 + 完整行列公式。"""
    r = client.get(f"/api/reports/{tpl_id}/design")
    assert r.status_code == 401, (
        f"匿名拿到 {r.status_code}：模板结构（行/列/单元格公式）已经泄漏，响应体 {r.text[:300]}")


def test_匿名连不存在的模板也是401而不是404(client, db):
    """鉴权应当先于存在性判断。

    反过来（先查存在性再鉴权）会形成一条探测通道：404/401 能区分
    「这个模板 id 存不存在」，等价于泄露模板清单。
    """
    r = client.get("/api/reports/99999999/design")
    assert r.status_code == 401, f"拿到 {r.status_code}，顺序反了，模板 id 可被枚举"


def test_无效令牌同样读不到(client, db, tpl_id):
    r = client.get(f"/api/reports/{tpl_id}/design",
                   headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401, f"拿到 {r.status_code}"


# ------------------------------------------------------ 动态：登录后照常

def test_登录后仍然能读模板列表(client, auth, db, tpl_id):
    r = client.get("/api/reports/templates", headers=auth)
    assert r.status_code == 200
    assert any(x["code"] == "BALANCE_SHEET" for x in r.json())


def test_登录后仍然能读模板设计(client, auth, db, tpl_id):
    r = client.get(f"/api/reports/{tpl_id}/design", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["rows"] and data["cols"], "登录后却读不到行列，矫枉过正把功能也堵了"
    assert any(c["formula"] for c in data["cells"]), "单元格公式读不到了"


# ------------------------------- 审计的另一条建议：无论结论如何，锁住「匿名不可写」

def test_匿名改不了公式(client, db, tpl_id):
    r = client.put(f"/api/reports/{tpl_id}/cells",
                   json=[{"row_no": 2, "col_no": 2, "formula": "=A1+1"}])
    assert r.status_code == 401, f"匿名改公式拿到 {r.status_code}"


def test_匿名改不了行(client, db, tpl_id):
    r = client.post(f"/api/reports/templates/{tpl_id}/rows", json={"label": "偷偷加的行"})
    assert r.status_code == 401, f"匿名插行拿到 {r.status_code}"


def test_匿名不能新建模板(client, db):
    r = client.post("/api/reports/templates", json={"code": "X", "name": "X", "type": "CUSTOM"})
    assert r.status_code == 401, f"匿名建模板拿到 {r.status_code}"
