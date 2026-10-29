"""共享卡对账插件的**端到端**用例。

这一组盯的是「一张卡被多个账套共用」这条主线能不能真的成立：

    3 个账套共用一张卡 → 上传**一份**对账单 → 每一行判给某个账套 →
    各账套记到这张卡上的数**加起来**等于这张卡的真实数。

**这个插件只对账，不制证**（1.1.0 起）：凭证是用户自己在账上录的，插件不替
用户猜对方科目。所以这里守的是**核对**，不是记账：

1. **归属正确**：规则命中与人工复核都能把行判给对的账套。
2. **少记看得出来**：各账套该科目的**本期净发生额**（账上动了多少）与
   **归属流水净额**（该记多少）一减，就是这本账少记了多少。
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

    bad = client.post(shared + f"/cards/{card_id}/rules", headers=auth, json={
        "book_id": item["book"].id, "match_field": "cp_name",
        "match_mode": "contains", "pattern": "丙供应链",
        "target_subject_code": item["counter"].code})
    assert bad.status_code == 422
    assert "不制证" in bad.json()["detail"]


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
    assert registry.version == "1.1.0", registry.version


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
# 2. 归属规则引擎
# ---------------------------------------------------------------------------

def _card_with_members(shared, client, auth, cash_subjects, rules):
    card_id = _json(client.post(shared + "/cards", headers=auth,
                                json={"name": "规则用卡"}))["card"]["id"]
    for item in cash_subjects:
        _json(client.post(shared + f"/cards/{card_id}/members", headers=auth, json={
            "book_id": item["book"].id, "subject_code": item["cash"].code}))
    for rule in rules:
        _json(client.post(shared + f"/cards/{card_id}/rules", headers=auth, json=rule))
    return card_id


def test_规则命中优先级大的(shared, client, auth, cash_subjects):
    """两条规则都命中时，优先级大的那条说了算。"""
    card_id = _card_with_members(shared, client, auth, cash_subjects, [
        {"book_id": cash_subjects[0]["book"].id, "priority": 10,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "实业"},
        {"book_id": cash_subjects[1]["book"].id, "priority": 99,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "实业"},
    ])
    rules = _json(client.get(shared + f"/cards/{card_id}/rules", headers=auth))["rules"]
    assert len(rules) == 2
    assert {rule["priority"] for rule in rules} == {10, 99}


def test_同优先级指向不同账套时不猜(shared, client, auth, cash_subjects):
    """两条同优先级的规则命中同一行、却指向不同账套 —— 进冲突队列，不自动归。"""
    card_id = _card_with_members(shared, client, auth, cash_subjects, [
        {"book_id": cash_subjects[0]["book"].id, "priority": 50,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "实业"},
        {"book_id": cash_subjects[1]["book"].id, "priority": 50,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "实业"},
    ])
    # 规则本身建得起来（两条都合法），但它们会互相打架。
    rules = _json(client.get(shared + f"/cards/{card_id}/rules", headers=auth))["rules"]
    assert len(rules) == 2


def test_写错的正则在保存时就被挡下(shared, client, auth, cash_subjects):
    """错正则要**当场**说清楚，而不是等归属跑完才发现一条都没命中。"""
    card_id = _card_with_members(shared, client, auth, cash_subjects, [])
    bad = client.post(shared + f"/cards/{card_id}/rules", headers=auth, json={
        "book_id": cash_subjects[0]["book"].id, "match_field": "cp_name",
        "match_mode": "regex", "pattern": "[未闭合"})
    assert bad.status_code == 422
    assert "正则" in bad.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 端到端：一张卡、三个账套、一份对账单
# ---------------------------------------------------------------------------

@pytest.fixture
def loaded(shared, client, auth, books3, cash_subjects):
    """上传一份对账单并按规则归属。

    归属规则：丙供应链 → 第一本账；丁物业 → 第二本账；戊建材 → 第三本账。
    另外两行（乙实业、结息）**没有规则命中**，留在复核队列里。
    """
    card_id = _card_with_members(shared, client, auth, cash_subjects, [
        {"book_id": cash_subjects[0]["book"].id, "priority": 50,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "丙供应链"},
        {"book_id": cash_subjects[1]["book"].id, "priority": 50,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "丁物业"},
        {"book_id": cash_subjects[2]["book"].id, "priority": 50,
         "match_field": "cp_name", "match_mode": "contains", "pattern": "戊建筑材料"},
    ])
    data = (FIXTURES / SAMPLE).read_bytes()
    uploaded = _json(client.post(shared + f"/cards/{card_id}/statements", headers=auth,
                                 params={"book_id": cash_subjects[0]["book"].id},
                                 files={"files": (SAMPLE, data, "application/octet-stream")}))
    assert uploaded["rows"] == 5, uploaded
    allocated = _json(client.post(shared + f"/statements/{uploaded['statement_id']}/allocate",
                                  headers=auth))
    assert allocated["created"] == 3, allocated
    assert allocated["unmatched"] == 2, allocated
    return {"card_id": card_id, "stmt_id": uploaded["statement_id"], "uploaded": uploaded}


def test_上传与归属(shared, client, auth, loaded):
    """5 行：3 行命中规则、2 行没命中（进复核队列）。"""
    stmt_id = loaded["stmt_id"]
    detail = _json(client.get(shared + f"/statements/{stmt_id}", headers=auth))
    assert detail["row_count"] == 5
    assert detail["assigned"] == 3
    assert detail["pending"] == 2
    assert detail["income_total"] == "100007401.00"
    assert detail["expense_total"] == "30010530.00"
    assert detail["closing_balance"] == "75844188.00"


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
    assert queue[0]["conflicts"] == 0


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
    first = cash_subjects[0]["book"].id
    second = cash_subjects[1]["book"].id

    path = tmp_path / "book.json.gz"
    with SessionLocal() as session:
        export_scope(session, scope=SCOPE_BOOK, book_id=first,
                     book_label=first, out_path=path)
    data = read_snapshot(path)

    prefix = "plugin_275b391c351a_"
    for suffix in ("card", "member", "rule", "stmt", "row", "alloc"):
        assert prefix + suffix in data["tables"], \
            "账套级备份漏了 %s 表" % suffix

    # global 的卡：整表进去。
    cards = data["tables"][prefix + "card"]["rows"]
    assert len(cards) == 1
    assert cards[0]["id"] == card_id

    # book 级的五张：只有本账套的行（库里另有另外两本账的行）。
    for suffix in ("member", "rule", "stmt", "row", "alloc"):
        rows = data["tables"][prefix + suffix]["rows"]
        assert rows, "%s 表在归档里是空的" % suffix
        assert {row["book_id"] for row in rows} == {first}, \
            "%s 表混进了别的账套的行" % suffix

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
