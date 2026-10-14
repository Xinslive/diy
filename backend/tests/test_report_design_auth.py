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


# -------------------------------------------- P2-13：零账套账号不得读取模板口径
#
# 上面那组钉的是「必须登录」。但**登录**不等于「有权看这家单位的账」：
# 自助注册产生的空账号一个账套都没有，而报表模板是全局的、跨账套共用的，
# 于是它照样能把资产负债表的科目排列和取数公式全枚举一遍。


def test_零账套账号读不到模板列表(client, plain_auth, db, plain_user):
    from app.models import UserBook

    assert not db.scalars(select(UserBook).where(UserBook.user_id == plain_user.id)).all()
    r = client.get("/api/reports/templates", headers=plain_auth)
    assert r.status_code == 403, f"零账套账号拿到 {r.status_code}：{r.text[:200]}"


def test_零账套账号读不到取数公式(client, plain_auth, db, tpl_id):
    """这条就是审计里的复现：一个账套都没有，却能读到完整行列公式。"""
    r = client.get(f"/api/reports/{tpl_id}/design", headers=plain_auth)
    assert r.status_code == 403, (
        f"零账套账号拿到 {r.status_code}：报表口径已泄漏，{r.text[:200]}")


def test_只要有一个账套授权就能读(client, plain_auth, db, book, plain_user, tpl_id):
    """VIEWER 也该能读 —— 只读不是不能用，把权限模型整体堵掉是另一种事故。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    assert client.get("/api/reports/templates", headers=plain_auth).status_code == 200
    assert client.get(f"/api/reports/{tpl_id}/design", headers=plain_auth).status_code == 200


# ---------------------------------------------- P1-5：全局模板写入 fail-closed
#
# 审计场景：用户在账套 B 是会计、在账套 A 是**只读**。
# 报表模板没有 book_id、却是所有账套共用的，而旧的 ``_can_write`` 在
# **缺省 book_id** 时退化成「该用户在任意账套非 VIEWER」——
# 于是不带 book_id 就能改写出账套 A 也在用的公式与打印模板。
#
# 现在两道闸都必须是 fail-closed 的：显式目标账套 + 该账套 OWNER。


def _two_books(client, auth, db, plain_user):
    """造两个账套：B 里普通用户是会计，A 里是只读。返回 (A, B)。

    名字必须每例唯一：``client`` 跑在**另一个** Session 里并且已经提交，
    ``db`` 夹具的 rollback 回滚不掉它 —— 下一个用例再建同名账套就会撞
    「账套已存在」的唯一约束（第一批用例踩过同一个坑，所以都带 uuid 后缀）。
    """
    import uuid

    from app.models import UserBook

    tag = uuid.uuid4().hex[:8]
    ids = []
    for name in (f"甲方账套{tag}", f"乙方账套{tag}"):
        r = client.post("/api/books", headers=auth,
                        json={"company_name": f"{name}公司", "name": name,
                              "start_year": 2026})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    a_id, b_id = ids
    db.add(UserBook(user_id=plain_user.id, book_id=a_id, role="VIEWER"))
    db.add(UserBook(user_id=plain_user.id, book_id=b_id, role="ACCOUNTANT"))
    db.commit()
    return a_id, b_id


def test_不带账套不能建模板(client, plain_auth, db, plain_user, auth, tpl_id):
    """审计的复现路径：不带 book_id 就该被挡下，而不是「任一可写账套」放行。"""
    a_id, b_id = _two_books(client, auth, db, plain_user)
    before = len(db.scalars(select(ReportTemplate)).all())

    r = client.post("/api/reports/templates", headers=plain_auth,
                    json={"name": "跨账套偷改", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 400, (
        f"拿到 {r.status_code}：全局模板的门只看了一个无关账套的角色")
    assert "book_id" in r.json()["detail"], r.json()
    db.expire_all()
    assert len(db.scalars(select(ReportTemplate)).all()) == before, "居然真的建出来了"


def test_只读账套不能建模板(client, plain_auth, db, plain_user, auth, tpl_id):
    """显式指向只读的那个账套 —— 必须 403，且理由说清是全局口径。"""
    a_id, b_id = _two_books(client, auth, db, plain_user)
    r = client.post(f"/api/reports/templates?book_id={a_id}", headers=plain_auth,
                    json={"name": "改只读账套的表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:200]}"
    assert "全局口径" in r.json()["detail"], r.json()


def test_会计在B也不能改全局模板(client, plain_auth, db, plain_user, auth, tpl_id):
    """会计改全局公式也不行 —— 与打印侧（OWNER 才放行）对齐。"""
    a_id, b_id = _two_books(client, auth, db, plain_user)
    r = client.post(f"/api/reports/templates?book_id={b_id}", headers=plain_auth,
                    json={"name": "会计改全局表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:200]}"


def test_所有者显式指定账套就能改(client, plain_auth, db, plain_user, auth, tpl_id):
    """收紧只针对「全局且无主」：OWNER 显式指定账套必须照常能改，否则等于把账锁死。"""
    r = client.post("/api/books", headers=plain_auth,
                    json={"company_name": "自有公司", "name": "自有账套", "start_year": 2026})
    assert r.status_code == 201, r.text   # 自己建的账套自动是 OWNER
    own_book = r.json()["id"]

    r = client.post(f"/api/reports/templates?book_id={own_book}", headers=plain_auth,
                    json={"name": "所有者建表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, f"OWNER 也被挡住了：{r.status_code} {r.text[:200]}"

    # 改了公式同样放行
    r = client.put(f"/api/reports/{tpl_id}/cells?book_id={own_book}", headers=plain_auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1+1"}])
    assert r.status_code == 200, f"OWNER 改不了公式：{r.status_code} {r.text[:200]}"


def test_改公式同样要求显式账套(client, plain_auth, db, plain_user, auth, tpl_id):
    """门禁覆盖**每一个**写入口，不只是新建。"""
    before = client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()["cells"]
    r = client.put(f"/api/reports/{tpl_id}/cells", headers=plain_auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1"}])
    assert r.status_code == 400, f"拿到 {r.status_code}：{r.text[:200]}"
    after = client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()["cells"]
    assert before == after, "公式居然被改了"


# ------------------------------- 审计的另一条建议：无论结论如何，锁住「匿名不可写」

def test_匿名改不了公式(client, db, tpl_id):
    r = client.put(f"/api/reports/{tpl_id}/cells",
                   json=[{"row_no": 2, "col_no": 3, "formula": "=A1+1"}])
    assert r.status_code == 401, f"匿名改公式拿到 {r.status_code}"


def test_匿名改不了行(client, db, tpl_id):
    r = client.post(f"/api/reports/templates/{tpl_id}/rows", json={"label": "偷偷加的行"})
    assert r.status_code == 401, f"匿名插行拿到 {r.status_code}"


def test_匿名不能新建模板(client, db):
    r = client.post("/api/reports/templates", json={"code": "X", "name": "X", "type": "CUSTOM"})
    assert r.status_code == 401, f"匿名建模板拿到 {r.status_code}"
