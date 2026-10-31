"""共享卡对账插件的**端到端**用例。

这一组盯的是「一张卡被多个账套共用」这条主线能不能真的成立：

    3 个账套共用一张卡 → 上传**一份**对账单 → 每一行判给某个账套 →
    各账套记到这张卡上的数**加起来**等于这张卡的真实数。

**这个插件只对账，不制证**（1.1.0 起）：凭证是用户自己在账上录的，插件不替
用户猜对方科目。所以这里守的是**核对**，不是记账：

1. **归属正确**：每一行由用户**人工指定**归属（1.2.0 起没有规则自动判定了）。
2. **少记看得出来**：各账套该科目的**本期发生额**（账上动了多少）与
   **该记的**（对账单说该有多少）分借贷两侧比，就是这本账少记了多少。
3. **不污染宿主**：宿主的对账单台账一行不多 —— 共享卡的对账单是插件实体，
   不是宿主那一份。

插件包是从**磁盘上的真实目录**打包的，不是测试里另写一份 —— 测的必须是
将要装到用户机器上的那一份。
"""
from __future__ import annotations

import base64
import json
import pathlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.db import SessionLocal
from app.models.accounting import Subject
from app.models.book import Book
from app.models.system import UserBook
from tests.conftest import build_voucher_factory
from tests.plugin_fixtures import plugin_env
from tests.plugin_helpers import package

REPO = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "com.cwq.sharedcard"
FIXTURES = REPO / "backend" / "tests" / "fixtures" / "statements"

# **插件源码不在 git 里**（``/plugins/`` 是 gitignore 的，见 .gitignore 第 104 行）：
# **干净 checkout 里没有这份源码**。所以源码缺失时整份用例跳过，而不是失败 ——
# 在本机全绿、在 CI 必红的用例等于没有用例（与 ``test_plugin_periodic.py``、
# ``test_plugin_reimburse.py`` 同一处置）。
#:
#: 用 skipif 而不是模块级 pytest.skip：模块级跳过会让 pytest 认为「一条都没
#: 收集到」，退出码是 5；单跑这一个文件时 CI 会把它当成失败。
pytestmark = pytest.mark.skipif(
    not PLUGIN_DIR.is_dir(), reason="本机没有共享卡插件源码（plugins/ 不入库）")

#: 用哪份真实银行样例：5 行，收入支出都有，不是 PDF（不依赖 OCR 夹具）。
SAMPLE = "cmbc_xlsx.xlsx"
#: 这份样例的期初余额（期末 75844188.00 减去本期净额 69996871.00）。
OPENING = Decimal("5847317.00")


def _plugin_files():
    files = {}
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(PLUGIN_DIR))
        # plugin.json 由打包器自己写进包里，files 清单不许再列它。
        if relative == "plugin.json":
            continue
        files[relative] = path.read_bytes()
    return files


@pytest.fixture
def shared(plugin_env, client, auth, admin_user):
    """把**磁盘上那份**共享卡插件装上。"""
    build, restart = plugin_env
    real = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    def edit(manifest):
        # 换成真插件的 manifest（files 清单由打包器算好，别动）。
        for key in ("format_version", "id", "name", "version", "api_version", "description",
                    "permissions", "access", "contributes", "hooks", "tasks", "tables"):
            if key in real:
                manifest[key] = real[key]

    raw, manifest, _keys, _private = build(plugin_id=real["id"], files=_plugin_files(), edit=edit)
    response = client.post("/api/system/plugins/install",
                           files={"file": ("sharedcard.cwp", raw)}, headers=auth)
    assert response.status_code == 200, response.text
    assert client.put(f"/api/system/plugins/{real['id']}/users", headers=auth,
                      json={"user_ids": [admin_user.id]}).status_code == 200
    restart()
    return f"/api/plugins/{real['id']}"


@pytest.fixture
def books3(db, admin_user):
    """三个账套。

    三个账套用**不同的现金科目**（这正是「同一张卡在不同账套编码不同」的
    场景）：100203、100206，以及一个 **1001 库存现金**（验证共享卡支持
    1001 族 —— 宿主那条流水对账线只认 1002）。
    """
    from app.accounting.periods import create_periods
    from app.accounting.years import ensure_book_year
    from app.seed.chart_of_accounts import install_chart

    books = []
    for index in range(3):
        tag = uuid.uuid4().hex[:8]
        book = Book(company_name=f"共享卡公司{tag}", name=f"共享账套{index + 1}",
                    start_year=2026, start_month=1)
        db.add(book)
        db.flush()
        create_periods(db, book)
        install_chart(db, book.id)
        by = ensure_book_year(db, book, 2026)
        by.init_subjects_at = datetime.now(timezone.utc)
        by.init_subjects_source = "CHART"
        db.add(UserBook(user_id=admin_user.id, book_id=book.id, role="OWNER"))
        books.append(book)
    db.commit()
    for book in books:
        db.refresh(book)
    return books


def _leaf(db, book, parent_code: str, code: str, name: str):
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == parent_code))
    # 父科目先不再是末级 —— 库层触发器 ``fn_subject_leaf_parent_guard``
    # 判的是「父科目不能是末级」。
    parent.is_leaf = False
    db.flush()
    subject = Subject(book_id=book.id, year=book.start_year, code=code, name=name,
                      parent_id=parent.id, level=parent.level + 1, category=parent.category,
                      direction=parent.direction, is_leaf=True, carry_forward=False,
                      sort_no=1, active=True)
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return subject


@pytest.fixture
def cash_subjects(db, books3):
    """每本账的现金科目 + 对方科目。"""
    codes = [("1002", "100203", "银行存款-基本户"), ("1002", "100206", "银行存款-一般户"),
             ("1001", "100101", "库存现金-备用金")]
    out = []
    for book, (parent, code, name) in zip(books3, codes):
        cash = _leaf(db, book, parent, code, name)
        counter = _leaf(db, book, "2202", "224101", "其他应付款-往来款")
        out.append({"book": book, "cash": cash, "counter": counter})
    return out


@pytest.fixture
def outsider_book(db, admin_user):
    """一本**不在任何共享卡成员里**的账套，而且它在 ``book_list`` 里排在最前。

    宿主 ``book_list`` 的顺序是 ``sort_order → company_name → name`` —— 而
    ``company_name`` 是**用户自己起的名字**。所以要测「存储落在成员账套」这条
    规则，得有一本**排第一却不是成员**的账：否则「碰巧排第一」也能让这条规则
    看起来成立 —— 那样的闸门没有牙。
    """
    from app.accounting.periods import create_periods
    from app.accounting.years import ensure_book_year
    from app.seed.chart_of_accounts import install_chart

    book = Book(company_name="AAA非成员公司", name="非成员账套",
                start_year=2026, start_month=1)
    db.add(book)
    db.flush()
    create_periods(db, book)
    install_chart(db, book.id)
    by = ensure_book_year(db, book, 2026)
    by.init_subjects_at = datetime.now(timezone.utc)
    by.init_subjects_source = "CHART"
    db.add(UserBook(user_id=admin_user.id, book_id=book.id, role="OWNER"))
    db.commit()
    db.refresh(book)
    return book


def _json(response):
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. 插件装得上，卡建得起来
# ---------------------------------------------------------------------------

def test_插件装上后卡建得起来(shared, client, auth, books3):
    body = _json(client.post(shared + "/cards", headers=auth, json={
        "name": "基本户（三个账套共用）", "bank_name": "示例银行",
        "account_no": "6789", "cash_prefixes": "1001,1002",
    }))
    card_id = body["card"]["id"]
    cards = _json(client.get(shared + "/cards", headers=auth))["cards"]
    assert len(cards) == 1
    assert cards[0]["name"] == "基本户（三个账套共用）"
    assert cards[0]["cash_prefixes"] == ["1001", "1002"]


def test_成员科目必须过四道校验(shared, client, auth, db, books3, cash_subjects):
    """上级科目、停用科目、非现金族 —— 都要挡下来并说清楚为什么。"""
    card_id = _json(client.post(shared + "/cards", headers=auth,
                                json={"name": "校验用卡"}))["card"]["id"]
    first = cash_subjects[0]["book"]

    # 1002 银行存款是**上级**科目，不能记账。
    bad = client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": first.id, "subject_code": "1002"})
    assert bad.status_code == 422
    assert "末级" in bad.json()["detail"]

    # 停用的科目也不行。
    cash = cash_subjects[0]["cash"]
    cash.active = False
    db.commit()
    bad = client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": first.id, "subject_code": cash.code})
    assert bad.status_code == 422
    assert "停用" in bad.json()["detail"]
    cash.active = True
    db.commit()

    # 2202 应付账款不是现金科目族 —— 拿它承载银行卡，余额永远对不上。
    bad = client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": first.id, "subject_code": "224101"})
    assert bad.status_code == 422
    assert "一级科目" in bad.json()["detail"]

    # 1001 库存现金下的末级科目**可以**承载共享卡。
    ok = _json(client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": first.id, "subject_code": cash.code}))
    assert ok["member"]["subject_code"] == cash.code


def test_现金科目按一级科目判(shared, client, auth, db, books3, cash_subjects):
    """**一级科目是 1001 或 1002** —— 判据是「它的一级科目」，不是「编码前缀」。

    界面上那个下拉只列一级科目在白名单里的科目（用户批注：不能选 1012
    「其他货币资金」）。判据只有一份实现（后端），所以下拉里选得到的，
    后端一定收；收不到的，界面也不该出现。
    """
    card_id = _json(client.post(shared + "/cards", headers=auth,
                                json={"name": "一级科目用卡"}))["card"]["id"]
    book = books3[0]
    # 1012 其他货币资金 下挂一个末级科目 —— 一级科目不是 1001/1002，不该收。
    other = _leaf(db, book, "1012", "101201", "其他货币资金-第三方支付")
    bad = client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": book.id, "subject_code": other.code})
    assert bad.status_code == 422
    assert "1012" in bad.json()["detail"]

    # 目录里每个科目都带上一级科目编码 —— 前端就靠它筛下拉。
    catalog = _json(client.get(shared + "/catalog", headers=auth))
    for index, item in enumerate(cash_subjects):
        entry = next(row for row in catalog["books"] if row["id"] == item["book"].id)
        roots = {row["code"]: row["root_code"] for row in entry["subjects"]}
        assert roots[item["cash"].code] == ("1002" if index < 2 else "1001"), roots
        assert "1001" in entry["roots"] and "1002" in entry["roots"]
    first_entry = next(row for row in catalog["books"] if row["id"] == books3[0].id)
    first_roots = {row["code"]: row["root_code"] for row in first_entry["subjects"]}
    assert first_roots["101201"] == "1012"


def test_对方科目与默认对方科目被显式拒绝(shared, client, auth, cash_subjects):
    """**下线一个字段要显式接住它并报错** —— 不能悄悄丢掉。

    插件 1.1.0 起只对账、不制证，所以「默认对方科目」「对方科目」这两项
    都不存在了。老调用方还在传它们的话，接口要**说清楚为什么没有这一项**，
    而不是回 200、让用户以为设上了。
    """
    card_id = _json(client.post(shared + "/cards", headers=auth,
                                json={"name": "对方科目用卡"}))["card"]["id"]
    item = cash_subjects[0]
    bad = client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": item["book"].id, "subject_code": item["cash"].code,
        "counterparty_code": item["counter"].code})
    assert bad.status_code == 422
    assert "不制证" in bad.json()["detail"]

    # 规则接口整块下线了（1.2.0 起）—— 老调用方再调它，要拿到**一句说清
    # 原因的**话，而不是没有信息量的 404。
    bad = client.post(shared + f"/cards/{card_id}/rules", headers=auth, json={
        "book_id": item["book"].id, "match_field": "cp_name",
        "match_mode": "contains", "pattern": "丙供应链"})
    assert bad.status_code == 422
    assert "下线" in bad.json()["detail"]


def test_制证路由已经下线(shared, client, auth, loaded):
    """**插件不再制证** —— 那个路由不该还在（还在就意味着它还能记账）。"""
    stmt_id = loaded["stmt_id"]
    response = client.post(shared + f"/statements/{stmt_id}/vouchers", headers=auth)
    assert response.status_code == 404, response.text


def test_插件不再声明凭证权限(shared, client, auth, db):
    """**装上去的那个插件**不能还有 ``voucher:write`` —— 声明了就是给了记账的权限。

    查的是**库里那个已安装插件的 manifest**（``plugin_registry.manifest_metadata``），
    不是磁盘上的文件 —— 装上去的才算数。
    """
    from app.models.plugin import PluginRegistry

    registry = db.get(PluginRegistry, "com.cwq.sharedcard")
    assert registry is not None
    permissions = (registry.manifest_metadata or {}).get("permissions") or []
    assert "voucher:write" not in permissions, permissions
    # 1.3.0 起插件多了「按金额初筛」（entry:read）与「导出 Excel」（export:write）
    # 两条权限；1.4.0 起又多了「自定义匹配月份范围」与「导出账面明细账」；
    # 1.4.1 把那份导出**收窄成逐笔七列**；1.4.2 改了两处界面 —— 对账单明细的
    # 「返回」回**对账单列表**（不是插件首页），归属条与表头**固定在顶上**；
    # 1.4.3 又砍了卡详情页的界面 —— 页签只剩「对账单 / 成员账套」（对账单在前），
    # 「余额核对」那一屏与卡详情工具条上的「删除」都没了（**接口仍在**）。
    # 2.0.1：归属一次读完（读次数与对账单份数无关），工作台不再重复读账套表。
    # 2.0.2：「已保存的核对记录」单独成页；确认核对只算被选中那本账的账面侧，
    # 写完不再重拉整个工作台（服务器把这一组原样交回，页面就地打补丁）。
    # 版本号跟着走，别让它停在老版本上假装还是老插件。
    assert registry.version == "2.0.2", registry.version


def test_卡可以删(shared, client, auth, loaded, cash_subjects):
    """删卡要连同成员、规则、对账单、流水行、归属一起删干净。

    **留着孤儿行毫无意义** —— 而它们会被别的卡列表读到，还会在复核队列里
    冒出来（用户会以为还有一张卡没处理）。
    """
    card_id = loaded["card_id"]
    stmt_id = loaded["stmt_id"]
    assert _json(client.get(shared + f"/cards/{card_id}/members",
                             headers=auth))["members"]
    body = _json(client.delete(shared + f"/cards/{card_id}", headers=auth))
    assert body["deleted"] is True
    # 卡没了。
    assert client.get(shared + f"/cards/{card_id}", headers=auth).status_code == 422
    cards = _json(client.get(shared + "/cards", headers=auth))["cards"]
    assert card_id not in [item["id"] for item in cards]
    # 成员也没了。
    assert _json(client.get(shared + f"/cards/{card_id}/members",
                             headers=auth))["members"] == []
    # 对账单与它的行、归属也没了 —— 复核队列里不该再冒出这张卡的东西。
    queue = _json(client.get(shared + "/queue", headers=auth))["queue"]
    assert stmt_id not in [item["statement_id"] for item in queue]


def test_1001族的账套也能认领共享卡(shared, client, auth, cash_subjects):
    """宿主那条流水对账线只认 1002；共享卡这条线**两个族都认**。"""
    card_id = _json(client.post(shared + "/cards", headers=auth,
                                json={"name": "含1001的卡"}))["card"]["id"]
    item = cash_subjects[2]
    body = _json(client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
        "book_id": item["book"].id, "subject_code": item["cash"].code}))
    assert body["member"]["subject_code"] == item["cash"].code


def test_建卡是管理员动作_普通账号看到的是能照着做的话(shared, client, auth, admin_user, plain_user):
    """**全局表只有管理员能写** —— 插件要把它翻成一句能照着做的话。

    宿主那句原话是「全局配置写入需要管理员」：对着插件页面上的用户，
    它没说清**哪件事**要管理员，用户只会以为是插件坏了。
    """
    granted = client.put("/api/system/plugins/com.cwq.sharedcard/users", headers=auth,
                         json={"user_ids": [admin_user.id, plain_user.id]})
    assert granted.status_code == 200, granted.text
    login = client.post("/api/auth/login",
                        json={"username": plain_user.username, "password": "staff123"})
    assert login.status_code == 200, login.text
    staff = {"Authorization": "Bearer " + login.json()["access_token"]}

    body = client.post(shared + "/cards", headers=staff, json={"name": "普通账号建的卡"})
    assert body.status_code == 403, body.text
    detail = body.json()["detail"]
    assert "系统管理员" in detail and "共享卡" in detail, detail
    # 管理员**能**建 —— 这道闸只挡普通账号，不挡这条路本身。
    ok = _json(client.post(shared + "/cards", headers=auth, json={"name": "管理员建的卡"}))
    assert ok["card"]["name"] == "管理员建的卡"


# ---------------------------------------------------------------------------
# 2. 归属规则：整块下线（1.2.0 起）
# ---------------------------------------------------------------------------

def _card_with_members(shared, client, auth, cash_subjects):
    """建一张卡并把三个账套都挂上去。"""
    card_id = _json(client.post(shared + "/cards", headers=auth,
                                json={"name": "共享卡"}))["card"]["id"]
    for item in cash_subjects:
        _json(client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
            "book_id": item["book"].id, "subject_code": item["cash"].code}))
    return card_id


def test_规则接口已下线_说清原因而不是404(shared, client, auth, cash_subjects):
    """**下线一个功能也要说清楚为什么** —— 不能回一句没有信息量的 404。

    老页面（还没刷新的浏览器标签）会继续调这些接口。回 404 的话用户只会
    以为插件坏了；回一句「这个功能没有了，每一行都要人工指定归属」才是
    同一件事的两种说法。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    for method, path, body in (
        ("get", f"/cards/{card_id}/rules", None),
        ("post", f"/cards/{card_id}/rules", {"book_id": cash_subjects[0]["book"].id,
                                             "match_field": "cp_name",
                                             "match_mode": "contains", "pattern": "实业"}),
        ("patch", f"/cards/{card_id}/rules/1", {"pattern": "实业"}),
        ("delete", f"/cards/{card_id}/rules/1", None),
    ):
        response = client.request(method.upper(), shared + path, headers=auth, json=body)
        assert response.status_code == 422, (method, path, response.text)
        assert "下线" in response.json()["detail"], response.text


def test_按规则自动归属已下线(shared, client, auth, loaded):
    """「按规则自动归属」是规则的下游 —— 规则没了，它也就没有意义了。"""
    stmt_id = loaded["stmt_id"]
    response = client.post(shared + f"/statements/{stmt_id}/allocate", headers=auth)
    assert response.status_code == 422, response.text
    assert "下线" in response.json()["detail"], response.text


def test_流水行不再有规则命中信息(shared, client, auth, loaded):
    """没有规则，就没有「命中/冲突」这种现算的结论 —— 字段也不该再有。"""
    stmt_id = loaded["stmt_id"]
    rows = _json(client.get(shared + f"/statements/{stmt_id}/rows", headers=auth,
                            params={"limit": 50}))["rows"]
    assert rows
    for row in rows:
        assert "match" not in row, row


# ---------------------------------------------------------------------------
# 3. 端到端：一张卡、三个账套、一份对账单
# ---------------------------------------------------------------------------

@pytest.fixture
def loaded(shared, client, auth, books3, cash_subjects):
    """上传一份对账单，并**人工指定**一部分行的归属。

    1.2.0 起没有规则自动判定：每一行由用户指定归属。这里模拟用户的动作 ——
    丙供应链 → 第一本账；丁物业 → 第二本账；戊建材 → 第三本账。
    另外两行（乙实业、结息）**先不指定**，留在复核队列里。

    **上传时不传 ``book_id``** —— 界面不再让用户为对账单选账套（它是「为这张
    共享卡上传的」）。所以这里走的正是界面那条路，而不是绕开它。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["rows"] == 5, uploaded
    stmt_id = uploaded["statement_id"]
    rows = _json(client.get(shared + f"/statements/{stmt_id}/rows", headers=auth,
                            params={"limit": 50}))["rows"]
    plan = {"丙供应链": cash_subjects[0], "丁物业": cash_subjects[1],
            "戊建筑材料": cash_subjects[2]}
    assigned = 0
    for row in rows:
        for key, item in plan.items():
            if key in (row.get("cp_name") or ""):
                body = _json(client.post(shared + f"/statements/{stmt_id}/assign", headers=auth,
                                         json={"rows": [row["id"]],
                                               "book_id": item["book"].id,
                                               "reason": "人工指定"}))
                assigned += body["assigned"]
                break
    assert assigned == 3, assigned
    return {"card_id": card_id, "stmt_id": stmt_id, "uploaded": uploaded}


def test_上传与归属(shared, client, auth, loaded):
    """5 行：3 行人工指定了归属，2 行还没指定（进复核队列）。"""
    stmt_id = loaded["stmt_id"]
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    assert detail["row_count"] == 5
    assert detail["assigned"] == 3
    assert detail["pending"] == 2
    assert detail["income_total"] == "100007401.00"
    assert detail["expense_total"] == "30010530.00"
    assert detail["closing_balance"] == "75844188.00"


def test_上传不用选账套_是为这个共享卡上传的(shared, client, auth, cash_subjects):
    """**不为这份对账单选账套** —— 它是「为这张共享卡上传的」。

    界面不再有「替哪本账上传」那个下拉：这份对账单属于**这张卡**，不属于任何
    一本账。存在哪本账的表里只是**存储位置**（账套级备份/导出要覆盖得到它），
    由后端自己挑第一本可写的 —— 不是用户要做的选择。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["rows"] == 5, uploaded
    # 存储位置由后端挑，但它得落在这张卡的**成员账套**里 —— 不能是「碰巧排
    # 第一」的那本账（宿主 `book_list` 按 company_name 排，那是用户自己起的
    # 名字，排第一是随机的）。
    assert uploaded["book_id"] in [item["book"].id for item in cash_subjects]
    # 这份对账单挂在这张卡名下（卡 → 对账单 → 流水行，一条线）。
    listed = _json(client.get(shared + f"/cards/{card_id}/statements", headers=auth))["statements"]
    assert [item["id"] for item in listed] == [uploaded["statement_id"]]


def test_显式指定存储账套仍然可用(shared, client, auth, cash_subjects):
    """``book_id`` 降级成**存储位置提示**（API 层面），界面不再传它。

    老脚本还会传 —— 传了就照用（不然就是一次「传了却没生效」的静默丢弃），
    但它**不是「替谁上传」**：归属仍然靠人工指定。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 params={"book_id": cash_subjects[1]["book"].id},
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["book_id"] == cash_subjects[1]["book"].id
    assert uploaded["rows"] == 5, uploaded


def test_存储落在成员账套_不是碰巧排第一的那本(shared, client, auth, cash_subjects,
                                              outsider_book):
    """存储账套**必须是这张卡的成员**。

    宿主 ``book_list`` 按 ``company_name`` 排 —— 那是用户自己起的名字，「第一本」
    是**随机**的一本。所以「碰巧排第一」也能让「落在成员账套」这条规则看起来成立；
    这条用例专门放一本**排第一却不是成员**的账来把它钉死。
    """
    member_ids = {item["book"].id for item in cash_subjects}
    assert outsider_book.id not in member_ids
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["book_id"] != outsider_book.id, \
        "对账单存到了不是这张卡成员的那本账里"
    assert uploaded["book_id"] in member_ids


def test_同卡同期重复上传被拒(shared, client, auth, loaded, cash_subjects):
    """同一份流水传两遍就是记两遍账 —— 这道拦截是硬拦。"""
    data = (FIXTURES / SAMPLE).read_bytes()
    again = client.post(shared + f"/cards/{loaded['card_id']}/statements", headers=auth,
                        params={"book_id": cash_subjects[0]["book"].id},
                        files={"files": (SAMPLE, data, "application/octet-stream")})
    assert again.status_code == 422
    assert "已经有一份对账单" in again.json()["detail"]


def test_未归属的行进复核队列(shared, client, auth, loaded):
    queue = _json(client.get(shared + "/queue", headers=auth))["queue"]
    assert len(queue) == 1
    assert queue[0]["pending"] == 2
    # 1.2.0 起没有规则了，「规则冲突」这个概念也一并没了。
    assert "conflicts" not in queue[0], queue[0]


def test_复核队列给的是卡名不是归属账套(shared, client, auth, loaded):
    """队列里那列得说**哪张卡**，不能说「归属账套」。

    「归属」在 1.2.0 起是**用户人工指定**的事；之前那一列写的其实是这份对账单
    存在哪本账的表里（存储位置）。两个概念混在一列里，用户会以为归属是插件
    定的 —— 而插件不替用户猜。
    """
    queue = _json(client.get(shared + "/queue", headers=auth))["queue"]
    assert len(queue) == 1
    assert queue[0]["card_name"] == "共享卡", queue[0]


def test_人工指定归属后队列清空(shared, client, auth, loaded, cash_subjects):
    """人工复核：把没命中的两行指给第一本账。"""
    stmt_id = loaded["stmt_id"]
    rows = _json(client.get(shared + f"/statements/{stmt_id}/rows", headers=auth,
                            params={"limit": 50}))["rows"]
    unassigned = [row["id"] for row in rows if row["alloc"] is None]
    assert len(unassigned) == 2
    body = _json(client.post(shared + f"/statements/{stmt_id}/assign", headers=auth, json={
        "rows": unassigned, "book_id": cash_subjects[0]["book"].id,
        "reason": "人工指定：乙实业与结息归第一本账"}))
    assert body["assigned"] == 2
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    assert detail["assigned"] == 5
    assert detail["pending"] == 0


def test_普通账号传的对账单进他自己的复核队列(shared, client, auth, admin_user,
                                            plain_user, books3, cash_subjects, db):
    """**复核队列不能只对管理员有数据**。

    浏览器演练里传对账单的是**普通账号**（会计），而这一组用例原本全是管理员
    视角 —— 管理员能访问全部账套，普通账号只能访问被授权的那几本，队列是按
    「当前用户能访问的账套」扫出来的。两边的口径不一样，就得**两边都验**：
    少了普通账号这一条，权限范围一旦收窄，界面就会对真正在干活的人说
    「没有待复核的流水行」—— 而那是**谎报**：那 5 行就在那儿。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    for book in books3:
        db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()
    granted = client.put("/api/system/plugins/com.cwq.sharedcard/users", headers=auth,
                         json={"user_ids": [admin_user.id, plain_user.id]})
    assert granted.status_code == 200, granted.text
    login = client.post("/api/auth/login",
                        json={"username": plain_user.username, "password": "staff123"})
    assert login.status_code == 200, login.text
    staff = {"Authorization": "Bearer " + login.json()["access_token"]}

    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=staff,
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["rows"] == 5, uploaded

    queue = _json(client.get(shared + "/queue", headers=staff))["queue"]
    assert len(queue) == 1, queue
    assert queue[0]["pending"] == 5, queue
    assert queue[0]["card_name"] == "共享卡", queue
    # 管理员那边也得看到同一份 —— 队列是**这张卡**的，不是某个人的。
    admin_queue = _json(client.get(shared + "/queue", headers=auth))["queue"]
    assert [item["statement_id"] for item in admin_queue] == [uploaded["statement_id"]]


def _assign_all(shared, client, auth, stmt_id, book_id):
    """把还没归属的行都指给某一本账，**再把行读回来**。

    **要重新读**：归属是刚写进去的，拿改动之前那份行去算，归属还没生效 ——
    算出来的合计会少一块，而那块看起来像「对不上」。
    """
    rows = _json(client.get(shared + f"/statements/{stmt_id}/rows", headers=auth,
                            params={"limit": 50}))["rows"]
    unassigned = [row["id"] for row in rows if row["alloc"] is None]
    if unassigned:
        body = client.post(shared + f"/statements/{stmt_id}/assign", headers=auth, json={
            "rows": unassigned, "book_id": book_id})
        assert body.status_code == 200, body.text
    return _json(client.get(shared + f"/statements/{stmt_id}/rows", headers=auth,
                            params={"limit": 50}))["rows"]


def _net_of(rows, book_id=None):
    """一批行的净额（收入减支出）；``book_id`` 给了就只算归给那本账的。"""
    total = Decimal("0")
    for row in rows:
        if book_id is not None:
            if not row["alloc"] or int(row["alloc"]["book_id"]) != int(book_id):
                continue
        amount = Decimal(row["amount"] or "0")
        total += amount if row["direction"] == "IN" else -amount
    return total


def _dec(value):
    """把接口返回的金额字符串读成 Decimal（比字符串相等可靠得多）。"""
    return Decimal(str(value if value not in (None, "") else "0"))


def _sides_of(rows, book_id=None):
    """一批行「该记多少」：(收款, 支出)。收款记借方、支出记贷方。"""
    debit = Decimal("0")
    credit = Decimal("0")
    for row in rows:
        if book_id is not None:
            if not row["alloc"] or int(row["alloc"]["book_id"]) != int(book_id):
                continue
        amount = Decimal(row["amount"] or "0")
        if row["direction"] == "IN":
            debit += amount
        elif row["direction"] == "OUT":
            credit += amount
    return debit, credit


def test_归属之后各账套的流水加起来等于对账单(shared, client, auth, loaded, cash_subjects):
    """**收支相等**：Σ 各账套归属到的流水净额 == 对账单的净额，误差 0。

    这条是「纯对账」口径下的第一条判据：插件不记账，但**归属**必须把每一
    分钱分给对的账套 —— 分错了，后面的「少记了多少」就全是错的。
    """
    stmt_id = loaded["stmt_id"]
    rows = _assign_all(shared, client, auth, stmt_id, cash_subjects[0]["book"].id)
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    statement_net = (Decimal(detail["income_total"]) - Decimal(detail["expense_total"]))
    assert _net_of(rows) == statement_net, (rows, detail)
    # 每本账各摊多少。
    per_book = {}
    for row in rows:
        if not row["alloc"]:
            continue
        book_id = int(row["alloc"]["book_id"])
        amount = Decimal(row["amount"] or "0")
        per_book[book_id] = per_book.get(book_id, Decimal("0")) \
            + (amount if row["direction"] == "IN" else -amount)
    assert sum(per_book.values()) == statement_net
    assert len(per_book) == 3, per_book


def test_归属状态就是已归属(shared, client, auth, loaded, cash_subjects):
    """插件不再制证 —— 归属行的状态就是「已归属」，没有「待制证/已制证」。"""
    stmt_id = loaded["stmt_id"]
    rows = _assign_all(shared, client, auth, stmt_id, cash_subjects[0]["book"].id)
    statuses = {row["alloc"]["status"] for row in rows if row["alloc"]}
    assert statuses == {"ALLOCATED"}, statuses
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    assert "posted" not in detail, detail


def test_某本账没有对应期间时它不参与核对(shared, client, auth, db, loaded, cash_subjects):
    """一本账没有覆盖对账单期间的会计期间 —— 它那一格要**说清楚为什么空着**。

    不是缺陷，是语义：各账套的账期可以不一样（有的用 4-4-5 财年）。缺的
    那一期必须列进「缺期间」，否则用户会以为这本账「少记 0 元」= 对上了。
    """
    from app.models.book import Period

    stmt_id = loaded["stmt_id"]
    _assign_all(shared, client, auth, stmt_id, cash_subjects[0]["book"].id)
    third = cash_subjects[2]["book"]
    period = db.scalar(select(Period).where(Period.book_id == third.id,
                                             Period.year == 2026, Period.month == 9))
    db.delete(period)
    db.commit()
    body = _json(client.get(shared + f"/cards/{loaded['card_id']}/balance", headers=auth))
    assert body["missing_period"], body
    assert any("2026-09" in item for item in body["missing_period"]), body["missing_period"]
    row = next(item for item in body["books"] if item["book_id"] == third.id)
    assert "会计期间" in row["reason"], row


def test_余额核对指出哪本账少记了多少(shared, client, auth, admin_user, db, loaded, cash_subjects):
    """**少记 = 该记的收款/支出 - 账上实际记的**（分借贷两侧比）。

    这里造三本账：
    * 第一本账：只记了一半收款 → 少记一半收款 + 全部支出；
    * 第二本账：全额记上 → 不少记；
    * 第三本账：一笔没记 → 少记全部。

    **分两侧比，不比净额**：一笔 30 万的支出没记，净额差是负数，界面上
    「少记 -30 万」没法读；分两侧比，「少记了 30 万的支出」是一句没有
    歧义的话。
    """
    stmt_id = loaded["stmt_id"]
    rows = _assign_all(shared, client, auth, stmt_id, cash_subjects[0]["book"].id)
    sides = {item["book"].id: _sides_of(rows, item["book"].id) for item in cash_subjects}

    # 第一本账：只记一半收款；第二本账：全额记上；第三本账：一笔不记。
    half = Decimal("50000000.00")
    first = cash_subjects[0]
    maker = build_voucher_factory(db, first["book"], admin_user)
    maker("2026-09-15",
          dr=[(first["cash"].code, half, "记卡上的收款")],
          cr=[(first["counter"].code, half, "记卡上的收款")])
    second = cash_subjects[1]
    credit = sides[second["book"].id][1]
    if credit > 0:
        maker = build_voucher_factory(db, second["book"], admin_user)
        maker("2026-09-15",
              dr=[(second["counter"].code, credit, "记卡上的支出")],
              cr=[(second["cash"].code, credit, "记卡上的支出")])

    body = _json(client.get(shared + f"/cards/{loaded['card_id']}/balance", headers=auth))
    books = {item["book_id"]: item for item in body["books"]}
    assert set(books) == {item["book"].id for item in cash_subjects}, body["books"]

    # 第一本账：账面只有一半收款，支出没记。
    debit, credit = sides[first["book"].id]
    row = books[first["book"].id]
    assert _dec(row["book_debit"]) == half, row
    assert _dec(row["book_credit"]) == 0, row
    assert _dec(row["expect_debit"]) == debit, row
    assert _dec(row["expect_credit"]) == credit, row
    assert _dec(row["short_debit"]) == debit - half, row
    assert _dec(row["short_credit"]) == credit, row
    assert _dec(row["missing"]) == debit - half + credit, row

    # 第二本账：全额记上 → 不少记。
    debit, credit = sides[second["book"].id]
    row = books[second["book"].id]
    assert _dec(row["book_credit"]) == credit, row
    assert _dec(row["missing"]) == 0, row
    assert _dec(row["diff"]) == 0, row

    # 第三本账：一笔没记 → 少记全部。
    debit, credit = sides[cash_subjects[2]["book"].id]
    row = books[cash_subjects[2]["book"].id]
    assert _dec(row["missing"]) == credit, row

    # 「谁少记了」要**点名**，别让用户自己从一堆数里找。
    short = {item["book_id"]: item for item in body["short"]}
    assert second["book"].id not in short, body["short"]
    assert _dec(short[first["book"].id]["missing"]) == _dec(books[first["book"].id]["missing"])
    assert _dec(short[cash_subjects[2]["book"].id]["missing"]) == _dec(row["missing"])

    # 还没归属的行单独列出来 —— 它们算不到任何账套头上。
    assert body["unassigned"]["count"] == 0, body["unassigned"]
    # 对账单这一期：收款与支出。
    assert body["statement_debit"] == "100007401.00", body
    assert body["statement_credit"] == "30010530.00", body


def test_方向记反了也能看出来(shared, client, auth, admin_user, db, loaded, cash_subjects):
    """**收款记成贷方**（方向记反）—— 界面必须说得出「少记收款、多记支出」。

    只比净额的话，方向反了会互相抵消成一个 0，账明显明对不上却显示「对得上」。
    """
    stmt_id = loaded["stmt_id"]
    rows = _assign_all(shared, client, auth, stmt_id, cash_subjects[0]["book"].id)
    debit, credit = _sides_of(rows, cash_subjects[0]["book"].id)
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    # **故意记反**：收款记贷方。
    maker("2026-09-15",
          dr=[(item["counter"].code, debit, "方向记反了")],
          cr=[(item["cash"].code, debit, "方向记反了")])

    body = _json(client.get(shared + f"/cards/{loaded['card_id']}/balance", headers=auth))
    row = next(item for item in body["books"]
               if item["book_id"] == cash_subjects[0]["book"].id)
    # 收款一分没记在借方 → 「少记收款」。
    assert _dec(row["short_debit"]) == debit, row
    # 支出侧不是少记，而是**多贷了**（把收款错记成了贷方）。
    assert _dec(row["short_credit"]) == 0, row
    assert _dec(row["over"]) == debit - credit, row
    assert _dec(row["diff"]) != 0, row


def test_宿主的对账单台账一行不多(shared, client, auth, db, loaded):
    """共享卡的对账单是**插件实体** —— 宿主那张表不能多一行。

    宿主那边多一行意味着：用户在宿主流水对账页面上会看到这份对账单，
    而它其实属于一张跨账套共用的卡 —— 余额对账会报红，其余账套满屏假
    「存疑凭证」。
    """
    from app.models import BankStatement, BankStatementRow

    statements = db.scalar(select(BankStatement).with_only_columns(
        BankStatement.id).select_from(BankStatement)) or 0
    rows = db.scalar(select(BankStatementRow).with_only_columns(
        BankStatementRow.id).select_from(BankStatementRow)) or 0
    assert statements == 0
    assert rows == 0


def test_账套级备份覆盖插件的六张表(shared, client, auth, db, loaded, cash_subjects, tmp_path):
    """**备份覆盖**：账套级归档里，这张卡与本账套的那几张表都必须在。

    两种 scope 的待遇**不一样**，这里逐条钉住：

    * ``card`` 是 **global**（卡是跨账套的配置）→ **整表**进归档。漏了它，
      恢复出来的是一张有成员、有流水、却**没有卡**的半截数据。
    * 其余五张是 **book** → 只带**本账套**的行。别的账套的数据不该被塞进
      这一份归档（那等于把账套 A 的备份塞进账套 B 的恢复里）。

    这条用例是「机器证明」—— 读代码猜不出来，得看归档里到底有什么。
    """
    from app.backup.export import export_scope, read_snapshot, SCOPE_BOOK

    card_id = loaded["card_id"]
    # **备份「对账单实际落在的那本账」** —— 存储账套是后端挑的（1.2.0 起优先
    # 这张卡的成员账套），测试不能假设是哪一本：假设错了，断言就变成在测
    # 「数据碰巧落在第一本」而不是「数据落在哪本就覆盖到哪本」。
    owner = loaded["uploaded"]["book_id"]
    assert owner in [item["book"].id for item in cash_subjects]

    path = tmp_path / "book.json.gz"
    with SessionLocal() as session:
        export_scope(session, scope=SCOPE_BOOK, book_id=owner,
                     book_label=owner, out_path=path)
    data = read_snapshot(path)

    prefix = "plugin_275b391c351a_"
    for suffix in ("card", "member", "rule", "stmt", "row", "alloc", "recon"):
        assert prefix + suffix in data["tables"], \
            "账套级备份漏了 %s 表" % suffix

    # global 的卡：整表进去。
    cards = data["tables"][prefix + "card"]["rows"]
    assert len(cards) == 1
    assert cards[0]["id"] == card_id

    # book 级的五张：只有本账套的行（库里另有另外两本账的行）。
    for suffix in ("member", "stmt", "row", "alloc"):
        rows = data["tables"][prefix + suffix]["rows"]
        assert rows, "%s 表在归档里是空的" % suffix
        assert {row["book_id"] for row in rows} == {owner}, \
            "%s 表混进了别的账套的行" % suffix
    # 规则表**还在归档里**（迁移链不改，表还在），但它已经是空表 ——
    # 1.2.0 起规则整块下线，建不出规则来了。
    assert data["tables"][prefix + "rule"]["rows"] == []

    # 库里明明有另外两本账的行 —— 证明上面的「只有本账套」是**过滤**出来的，
    # 不是因为库里本来就只有一本账。
    db_members = db.execute(
        text("SELECT count(*) FROM %s" % (prefix + "member"))).scalar()
    assert db_members == 3, db_members
    assert len(data["tables"][prefix + "member"]["rows"]) == 1


# ---------------------------------------------------------------------------
# 4. 重新解析：行按位置锚定，归属一行都不动
# ---------------------------------------------------------------------------


def _sample_bytes(mutate):
    """在样例对账单上做一点改动，返回新的文件字节。

    **真的改文件**，不是 mock 解析结果 —— 这条路的问题正出在「重新解析出来
    的行」与「库里已有的行」对上之后，所以必须让宿主的解析器真的再跑一遍。
    """
    import io

    import openpyxl

    workbook = openpyxl.load_workbook(FIXTURES / SAMPLE)
    mutate(workbook.active)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _change_first_amount(sheet):
    """第 1 行流水（乙实业，9780）的借方发生额改成 9999。"""
    sheet.cell(row=19, column=3).value = 9999


def _change_second_amount(sheet):
    """第 2 行流水（丙供应链，贷方 100000000）的贷方发生额改成 99999999。"""
    sheet.cell(row=20, column=4).value = 99999999


def _drop_last_row(sheet):
    """删掉最后一行流水（结息）—— 行数从 5 变成 4。"""
    sheet.delete_rows(23)


def _rows(shared, client, auth, stmt_id):
    return _json(client.get(shared + f"/statements/{stmt_id}/rows", headers=auth,
                            params={"limit": 50}))["rows"]


def test_重新解析不会把已有归属冲掉(shared, client, auth, loaded):
    """**这一条是回归**：旧实现把新解析的行整批插回去，而 row 表上有
    ``(book_id, stmt_id, seq)`` 唯一约束 —— 只要这份对账单已经有过归属，
    重新解析就必然撞约束、整批回滚（HTTP 400）。

    修法是「行按 ``seq`` 锚定、**行 id 不变**」：同一个位置的行就地改内容，
    归属不用改指向，约束也撞不上。
    """
    stmt_id = loaded["stmt_id"]
    before = _rows(shared, client, auth, stmt_id)
    assert len(before) == 5
    data = (FIXTURES / SAMPLE).read_bytes()
    body = _json(client.post(shared + f"/statements/{stmt_id}/reparse", headers=auth,
                              files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert body["rows"] == 5, body
    assert body["carried"] == 3, body
    assert body["added"] == 0 and body["removed"] == 0, body
    after = _rows(shared, client, auth, stmt_id)
    assert len(after) == 5
    assert [row["id"] for row in after] == [row["id"] for row in before]
    assert [row["alloc"] for row in after] == [row["alloc"] for row in before]


def test_已归属的行按新解析刷新_归属不动(shared, client, auth, loaded):
    """**已归属的行内容跟着新解析走，归属不动**。

    插件不再制证，所以「那一行在账上有没有凭证」不再是约束；但归属是**用户
    做的决定**，重新解析不能把它冲回去 —— 行 id 不变、归属账套不变，
    只有内容按新解析刷新。
    """
    stmt_id = loaded["stmt_id"]
    before = _rows(shared, client, auth, stmt_id)
    # 第 2 行（丙供应链）是**已归属**的那一行 —— 改它的金额。
    target = next(row for row in before if row["alloc"] is not None)
    data = _sample_bytes(_change_second_amount)
    body = _json(client.post(shared + f"/statements/{stmt_id}/reparse", headers=auth,
                              files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert body["rows"] == 5, body
    assert body["refreshed"] == 1, body
    assert body["carried"] == 3, body
    after = _rows(shared, client, auth, stmt_id)
    changed = next(row for row in after if row["id"] == target["id"])
    assert changed["amount"] == "99999999.00", changed
    # 归属一点没动。
    assert changed["alloc"]["book_id"] == target["alloc"]["book_id"]
    assert changed["alloc"]["id"] == target["alloc"]["id"]
    assert changed["alloc"]["status"] == "ALLOCATED"
    # 其他行也没动。
    assert [row["id"] for row in after] == [row["id"] for row in before]


def test_行数变了就不结转归属(shared, client, auth, loaded):
    """行数变了说明「第几行」这个锚本身动了 —— 按位置结转会把归属挂到别的
    行上，这时宁可拒绝，也不要让钱记到别的账上。
    """
    stmt_id = loaded["stmt_id"]
    data = _sample_bytes(_drop_last_row)
    response = client.post(shared + f"/statements/{stmt_id}/reparse", headers=auth,
                           files={"files": (SAMPLE, data, "application/octet-stream")})
    assert response.status_code == 422, response.text
    assert "行数" in response.json()["detail"]
    # 库里没动。
    after = _rows(shared, client, auth, stmt_id)
    assert len(after) == 5
    assert sum(1 for row in after if row["alloc"] is not None) == 3


def test_没归属的行按新解析刷新(shared, client, auth, loaded):
    """重新解析就是为了改内容：没归属的行按新解析刷新，已归属的行归属不动。"""
    stmt_id = loaded["stmt_id"]
    before = _rows(shared, client, auth, stmt_id)
    assert [row["alloc"] is None for row in before] == [True, False, False, False, True]
    data = _sample_bytes(_change_first_amount)
    body = _json(client.post(shared + f"/statements/{stmt_id}/reparse", headers=auth,
                              files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert body["rows"] == 5, body
    assert body["refreshed"] == 1, body
    assert body["unchanged"] == 4, body
    after = _rows(shared, client, auth, stmt_id)
    # 第 1 行（没归属的那行）金额按新解析改了。
    assert after[0]["amount"] == "9999.00"
    assert after[0]["alloc"] is None
    # 已归属的三行：行 id 不变、归属不变。
    for old, new in zip(before[1:4], after[1:4]):
        assert old["id"] == new["id"]
        assert old["alloc"] == new["alloc"]
    # 对账单上的合计也跟着新解析走（不然表头与流水就对不上了）。
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    assert detail["expense_total"] == "30010749.00"


# ---------------------------------------------------------------------------
# 5. 按金额初筛归属（1.3.0）：拿账上已经记过的同额分录去认领流水行
# ---------------------------------------------------------------------------
#
# 1.2.0 起「归属规则自动判定」整块下线了 —— 每一行都靠人工指定。1.3.0 加回来
# 一条**窄**的路：拿**账上已经记过的同额分录**去认领流水行。三档结果：
#
# * **命中**（日期差在主窗口 7 天内，且**只有一本账像**）→ 自动归属，
#   状态写成 ``PRESCREENED``（区别于人工指定的 ``ALLOCATED``）；
# * **待复核**（两本账都像 / 金额对得上但日期差超过 7 天）→ 报给用户，
#   **不替他定**；
# * **没配上**（账上压根没有这笔钱）→ 留在队列里由人工指定（``unmatched``）。
#
# 这条路有三条不许越过的线：
#
# * **不盖掉人工指定的归属**（人工的决定优先）；
# * **不抢走人工指定过的行占着的分录**（一对一 —— 宿主 ``allocate`` 那把尺子）；
# * **只读账套接不住归属**（归属要落到那本账的表里，只读的接不住）。
#
# 初筛靠两条**新的宿主能力**：``ctx.entry_match``（宿主那把尺子）与
# ``ctx.export_xlsx``（宿主生成 .xlsx）。它们按权限开关（``entry:read`` /
# ``export:write``）—— 本插件的 manifest 已经声明了，所以真实安装路径上可用。


@pytest.fixture
def prescreen_ready(shared, client, auth, books3, cash_subjects):
    """一张卡、三个成员账套、一份刚上传的对账单 —— **一行都还没归属**。

    初筛**不主动跑**（用户点了才跑），所以这里停在「刚上传」这一步，
    好让每条用例自己决定账上记了什么。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["rows"] == 5, uploaded
    stmt_id = uploaded["statement_id"]
    return {"card_id": card_id, "stmt_id": stmt_id,
            "storage_book_id": uploaded["book_id"],
            "rows": _rows(shared, client, auth, stmt_id)}


def _seq_row(rows, seq):
    """按行号取一行流水（样例的行号是 1~5）。"""
    return next(row for row in rows if int(row["seq"]) == seq)


def _record_cash(maker, item, row, when):
    """在某本账的**现金科目**上记一笔与某行流水同额同向的分录。

    **方向是反的**：银行账 IN（收钱）= 会计现金**借方**；OUT（付钱）= 现金
    **贷方** —— 这是宿主 ``allocate.CASH_DIRECTION`` 那张表。写反了，初筛
    永远配不上（而且症状很像「账上压根没记」）。
    """
    value = Decimal(str(row["amount"]))
    if row["direction"] == "IN":
        return maker(when, dr=[(item["cash"].code, value, "记卡上的收款")],
                     cr=[(item["counter"].code, value, "记卡上的收款")])
    assert row["direction"] == "OUT", row
    return maker(when, dr=[(item["counter"].code, value, "记卡上的支出")],
                 cr=[(item["cash"].code, value, "记卡上的支出")])


@pytest.fixture
def continuous_case(shared, client, auth, db, admin_user, loaded, cash_subjects):
    row = _seq_row(_rows(shared, client, auth, loaded["stmt_id"]), 4)
    item = next((item for item in cash_subjects if item['book'].id == (row.get('alloc') or {}).get('book_id')), cash_subjects[0])
    maker = build_voucher_factory(db, item["book"], admin_user)
    voucher = _record_cash(maker, item, row, "2026-08-31")
    path = shared + f'/cards/{loaded["card_id"]}'
    workspace = _json(client.get(path + '/workspace', headers=auth))
    tx = next(t for t in workspace['transactions'] if any(s['row_id'] == row['id'] for s in t['sources']))
    entry = next(e for e in workspace['entries'] if e['voucher_id'] == voucher.id)
    bank_keys = ('id','card_id','tx_date','tx_time','direction','amount_cents','balance_cents')
    entry_keys = ('id','book_id','voucher_id','voucher_no','voucher_date','period','subject_code','amount_cents','direction','summary','voucher_status')
    payload = {'bank': [tx['id']], 'entries': [entry['id']],
               'bank_snapshot': [{k: tx.get(k) for k in bank_keys}],
               'entry_snapshot': [{k: entry.get(k) for k in entry_keys}]}
    return path, workspace, tx, entry, payload


def test_连续资金线使用宿主余额及真实分录标识(continuous_case, db):
    path, workspace, tx, entry, payload = continuous_case
    from app.models.accounting import Entry
    actual = db.get(Entry, entry['id'])
    assert actual is not None and actual.voucher_id == entry['voucher_id']
    assert entry['reference_ok'] is True
    assert entry['period'] == '2026-08' and tx['tx_date'].startswith('2026-09')
    assert workspace['ledger']['summary']['chain_ok'], workspace['ledger']
    assert entry['book_balance_cents'] == -entry['amount_cents']
    assert len(workspace['transactions']) == 5


def test_核对组持久保存重复占用整组拒绝(continuous_case, client, auth, db):
    path, workspace, tx, entry, payload = continuous_case
    body = _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    assert -(2**53) < body['id'] < 0
    fresh = _json(client.get(path + '/workspace', headers=auth))
    assert len(fresh['groups']) == 1 and not fresh['groups'][0]['stale']
    response = client.post(path + '/reconciliations', headers=auth, json=payload)
    assert response.status_code == 409, response.text
    assert db.scalar(text("SELECT count(*) FROM plugin_275b391c351a_recon WHERE kind='GROUP'")) == 1
    assert db.scalar(text("SELECT count(*) FROM plugin_275b391c351a_recon WHERE kind='CLAIM'")) == len(tx['sources']) + 1


def test_撤销原子释放占用并保留历史(continuous_case, client, auth, db):
    path, workspace, tx, entry, payload = continuous_case
    group = _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    _json(client.delete(path + '/reconciliations/' + str(group['id']), headers=auth))
    fresh = _json(client.get(path + '/workspace', headers=auth))
    assert fresh['groups'] == [] and len(fresh['history']) == 1
    assert fresh['history'][0]['active'] is False
    assert db.scalar(text("SELECT count(*) FROM plugin_275b391c351a_recon WHERE kind='CLAIM'")) == 0
    _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    assert len(_json(client.get(path + '/workspace', headers=auth))['history']) == 2


def test_确认拒绝过时快照不写半组(continuous_case, client, auth, db):
    path, workspace, tx, entry, payload = continuous_case
    payload['entry_snapshot'][0]['summary'] = '过期摘要'
    response = client.post(path + '/reconciliations', headers=auth, json=payload)
    assert response.status_code == 409, response.text
    assert db.scalar(text("SELECT count(*) FROM plugin_275b391c351a_recon")) == 0


def test_核对拒绝非法标识不写入(continuous_case, client, auth, db):
    path, workspace, tx, entry, payload = continuous_case
    payload['bank'] = [{'invalid': True}]
    response = client.post(path + '/reconciliations', headers=auth, json=payload)
    assert response.status_code == 422, response.text
    assert db.scalar(text("SELECT count(*) FROM plugin_275b391c351a_recon")) == 0


def test_分录变化保留核对并提示复核(continuous_case, client, auth, db):
    path, workspace, tx, entry, payload = continuous_case
    _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    db.execute(text('UPDATE entry SET summary=:summary WHERE id=:id'),
               {'summary': '确认后修订摘要', 'id': entry['id']})
    db.commit()
    fresh = _json(client.get(path + '/workspace', headers=auth))
    assert len(fresh['groups']) == 1 and fresh['groups'][0]['stale'] is True
    assert fresh['groups'][0]['entry_snapshot'][0]['summary'] == entry['summary']
    assert db.scalar(text("SELECT count(*) FROM plugin_275b391c351a_recon WHERE kind='CLAIM'")) > 0


def test_有效核对原件不能删除或重解析(continuous_case, client, auth, loaded):
    path, workspace, tx, entry, payload = continuous_case
    _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    stmt_path = path.split('/cards/')[0] + '/statements/' + str(loaded['stmt_id'])
    response = client.delete(stmt_path, headers=auth)
    assert response.status_code == 409, response.text
    response = client.post(stmt_path + '/reparse', headers=auth,
                           files={'files': (SAMPLE, (FIXTURES / SAMPLE).read_bytes())})
    assert response.status_code == 409, response.text


def test_归属修订保存且保留旧证据(continuous_case, client, auth):
    path, workspace, tx, entry, payload = continuous_case
    _json(client.post(path + '/ownership', headers=auth, json={'tx_id':tx['id'],'book_id':entry['book_id']}))
    fresh = _json(client.get(path + '/workspace', headers=auth))
    changed = next(t for t in fresh['transactions'] if t['id'] == tx['id'])
    assert changed['owner_book_id'] == entry['book_id'] and changed['ownership'] == 'MANUAL'
    assert changed['allocation_evidence'] == tx['allocation_evidence']


def test_账上同额分录命中_自动归属(shared, client, auth, db, admin_user,
                                   prescreen_ready, cash_subjects):
    """一笔 750 的支出已经记在第一本账的银行存款上 → 这一行就归第一本账。

    这是「按金额初筛」唯一敢自动落库的一档：**证据是账本身**，不是猜。
    而且落库的归属状态是 ``PRESCREENED`` —— 一眼能看出它是机器认领的，
    不是人指定的（撤销时也只撤这一种）。
    """
    stmt_id = prescreen_ready["stmt_id"]
    row = _seq_row(prescreen_ready["rows"], 4)          # 750.00 / OUT / 2026-09-14
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    voucher = _record_cash(maker, item, row, "2026-09-14")   # 同日、同额、同向

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 1, body
    assert body["review"] == 0, body
    assert body["unmatched"] == 4, body
    assert isinstance(body["note"], str)

    rows = _rows(shared, client, auth, stmt_id)
    hit = _seq_row(rows, 4)
    assert hit["alloc"] is not None
    assert hit["alloc"]["book_id"] == item["book"].id
    assert hit["alloc"]["status"] == "PRESCREENED"
    # 归属依据里带着**证据**（哪张凭证、差几天）—— 不是一句「自动」。
    assert "初筛" in hit["alloc"]["reason"]
    assert voucher.voucher_no in hit["alloc"]["reason"]
    # 其余四行**一行都没被动过**。
    assert [row_item["alloc"] for row_item in rows if int(row_item["seq"]) != 4] == [None] * 4


def test_两本账都有同额分录_不自动归属_报出来待复核(shared, client, auth, db, admin_user,
                                                   prescreen_ready, cash_subjects):
    """两本账上都有同额同向的分录 → **不替他定**，报给用户去判。

    这正是「初筛」与「规则自动判定」的分界：规则是猜，账上的分录是证据 ——
    但**两份证据打架**时就没有证据了，只能问人。
    """
    stmt_id = prescreen_ready["stmt_id"]
    row = _seq_row(prescreen_ready["rows"], 4)          # 750.00 / OUT / 2026-09-14
    for item in cash_subjects[:2]:
        maker = build_voucher_factory(db, item["book"], admin_user)
        _record_cash(maker, item, row, "2026-09-14")

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 1, body
    assert body["unmatched"] == 4, body
    assert len(body["review_rows"]) == 1, body

    review = body["review_rows"][0]
    assert review["row_id"] == row["id"]
    assert review["seq"] == 4
    assert review["tx_date"] == "2026-09-14"
    assert review["amount"] == "750.00"
    assert review["direction"] == "OUT"
    assert "同额" in review["why"], review["why"]
    # 候选：两本账、两张凭证，日期差都在主窗口内。
    assert len(review["candidates"]) == 2, review["candidates"]
    assert {item["book_id"] for item in review["candidates"]} == {
        cash_subjects[0]["book"].id, cash_subjects[1]["book"].id}
    assert all(item["in_primary"] for item in review["candidates"])
    assert all(item["days_diff"] == 0 for item in review["candidates"])
    assert all(item["amount"] == "750.00" for item in review["candidates"])
    # **没有自动归属**：这一行还空着，等人定。
    assert _seq_row(_rows(shared, client, auth, stmt_id), 4)["alloc"] is None


def test_日期差超过7天_待复核而不是自动归属(shared, client, auth, db, admin_user,
                                            prescreen_ready, cash_subjects):
    """金额对得上、日期差 11 天（还在 30 天窗口内）→ **待复核**，不自动归属。

    7 天是宿主那条线的「已入账」窗口（``allocate.PRIMARY_WINDOW_DAYS``）——
    差得更多就不一定是同一笔了，替用户认定「差 11 天也是同一笔」就是替他记账。
    """
    stmt_id = prescreen_ready["stmt_id"]
    row = _seq_row(prescreen_ready["rows"], 5)          # 7401.00 / IN / 2026-09-21
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, row, "2026-09-10")        # 差 11 天

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 1, body
    assert body["unmatched"] == 4, body

    review = body["review_rows"][0]
    assert review["row_id"] == row["id"]
    assert "日期差" in review["why"], review["why"]
    assert len(review["candidates"]) == 1, review["candidates"]
    candidate = review["candidates"][0]
    assert candidate["days_diff"] == 11
    assert candidate["in_primary"] is False
    # **没有自动归属**。
    assert _seq_row(_rows(shared, client, auth, stmt_id), 5)["alloc"] is None


def test_账上没有这笔钱_不归属_计入未匹配(shared, client, auth, prescreen_ready):
    """账上压根没有这笔钱 → 不归属，计入 ``unmatched``（留在队列里等人工指定）。

    **不能算「待复核」**：没有候选就没有「疑似」可言 —— 报一条疑似出来，
    只会让用户去翻一个根本不存在的东西。
    """
    stmt_id = prescreen_ready["stmt_id"]
    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 0, body
    assert body["unmatched"] == 5, body
    assert body["review_rows"] == [], body
    rows = _rows(shared, client, auth, stmt_id)
    assert all(row["alloc"] is None for row in rows)


def test_初筛不覆盖人工指定的归属(shared, client, auth, db, admin_user,
                                  prescreen_ready, cash_subjects):
    """人工指定过的行**原样保留** —— 初筛只动还没归属的行。

    这里故意在**另一本账**上放一笔同额同向的分录：初筛要是敢覆盖人工指定的
    归属，钱就会被挪到另一本账上 —— 那是「猜」盖过「人做的决定」。
    """
    stmt_id = prescreen_ready["stmt_id"]
    manual = _seq_row(prescreen_ready["rows"], 2)       # 100000000.00 / IN / 2026-09-14
    body = _json(client.post(shared + f"/statements/{stmt_id}/assign", headers=auth, json={
        "rows": [manual["id"]], "book_id": cash_subjects[1]["book"].id,
        "reason": "人工指定：丙供应链"}))
    assert body["assigned"] == 1

    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, manual, "2026-09-16")     # 第一本账上也有同额同向

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    # 这一行已经归属了 → 不在待初筛的行里，也就不该被任何一方改写。
    assert body["assigned"] == 0, body
    assert body["review"] == 0, body
    assert body["unmatched"] == 4, body

    after = _seq_row(_rows(shared, client, auth, stmt_id), 2)
    assert after["alloc"]["status"] == "ALLOCATED"
    assert after["alloc"]["book_id"] == cash_subjects[1]["book"].id
    assert after["alloc"]["reason"] == "人工指定：丙供应链"


def _make_row1_same_amount_as_row4(sheet):
    """把第 1 行流水的金额改成 750 —— 与第 4 行**同额同向**。

    两行同额才谈得上「抢」：同一笔分录能同时像两行，一对一分配才有得争。
    """
    sheet.cell(row=19, column=3).value = 750


def test_人工指定过的行占着的分录不会被别的行抢走(shared, client, auth, db, admin_user,
                                                  books3, cash_subjects):
    """**一对一**：人工指定过的那一行占着的分录，别的行不许抢。

    一笔账就是一笔钱。对账单上两行同额同向时，账上那一笔只配其中一行 ——
    配错了等于「甲流水的钱记到了乙头上」。

    这里把样例第 1 行的金额改成 750（与第 4 行同额同向），再：
    第 1 行人工指定给第一本账；第一本账上有一笔 750 的分录（贴着第 1 行）。
    若初筛只把**没归属的行**拿去配对，第 4 行就会抢走那笔分录、被自动归属；
    正确做法是这一笔分录归第 1 行（它在配对集合里），第 4 行**配不上**。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects)
    data = _sample_bytes(_make_row1_same_amount_as_row4)
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["rows"] == 5, uploaded
    stmt_id = uploaded["statement_id"]
    rows = _rows(shared, client, auth, stmt_id)
    first = _seq_row(rows, 1)
    fourth = _seq_row(rows, 4)
    assert first["amount"] == fourth["amount"] == "750.00", (first, fourth)
    assert first["direction"] == fourth["direction"] == "OUT"

    # 第 1 行人工指定给第一本账。
    _json(client.post(shared + f"/statements/{stmt_id}/assign", headers=auth, json={
        "rows": [first["id"]], "book_id": cash_subjects[0]["book"].id,
        "reason": "人工指定：乙实业"}))

    # 第一本账上记一笔 750 的分录，贴着第 1 行（差 0 天；离第 4 行差 2 天）。
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, first, "2026-09-12")

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 0, body
    assert body["unmatched"] == 4, body
    after = _rows(shared, client, auth, stmt_id)
    # **分录没被抢走**：第 4 行配不上，仍然空着（而不是被自动归属给第一本账）。
    assert _seq_row(after, 4)["alloc"] is None, \
        "人工指定过的行占着的分录被别的行抢走了"
    # 人工指定的那一行原样保留。
    kept = _seq_row(after, 1)
    assert kept["alloc"]["status"] == "ALLOCATED"
    assert kept["alloc"]["book_id"] == cash_subjects[0]["book"].id
    assert kept["alloc"]["reason"] == "人工指定：乙实业"


def test_撤销初筛只撤机器认领的行(shared, client, auth, db, admin_user,
                                   prescreen_ready, cash_subjects):
    """撤销**只撤初筛**：人工指定的归属原样保留。

    初筛是机器按证据认领的 —— 认错了用户要有退路，而且是一键退路；
    但退路只该退掉机器做的那部分，人自己指定的不该被顺带抹掉。
    """
    stmt_id = prescreen_ready["stmt_id"]
    manual = _seq_row(prescreen_ready["rows"], 2)
    _json(client.post(shared + f"/statements/{stmt_id}/assign", headers=auth, json={
        "rows": [manual["id"]], "book_id": cash_subjects[1]["book"].id,
        "reason": "人工指定：丙供应链"}))
    hit_row = _seq_row(prescreen_ready["rows"], 4)
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, hit_row, "2026-09-14")
    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 1, body

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen/undo", headers=auth))
    assert body["removed"] == 1, body
    rows = _rows(shared, client, auth, stmt_id)
    # 初筛认领的那一行回到「还没归属」。
    assert _seq_row(rows, 4)["alloc"] is None
    # 人工指定的那一行**原样保留**。
    kept = _seq_row(rows, 2)
    assert kept["alloc"]["status"] == "ALLOCATED"
    assert kept["alloc"]["book_id"] == cash_subjects[1]["book"].id
    assert kept["alloc"]["reason"] == "人工指定：丙供应链"
    # 再撤一次：已经没有初筛的行可撤了（幂等，不报错）。
    again = _json(client.post(shared + f"/statements/{stmt_id}/prescreen/undo", headers=auth))
    assert again["removed"] == 0, again


def test_导出是能打开的xlsx_金额列是数字(shared, client, auth, db, admin_user,
                                          prescreen_ready, cash_subjects):
    """导出是给**出纳**填的表：能打开、金额能直接求和、两列留空给它填。

    * 表头得说清每列是干什么的（「归属账套」「归属依据」「初筛候选（疑似）」
      「待出纳确认」「备注」）；
    * 行数与流水行数一致；
    * **金额列给数字，不给字符串** —— 出纳要拿它求和，给文本他就得先改格式。
    """
    import base64
    import io

    import openpyxl

    stmt_id = prescreen_ready["stmt_id"]
    hit_row = _seq_row(prescreen_ready["rows"], 4)      # 750.00 / OUT / 2026-09-14
    review_row = _seq_row(prescreen_ready["rows"], 5)   # 7401.00 / IN / 2026-09-21
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, hit_row, "2026-09-14")
    # 另一行：金额对得上但日期差 11 天 → 初筛不认领它，只是「疑似」。
    _record_cash(maker, item, review_row, "2026-09-10")
    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 1 and body["review"] == 1, body

    export = _json(client.get(shared + f"/statements/{stmt_id}/export", headers=auth))
    assert export["filename"].endswith(".xlsx"), export["filename"]
    raw = base64.b64decode(export["content_base64"])
    workbook = openpyxl.load_workbook(io.BytesIO(raw))
    sheet = workbook.active
    titles = [cell.value for cell in sheet[1]]
    for want in ("归属账套", "归属依据", "初筛候选（疑似）", "待出纳确认", "备注"):
        assert want in titles, titles
    # 行数 = 表头 + 5 行流水。
    assert sheet.max_row == 6, sheet.max_row

    # 金额列是**数字**。
    amount_col = titles.index("金额") + 1
    amounts = [sheet.cell(row=r, column=amount_col).value for r in range(2, 7)]
    assert all(isinstance(value, (int, float)) for value in amounts), amounts
    expected = sum(Decimal(row["amount"]) for row in prescreen_ready["rows"])
    assert sum(Decimal(str(value)) for value in amounts) == expected, amounts

    # 归属账套 / 归属依据：认领到的那一行有，其余行空着。
    book_col = titles.index("归属账套") + 1
    reason_col = titles.index("归属依据") + 1
    hint_col = titles.index("初筛候选（疑似）") + 1
    confirm_col = titles.index("待出纳确认") + 1
    note_col = titles.index("备注") + 1
    assert sheet.cell(row=5, column=book_col).value == cash_subjects[0]["book"].name
    assert "初筛" in str(sheet.cell(row=5, column=reason_col).value)
    assert sheet.cell(row=2, column=book_col).value in (None, "")
    # 待复核的那一行：**没有归属，但有疑似候选**。
    assert sheet.cell(row=6, column=book_col).value in (None, "")
    hint = str(sheet.cell(row=6, column=hint_col).value)
    assert cash_subjects[0]["book"].name in hint, hint
    assert "11 天" in hint, hint
    # 「待出纳确认」「备注」两列**留空**给出纳填。
    for row in range(2, 7):
        assert sheet.cell(row=row, column=confirm_col).value in (None, "")
        assert sheet.cell(row=row, column=note_col).value in (None, "")


def test_对账单详情带出这张卡的成员账套(shared, client, auth, prescreen_ready,
                                        cash_subjects):
    """详情里带上**这张卡的成员账套**（含各本账的现金科目）。

    「归属到哪本账」的选项得是「做得到的事」—— 只能列这张卡登记在名下的
    账套，且每本账带上它自己的现金科目编码。
    """
    stmt_id = prescreen_ready["stmt_id"]
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    members = detail["members"]
    assert len(members) == 3, members
    assert {item["book_id"] for item in members} == {
        item["book"].id for item in cash_subjects}
    for member in members:
        assert member["book_name"]
        assert member["subject_code"] in (
            "100203", "100206", "100101"), member
        assert member["subject_name"]
        assert member["can_write"] is True


# ---------------------------------------------------------------------------
# 6. 反向测试：闸门得有牙（**不该发生的事真的没发生**）
# ---------------------------------------------------------------------------


def test_日期差超过30天_连候选都不该有(shared, client, auth, db, admin_user,
                                         prescreen_ready, cash_subjects):
    """差 32 天（超出 30 天窗口）→ **连候选都不该有**，落进 ``unmatched``。

    这一条比「不自动归属」更严：出现在待复核里也不行 —— 那会把用户引到
    一个根本不是同一笔的记录上（每月固定金额的房租、货款最容易撞）。

    **带一个对照组**：同样在 2026-08-20 记账，另一行（差 23 天）**能**配上
    候选。所以挡掉差 32 天那笔的正是宿主那把尺子的 30 天上限，而不是
    「压根没查到那一段日期」。
    """
    stmt_id = prescreen_ready["stmt_id"]
    near = _seq_row(prescreen_ready["rows"], 1)         # 9780.00 / OUT / 2026-09-12
    far = _seq_row(prescreen_ready["rows"], 5)          # 7401.00 / IN / 2026-09-21
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, near, "2026-08-20")        # 差 23 天 → 是候选
    _record_cash(maker, item, far, "2026-08-20")         # 差 32 天 → 不是候选

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 1, body
    assert body["unmatched"] == 4, body
    # 待复核的**只有**差 23 天那行；差 32 天那行连候选都没有。
    assert [item["row_id"] for item in body["review_rows"]] == [near["id"]], body
    candidate = body["review_rows"][0]["candidates"][0]
    assert candidate["days_diff"] == 23
    assert candidate["in_primary"] is False
    rows = _rows(shared, client, auth, stmt_id)
    assert _seq_row(rows, 5)["alloc"] is None
    assert _seq_row(rows, 1)["alloc"] is None


def test_只读的账套不能接收初筛归属(shared, client, auth, db, admin_user,
                                     prescreen_ready, cash_subjects):
    """成员账套变成只读之后 → 初筛**不能**往它那儿归属。

    只读的账套接不住归属（人工指定时也是这么挡的）。但它账上的分录**照样是
    证据** —— 所以那一行**进待复核**（唯一像的那本账是只读的，归属落不进去），
    **不是**被静悄悄地归到别的账去，也不是当没这回事。

    曾经这里是「连候选都不问」：只读书的证据被藏起来，于是剩下的那份证据成了
    「唯一」证据，行就被自动归走了。那正是「初筛不猜错」这条主线要避免的事。

    这本账是「登记成成员之后才被降成只读的」—— 成员登记时是可写的，
    这才造得出「只读的成员账套」这个状态。
    """
    stmt_id = prescreen_ready["stmt_id"]
    # 选一本**不是**存放对账单那本账的成员账套（存放位置由后端挑，随机）。
    readonly_item = next(item for item in cash_subjects
                         if item["book"].id != prescreen_ready["storage_book_id"])
    row = _seq_row(prescreen_ready["rows"], 4)
    maker = build_voucher_factory(db, readonly_item["book"], admin_user)
    _record_cash(maker, readonly_item, row, "2026-09-14")

    # 把它降成只读。
    link = db.scalar(select(UserBook).where(
        UserBook.book_id == readonly_item["book"].id,
        UserBook.user_id == admin_user.id))
    assert link is not None
    link.role = "VIEWER"
    db.commit()

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    # **一行都没被自动归属** —— 唯一像的那本账是只读的。
    assert body["assigned"] == 0, body
    # 那一行**进待复核**，证据摆出来（只读那本 + 凭证号）。
    assert body["review"] == 1, body
    review = next(item for item in body["review_rows"] if item["row_id"] == row["id"])
    assert [item["book_id"] for item in review["candidates"]] == [readonly_item["book"].id], review
    assert review["candidates"][0]["can_write"] is False, review
    assert "只读" in review["why"], review
    rows = _rows(shared, client, auth, stmt_id)
    assert all(item["alloc"] is None for item in rows)
    assert not any(item["alloc"] and item["alloc"]["book_id"] == readonly_item["book"].id
                   for item in rows)
    # 详情里也要标出它现在是只读的（界面上就不该把它列成能归属的账套）。
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    readonly = next(item for item in detail["members"]
                    if item["book_id"] == readonly_item["book"].id)
    assert readonly["can_write"] is False, readonly


def test_只读书上的证据照样参与_行进待复核而不是被静悄悄归走(shared, client, auth, db, admin_user,
                                                        prescreen_ready, cash_subjects):
    """只读成员账套**不能接收归属**，但它账上的分录**照样是证据**。

    这里那笔 750 的分录有两份：一份在只读账上，一份在可写账上。

    曾经只读书被**整本跳过**（问都不问）—— 它的证据被藏起来，于是可写账那份
    成了「唯一」的强候选，那一行被**静悄悄地归到了可写账**，而它真正的来处是
    那本只读的账。那正是「初筛不猜错」这条主线要避免的事。

    现在的口径：**证据照常收，归属落不到只读账套那儿** → 这一行进**待复核**，
    两个候选都摆出来，由人定。
    """
    stmt_id = prescreen_ready["stmt_id"]
    readonly_item = next(item for item in cash_subjects
                         if item["book"].id != prescreen_ready["storage_book_id"])
    writable_item = next(item for item in cash_subjects
                         if item["book"].id != readonly_item["book"].id)
    row = _seq_row(prescreen_ready["rows"], 4)          # 750.00 / OUT / 2026-09-14
    # 两本账上各有一笔 750 的分录。
    for item in (readonly_item, writable_item):
        maker = build_voucher_factory(db, item["book"], admin_user)
        _record_cash(maker, item, row, "2026-09-14")
    # 把只读那本降成只读。
    link = db.scalar(select(UserBook).where(
        UserBook.book_id == readonly_item["book"].id,
        UserBook.user_id == admin_user.id))
    assert link is not None
    link.role = "VIEWER"
    db.commit()

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    # **不许自动归属**：两份证据都在，两个候选都摆出来让人判。
    assert body["assigned"] == 0, body
    assert body["review"] == 1, body
    review = next(item for item in body["review_rows"] if item["row_id"] == row["id"])
    assert {item["book_id"] for item in review["candidates"]} == {
        readonly_item["book"].id, writable_item["book"].id}, review
    # 只读那本**不接收归属**（人工指定时也是这么挡的），但它的证据仍然摆出来。
    readonly_candidate = next(item for item in review["candidates"]
                              if item["book_id"] == readonly_item["book"].id)
    assert readonly_candidate["can_write"] is False, review
    after = _seq_row(_rows(shared, client, auth, stmt_id), 4)
    assert after["alloc"] is None, after


# ---------------------------------------------------------------------------
# 7. 匹配月份范围（1.4.0）：「9 月的对账单去对 8 月的账」
# ---------------------------------------------------------------------------
#
# 流水日期是**银行记账日**，记账日期是**实际发生日** —— 两者差一个月是常事
# （8 月的费用 9 月初才从卡上扣，账就记在 8 月）。1.3.0 的候选池是「流水行
# 日期 ± 30 天」，跨月错位**够不着**：9 月 14 日的流水配 8 月 5 日的凭证差
# 40 天，连候选都不会有 —— 看上去就成了「账上压根没记这笔」。
#
# 1.4.0 起，用户可以**自己指定这份对账单去匹配哪几个月**。这一组用例盯的
# 是三件事：
#
# * 不指定时，老行为**一字不变**（别把 1.3.0 悄悄改了）；
# * 指定了，跨月的分录就配上了，而且**范围是排他的**（说 8 月就只扫 8 月）；
# * 余额核对取账上发生额时，也按**同一段账**（两边不对着同一段比，差额是
#   假的）。


def _set_match_months(shared, client, auth, stmt_id, months):
    return _json(client.patch(shared + f"/statements/{stmt_id}/match-months",
                              headers=auth, json={"months": months}))


def test_没指定匹配月份范围时_跨月的分录连候选都没有(shared, client, auth, db, admin_user,
                                                  prescreen_ready, cash_subjects):
    """**老行为不变**：窗口是「流水行日期 ± 30 天」，差 40 天压根不是候选。

    这一条是「别把老行为悄悄改了」的钉子 —— 1.3.0 的用户看到的必须是原来
    那个样子。
    """
    stmt_id = prescreen_ready["stmt_id"]
    row = _seq_row(prescreen_ready["rows"], 4)          # 750.00 / OUT / 2026-09-14
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, row, "2026-08-05")        # 差 40 天：记账在上个月

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 0, body
    assert body["unmatched"] == 5, body
    assert body["months"] == ["2026-09"], body


def test_指定匹配月份范围后_跨月的分录配上了(shared, client, auth, db, admin_user,
                                              prescreen_ready, cash_subjects):
    """说「这份 9 月的对账单去对 8 月的账」→ 8 月记的那笔就配上了。

    **为什么这行能自动归属**：用户已经**明确说了**这份对账单对应哪几个月 ——
    跨月这件事本来就是他定的（宿主那把尺子的 7 天窗口是给「同月」这件事
    定的，它够不着「跨月」，不是它判「不是同一笔」）。证据还是账本身：
    同额、同向、只有一本账像。

    **但跨月的行要报个数**：同额同向的候选在 ±60 天有 4 条（宿主自己量过），
    多看一眼比事后发现错账便宜。
    """
    stmt_id = prescreen_ready["stmt_id"]
    row = _seq_row(prescreen_ready["rows"], 4)          # 750.00 / OUT / 2026-09-14
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, row, "2026-08-05")        # 差 40 天：记账在上个月

    body = _set_match_months(shared, client, auth, stmt_id, ["2026-08"])
    assert body["match_months"] == ["2026-08"], body
    assert body["match_months_effective"] == ["2026-08"], body
    assert body["statement_months"] == ["2026-09"], body

    screen = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert screen["assigned"] == 1, screen
    assert screen["review"] == 0, screen
    assert screen["unmatched"] == 4, screen
    assert screen["months"] == ["2026-08"], screen
    # **跨月对上的行要报个数出来**。
    assert screen["cross_month"] == 1, screen

    rows = _rows(shared, client, auth, stmt_id)
    hit = _seq_row(rows, 4)
    assert hit["alloc"] is not None
    assert hit["alloc"]["book_id"] == item["book"].id
    assert hit["alloc"]["status"] == "PRESCREENED"
    # 归属依据里带着证据（哪张凭证、差几天）。
    assert "初筛" in hit["alloc"]["reason"]


def test_匹配月份范围是排他的_声明之外的月份不参与(shared, client, auth, db, admin_user,
                                                 prescreen_ready, cash_subjects):
    """说「去匹配 8 月」就是**只**匹配 8 月 —— 9 月账上那笔不该来抢同一行。

    不排他的话，8 月与 9 月的同额分录会互相抢同一行流水，结论就变成「两本
    账都像」—— 而用户明明已经指了对手方。
    """
    stmt_id = prescreen_ready["stmt_id"]
    row = _seq_row(prescreen_ready["rows"], 4)          # 750.00 / OUT / 2026-09-14
    item = cash_subjects[0]
    maker = build_voucher_factory(db, item["book"], admin_user)
    _record_cash(maker, item, row, "2026-09-20")        # 同月，但不在声明的范围里
    _set_match_months(shared, client, auth, stmt_id, ["2026-08"])

    body = _json(client.post(shared + f"/statements/{stmt_id}/prescreen", headers=auth))
    assert body["assigned"] == 0, body
    assert body["review"] == 0, body
    assert body["unmatched"] == 5, body


def test_匹配月份范围可以改回默认(shared, client, auth, prescreen_ready):
    """声明是**加法**，不是必填项 —— 清空就回到「跟着对账单自己的月份走」。"""
    stmt_id = prescreen_ready["stmt_id"]
    _set_match_months(shared, client, auth, stmt_id, ["2026-08"])
    body = _set_match_months(shared, client, auth, stmt_id, [])
    assert body["match_months"] == [], body
    assert body["match_months_effective"] == ["2026-09"], body
    assert body["match_months_customized"] is False, body

    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    assert detail["match_months"] == [], detail
    assert detail["match_months_effective"] == ["2026-09"], detail
    assert detail["statement_months"] == ["2026-09"], detail
    assert detail["match_months_customized"] is False, detail


def test_月份写错要当场说清(shared, client, auth, prescreen_ready):
    """月份认不出来要**当场**说 —— 存坏了的月份会让初筛一根候选都找不到，
    而用户会以为「账上真没记」。"""
    stmt_id = prescreen_ready["stmt_id"]
    response = client.patch(shared + f"/statements/{stmt_id}/match-months", headers=auth,
                            json={"months": ["去年"]})
    assert response.status_code == 422, response.text
    assert "月份" in response.json()["detail"], response.text


def test_余额核对按声明的月份取账上的发生额(shared, client, auth, db, admin_user,
                                            loaded, cash_subjects):
    """9 月的对账单去对 8 月的账 → 账上取的是**8 月**的发生额。

    **两边必须对着同一段账**：账上取 9 月、流水取 9 月的对账单，差额里混着
    的就是「错位」本身，而不是「少记」。
    """
    stmt_id = loaded["stmt_id"]
    card_id = loaded["card_id"]
    _assign_all(shared, client, auth, stmt_id, cash_subjects[0]["book"].id)
    first = cash_subjects[0]
    maker = build_voucher_factory(db, first["book"], admin_user)
    amount = Decimal("50000000.00")
    maker("2026-08-15", dr=[(first["cash"].code, amount, "记卡上的收款")],
          cr=[(first["counter"].code, amount, "记卡上的收款")])

    body = _json(client.get(shared + f"/cards/{card_id}/balance", headers=auth,
                            params={"stmt_id": stmt_id}))
    assert body["months"] == ["2026-09"], body
    row = next(item for item in body["books"] if item["book_id"] == first["book"].id)
    assert _dec(row["book_debit"]) == 0, row          # 默认取 9 月 → 8 月那笔不在里头

    _set_match_months(shared, client, auth, stmt_id, ["2026-08"])
    body = _json(client.get(shared + f"/cards/{card_id}/balance", headers=auth,
                            params={"stmt_id": stmt_id}))
    assert body["months"] == ["2026-08"], body
    row = next(item for item in body["books"] if item["book_id"] == first["book"].id)
    assert _dec(row["book_debit"]) == amount, row     # 取 8 月 → 那笔收款就在里头


def test_导出账面明细账(shared, client, auth, db, admin_user, loaded, cash_subjects):
    """导出**账上那一本**明细账，但**只导逐笔**、**只导那七列** ——
    账套、期间、日期、凭证号、摘要、借方、贷方。

    期初、本月合计、累计那些**汇总行不导** —— 混在逐笔里，核账的人分不清
    哪一行是「真的一笔」，汇总行的借方加起来还会跟逐笔对不上。

    取数仍走宿主那本明细账（``detail_ledger_read``），不是插件自己拼的。
    """
    import io

    import openpyxl

    stmt_id = loaded["stmt_id"]
    first = cash_subjects[0]
    maker = build_voucher_factory(db, first["book"], admin_user)
    amount = Decimal("50000000.00")
    maker("2026-09-15", dr=[(first["cash"].code, amount, "记卡上的收款")],
          cr=[(first["counter"].code, amount, "记卡上的收款")])

    export = _json(client.get(shared + f"/cards/{loaded['card_id']}/ledger-export",
                              headers=auth, params={"stmt_id": stmt_id}))
    assert export["filename"].endswith(".xlsx"), export["filename"]
    workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(export["content_base64"])))
    sheet = workbook.active
    titles = [cell.value for cell in sheet[1]]
    # **就这七列**，一列不多一列不少。
    assert titles == ["账套", "期间", "日期", "凭证号", "摘要", "借方", "贷方"], titles

    book_col = titles.index("账套") + 1
    period_col = titles.index("期间") + 1
    date_col = titles.index("日期") + 1
    voucher_col = titles.index("凭证号") + 1
    summary_col = titles.index("摘要") + 1
    debit_col = titles.index("借方") + 1
    credit_col = titles.index("贷方") + 1

    # **只有那一笔逐笔**：另外两本账这段时间没有分录，导出里不该出现它们
    # 的期初 / 本月合计 / 累计行。
    assert sheet.max_row == 2, sheet.max_row
    summaries = [sheet.cell(row=r, column=summary_col).value
                 for r in range(2, sheet.max_row + 1)]
    for marker in ("期初余额", "本月合计", "累　　计", "本年累计"):
        assert marker not in summaries, summaries

    # 那笔 50 万的收款在表里，借方 50 万。
    assert sheet.cell(row=2, column=book_col).value == first["book"].name
    assert sheet.cell(row=2, column=period_col).value == "2026-09"
    assert sheet.cell(row=2, column=date_col).value == "2026-09-15"
    assert str(sheet.cell(row=2, column=voucher_col).value or "") != ""
    assert "收款" in str(sheet.cell(row=2, column=summary_col).value)
    assert sheet.cell(row=2, column=debit_col).value == float(amount)
    # 这一笔是收款：借方 50 万，贷方空着（对方科目的分录不在这个科目上）。
    assert sheet.cell(row=2, column=credit_col).value in (None, 0)


def test_导出账面明细账按声明的月份(shared, client, auth, db, admin_user,
                                     loaded, cash_subjects):
    """导出的月份跟着**声明的匹配月份范围**走 —— 9 月的对账单去对 8 月的账，
    导出来的就是 8 月那本账的**逐笔**。"""
    import io

    import openpyxl

    stmt_id = loaded["stmt_id"]
    first = cash_subjects[0]
    maker = build_voucher_factory(db, first["book"], admin_user)
    amount = Decimal("50000000.00")
    maker("2026-08-15", dr=[(first["cash"].code, amount, "记卡上的收款")],
          cr=[(first["counter"].code, amount, "记卡上的收款")])
    _set_match_months(shared, client, auth, stmt_id, ["2026-08"])

    export = _json(client.get(shared + f"/cards/{loaded['card_id']}/ledger-export",
                              headers=auth, params={"stmt_id": stmt_id}))
    workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(export["content_base64"])))
    sheet = workbook.active
    titles = [cell.value for cell in sheet[1]]
    assert titles == ["账套", "期间", "日期", "凭证号", "摘要", "借方", "贷方"], titles
    period_col = titles.index("期间") + 1
    debit_col = titles.index("借方") + 1
    periods = {sheet.cell(row=r, column=period_col).value
               for r in range(2, sheet.max_row + 1)}
    assert periods == {"2026-08"}, periods
    values = [sheet.cell(row=r, column=debit_col).value for r in range(2, sheet.max_row + 1)]
    assert float(amount) in values, values


def test_老宿主不认窗口参数时退回旧口径(shared, client, auth, prescreen_ready):
    """老宿主（SDK 1.3.0 及更早）的 ``entry_match`` 不认那几个关键字参数。

    插件要**接住**它、退回老口径，**并把这件事说给用户听**（初筛的返回值里
    带一句）—— 而不是让用户以为「指定了匹配月份范围，但它没生效」。

    **只接「不认识这几个参数」这一种 ``TypeError``**：别的 ``TypeError``
    是真 bug，放过去只会把问题藏起来。
    """
    import importlib.util
    from datetime import date

    spec = importlib.util.spec_from_file_location(
        "sharedcard_backend", PLUGIN_DIR / "backend" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class OldHost:
        def __init__(self):
            self.calls = []

        def entry_match(self, book_id, subject_codes, rows):
            self.calls.append((book_id, subject_codes, len(rows)))
            return {"pairs": [], "unmatched_row_ids": []}

    old = OldHost()
    matched, applied = module._entry_match(old, 7, ["100203"], [{"id": 1}],
                                           None, None, 7, 30)
    assert applied is True, "没给窗口时不走「新参数」那条路"
    assert matched == {"pairs": [], "unmatched_row_ids": []}
    assert old.calls == [(7, ["100203"], 1)]

    class NewHost(OldHost):
        def entry_match(self, book_id, subject_codes, rows, **kwargs):
            self.last_kwargs = kwargs
            self.calls.append((book_id, subject_codes, len(rows)))
            return {"pairs": [], "unmatched_row_ids": []}

    new = NewHost()
    matched, applied = module._entry_match(new, 7, ["100203"], [{"id": 1}],
                                           date(2026, 8, 1), date(2026, 8, 31), 60, 90)
    assert applied is True
    assert new.last_kwargs["date_from"] == "2026-08-01"
    assert new.last_kwargs["date_to"] == "2026-08-31"
    assert new.last_kwargs["primary_days"] == 60
    assert new.last_kwargs["review_days"] == 90

    # **老宿主**：参数不认识 → 抛 TypeError → 接住它、退回老口径，并报
    # 「没生效」。
    class AncientHost(OldHost):
        def entry_match(self, book_id, subject_codes, rows, **kwargs):
            # **老宿主**：只认老签名，碰到新关键字参数就报错。
            if kwargs:
                raise TypeError(
                    "entry_match() got an unexpected keyword argument 'date_from'")
            self.calls.append((book_id, subject_codes, len(rows)))
            return {"pairs": [], "unmatched_row_ids": []}

    ancient = AncientHost()
    matched, applied = module._entry_match(ancient, 7, ["100203"], [{"id": 1}],
                                            date(2026, 8, 1), date(2026, 8, 31), 60, 90)
    assert applied is False, "老宿主上要报「没生效」，不能让用户以为它生效了"
    assert matched == {"pairs": [], "unmatched_row_ids": []}

    # **别的 TypeError 是真 bug**：不能一起吞掉。
    class BrokenHost(OldHost):
        def entry_match(self, book_id, subject_codes, rows, **kwargs):
            raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    with pytest.raises(TypeError):
        module._entry_match(BrokenHost(), 7, ["100203"], [{"id": 1}],
                             date(2026, 8, 1), date(2026, 8, 31), 60, 90)


# ---------------------------------------------------------------------------
# 读次数：与「对账单份数」无关
# ---------------------------------------------------------------------------

@contextmanager
def _count_table_reads(fragment: str):
    """数一数某张插件表被读了几次（只读探针，不改任何行为）。"""
    from sqlalchemy import event

    from app.core.db import engine

    reads = []

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if fragment in statement and statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    try:
        yield reads
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def test_归属表的读次数与对账单份数无关(shared, client, auth, books3, cash_subjects):
    """**归属一次读完**，不是「每份对账单再读一遍全部账套」。

    **为什么这件事值得一条用例**：归属表是账套表 —— 一行落在它归属的那本账里，
    所以「按账套读」躲不掉。但**读几次**是可以选的：逐份对账单去读
    （每份再遍历全部账套）是 **N×M** 次。实测（生产）12 份对账单 × 12 本账
    = 144 次读，而归属表**总共只有 138 行** —— 读了一百多遍同一批数据。

    判据是**读次数与对账单份数无关**：加一份对账单，读次数**不许变**。
    （旧实现是 N×M，所以份数一加，读次数就跟着翻倍。）
    """
    from app.plugins.manifest import table_prefix

    card_id = _card_with_members(shared, client, auth, cash_subjects)
    alloc_table = table_prefix("com.cwq.sharedcard") + "alloc"

    # 插件能看到几本账 —— 归属表是账套表，每本账读一次就是那个数。
    book_count = len(_json(client.get(shared + "/catalog", headers=auth))["books"])

    def upload_and_count(name: str, path: str) -> int:
        data = (FIXTURES / path).read_bytes()
        with _count_table_reads(alloc_table) as reads:
            _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                              files={"files": (name, data, "application/octet-stream")}))
            reads.clear()
            _json(client.get(shared + f"/cards/{card_id}/statements", headers=auth))
            statements_reads = len(reads)
            reads.clear()
            _json(client.get(shared + "/queue", headers=auth))
            queue_reads = len(reads)
        return statements_reads, queue_reads

    one = upload_and_count(SAMPLE, SAMPLE)
    two = upload_and_count("农商9月.xlsx", "nongshang_xlsx.xlsx")

    # **份数从 1 变成 2，读次数不许变。**
    assert one[0] == two[0], (one, two)
    assert one[1] == two[1], (one, two)
    # 每本账读一次 —— 不是「每份对账单 × 每本账」。
    assert one == (book_count, book_count), (one, book_count)


@contextmanager
def _count_subject_books():
    """数一数「读科目表」时碰到了哪几本账（只读探针，不改任何行为）。

    插件读明细账的每一步都要读那本账的科目表 —— 所以「科目表碰到了
    哪几本账」就等于「这次请求算了几本账的账面」。
    """
    from sqlalchemy import event

    from app.core.db import engine

    books = []

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if "FROM subject" in statement and statement.lstrip().upper().startswith("SELECT"):
            books.append((parameters or {}).get("book_id_1"))

    try:
        yield books
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def test_确认核对只读被选中那本账的明细账(continuous_case, client, auth, db):
    """一次核对只写**一本账** —— 其余账套的明细账不该被读一遍。

    **为什么这件事值得一条用例**：生产上七本成员账的明细账要 580 ms，
    只读被选中的那一本只要 80 ms。白读六本账，用户点一下「确认核对」
    就得多等半秒以上 —— 而且那六本的结果既写不进去、也校验不到，
    纯纯白算。

    判据：**确认核对这一次请求里，科目表只碰到被选中的那一本账**。
    """
    path, workspace, tx, entry, payload = continuous_case
    with _count_subject_books() as books:
        _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    assert books, "一次科目表都没读 —— 这条用例本身失效了"
    assert set(books) == {entry['book_id']}, books


def test_确认核对把这一组原样交回页面(continuous_case, client, auth, db):
    """服务器把这一组**原样**交回页面 —— 页面靠它打补丁，不再重拉整个工作台。

    **判据是「两边一模一样」**：页面上的核对状态直接来自这份返回值。
    与工作台里的那份对不上，用户就会同时看到两个互相矛盾的界面。
    """
    path, workspace, tx, entry, payload = continuous_case
    body = _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    fresh = _json(client.get(path + '/workspace', headers=auth))
    assert body['ok'] is True
    assert body['id'] == fresh['groups'][0]['id']
    assert body['group'] == fresh['groups'][0]


def test_撤销把这一组交回页面(continuous_case, client, auth, db):
    """撤销也要交回这一组 —— 页面据此把它从「生效中」挪进「已撤销」。"""
    path, workspace, tx, entry, payload = continuous_case
    group = _json(client.post(path + '/reconciliations', headers=auth, json=payload))
    revoked = _json(client.delete(path + '/reconciliations/' + str(group['id']), headers=auth))
    fresh = _json(client.get(path + '/workspace', headers=auth))
    assert revoked['group']['id'] == group['id']
    assert revoked['group']['active'] is False
    assert revoked['group'] == fresh['history'][0]
