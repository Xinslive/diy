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

import uuid

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.deps import get_current_user
from app.models import ReportCell, ReportCol, ReportRow, ReportTemplate

_REPORTS_PREFIX = "/api/reports"


# ------------------------------------------------------------------ 夹具
#
# 这里有两种模板，**别混用**：
#
# * ``tpl_id`` —— 官方内置基线（``book_id IS NULL``）。所有账套共看同一份，
#   而且只读。它是「读」这件事的正当对象，也是 400「内置报表不能直接改」
#   的触发对象。
# * ``owned_tpl_id`` —— 归属 ``book`` 的副本。写接口只认这一种。
#
# 写接口拿基线去调会拿到 400（基线是公开的，回一句「去设计模式改副本」
# 不泄露任何新信息）。但对「验证门禁**放行**」的用例来说那仍然是「什么都没
# 验成」，所以验放行必须用 ``owned_tpl_id``。

@pytest.fixture
def tpl_id(db, book):
    """一张真实存在的**官方内置基线** id。

    用真实 id 是为了让「匿名 200」这条复现有意义：拿不存在的 id 时，
    鉴权没过会返回 401、过了才返回 404，两条路径容易混。

    显式按 ``book_id IS NULL`` 取，别只按 code：某个账套 fork 之后库里
    会有同 code 的副本，按 code 取可能拿到副本，测的就不是基线版式了。
    """
    _ = book  # 读接口现在必须带 book_id，这里先把账套准备好
    tid = db.scalar(select(ReportTemplate.id).where(
        ReportTemplate.code == "BALANCE_SHEET", ReportTemplate.book_id.is_(None)))
    assert tid, "内置资产负债表模板没建出来，种子数据有问题"
    return tid


@pytest.fixture
def owned_tpl_id(db, book, tpl_id):
    """一张归属 ``book`` 的资产负债表副本，供写接口使用。

    模板按账套隔离（迁移 0020）之后，写路径只接受归属当前账套的模板，
    所以**造夹具时必须带上 ``book_id=book.id``** —— 不带的话造出来的是
    只读基线，后续每一步写操作都会 404。
    """
    tag = uuid.uuid4().hex[:8]
    t = ReportTemplate(code=f"TEST_{tag}", book_id=book.id, name=f"测试报表{tag}",
                       type="CUSTOM", remark="")
    db.add(t)
    db.flush()
    # 骨架照抄基线：门禁用例只关心「能不能改」，不关心表长什么样，
    # 但沿用真实版式能让坐标（行 2 / 列 3 是数值列）读起来不费解释。
    for r in db.scalars(select(ReportRow).where(ReportRow.template_id == tpl_id)).all():
        db.add(ReportRow(template_id=t.id, row_no=r.row_no, label=r.label,
                         indent=r.indent, bold=r.bold, hidden=r.hidden, style=r.style))
    for c in db.scalars(select(ReportCol).where(ReportCol.template_id == tpl_id)).all():
        db.add(ReportCol(template_id=t.id, col_no=c.col_no, label=c.label,
                         kind=c.kind, hidden=c.hidden, width=c.width))
    for c in db.scalars(select(ReportCell).where(ReportCell.template_id == tpl_id)).all():
        db.add(ReportCell(template_id=t.id, row_no=c.row_no, col_no=c.col_no,
                          formula=c.formula))
    db.commit()
    db.refresh(t)
    return t.id


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

def test_登录后仍然能读模板列表(client, auth, db, book, tpl_id):
    """``book_id`` 现在是**必填**：列表靠它判断哪些模板对这个账套可见。"""
    r = client.get(f"/api/reports/templates?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text
    assert any(x["code"] == "BALANCE_SHEET" for x in r.json())


def test_登录后仍然能读模板设计(client, auth, db, book, tpl_id):
    r = client.get(f"/api/reports/{tpl_id}/design?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text
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
    assert client.get(f"/api/reports/templates?book_id={book.id}",
                      headers=plain_auth).status_code == 200
    assert client.get(f"/api/reports/{tpl_id}/design?book_id={book.id}",
                      headers=plain_auth).status_code == 200


# ---------------------------------------------- P1-5：模板写入 fail-closed
#
# 审计场景：用户在账套 B 是会计、在账套 A 是**只读**。
# 报表模板当时没有 book_id、却是所有账套共用的，而旧的 ``_can_write`` 在
# **缺省 book_id** 时退化成「该用户在任意账套非 VIEWER」——
# 于是不带 book_id 就能改写出账套 A 也在用的公式与打印模板。
#
# 迁移 0020 之后模板有了归属，门禁多了一条：**写路径只接受归属当前账套的
# 模板**（基线只读、回 400 讲清怎么改，别人的模板一律 404）。但底下那两道
# 闸没变，也仍然必须 fail-closed：显式目标账套 + 该账套 OWNER。
# 「收紧不许收紧到把账锁死」——OWNER 显式指定账套必须照常能改。


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
    """审计的复现路径：不带 book_id 就该被挡下，而不是「任一可写账套」放行。

    状态码是 422 而不是 400，原因值得记一笔：``create_template`` 挂着
    ``get_my_book``，而它把 ``book_id`` 声明成必填查询参数，于是请求在
    **进函数体之前**就被 FastAPI 挡下了 —— 函数体里那句带解释文案的 400
    （``_require_template_write``）在这条路由上压根走不到。
    不挂 ``get_my_book`` 的写路由（cells / rows / patch / import…）拿到的是 400。
    两种都算「被挡下」，所以这里按集合断言；真正不能松的是「一个都没建出来」。
    """
    a_id, b_id = _two_books(client, auth, db, plain_user)
    before = len(db.scalars(select(ReportTemplate)).all())

    r = client.post("/api/reports/templates", headers=plain_auth,
                    json={"name": "跨账套偷改", "base_code": "BALANCE_SHEET"})
    assert r.status_code in (400, 422), (
        f"拿到 {r.status_code}：全局模板的门只看了一个无关账套的角色")
    assert "book_id" in str(r.json()["detail"]), r.json()
    db.expire_all()
    assert len(db.scalars(select(ReportTemplate)).all()) == before, "居然真的建出来了"


def test_只读账套不能建模板(client, plain_auth, db, plain_user, auth, tpl_id):
    """显式指向只读的那个账套 —— 必须 403，且理由说清是**本账套的口径**。

    文案随隔离改过：模板不再是全局口径，所以说的是「本账套的口径，
    只有账套所有者或管理员可以修改」。断言跟着改，但 403 本身没变。
    """
    a_id, b_id = _two_books(client, auth, db, plain_user)
    r = client.post(f"/api/reports/templates?book_id={a_id}", headers=plain_auth,
                    json={"name": "改只读账套的表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:200]}"
    assert "本账套的口径" in r.json()["detail"], r.json()


def test_会计在B也不能建模板(client, plain_auth, db, plain_user, auth, tpl_id):
    """会计建模板也不行 —— 与打印侧（OWNER 才放行）对齐。

    以前这条叫「会计改全局模板」；模板有了归属之后要改的始终是
    **本账套自己那份**，所以「改全局」这个说法已经不成立，名字一并改掉 ——
    留着会让人以为这里还有一张全局表。
    """
    a_id, b_id = _two_books(client, auth, db, plain_user)
    r = client.post(f"/api/reports/templates?book_id={b_id}", headers=plain_auth,
                    json={"name": "会计建表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 403, f"拿到 {r.status_code}：{r.text[:200]}"


def test_所有者显式指定账套就能改(client, plain_auth, db, plain_user, auth, tpl_id):
    """收紧只针对「无归属却无主」：OWNER 显式指定账套必须照常能改，否则等于把账锁死。

    注意「改公式」那一步打的是**刚建出来的那张**（它一出生就归属
    ``own_book``），不是内置基线：基线只读，打它只会拿到 400，
    那样这条用例就变成在验「基线不可写」，而它本来要验的是「OWNER 能写」。
    """
    r = client.post("/api/books", headers=plain_auth,
                    json={"company_name": "自有公司", "name": "自有账套", "start_year": 2026})
    assert r.status_code == 201, r.text   # 自己建的账套自动是 OWNER
    own_book = r.json()["id"]

    r = client.post(f"/api/reports/templates?book_id={own_book}", headers=plain_auth,
                    json={"name": "所有者建表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, f"OWNER 也被挡住了：{r.status_code} {r.text[:200]}"
    own_tpl = r.json()["id"]

    # 改了公式同样放行
    r = client.put(f"/api/reports/{own_tpl}/cells?book_id={own_book}", headers=plain_auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1+1"}])
    assert r.status_code == 200, f"OWNER 改不了公式：{r.status_code} {r.text[:200]}"


def test_改公式同样要求显式账套(client, plain_auth, db, plain_user, auth, book, tpl_id):
    """门禁覆盖**每一个**写入口，不只是新建。

    拿 ``book`` 归属的那张副本当靶子：门禁的作用发生在查模板之前，
    所以「不带 book_id 就该 400」这条与靶子归属无关，但带上 ``book_id``
    去读前后公式才读得到，才能验「公式真的没被改」。
    """
    before = client.get(f"/api/reports/{tpl_id}/design?book_id={book.id}",
                        headers=auth).json()["cells"]
    r = client.put(f"/api/reports/{tpl_id}/cells", headers=plain_auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1"}])
    assert r.status_code == 400, f"拿到 {r.status_code}：{r.text[:200]}"
    after = client.get(f"/api/reports/{tpl_id}/design?book_id={book.id}",
                       headers=auth).json()["cells"]
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
