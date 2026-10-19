"""报表模板的**账套隔离**（迁移 0020）。

改这一列之前，报表模板是全局的：所有账套共用同一套行、列和取数公式，
账套 A 改了公式，账套 B 第二天打开报表看到的就是改过的那份 —— 而报表
的取数口径正是「这家单位怎么算账」，串改一次等于两个互不相识的公司
在用同一张利润表。

现在 ``report_template.book_id`` 把归属摆到明面上：

* ``NULL`` = **官方内置基线**。所有账套共看同一份，而且**只读**。
* 某个 ``book_id`` = 归属该账套，只有它的 OWNER（或管理员）能改。

「改过」和「共享」不能同时成立，所以走**写时复制**：某账套第一次要改
内置表时先 ``fork`` 一份归属自己，之后改副本 —— 基线因此永远是干净的
出厂状态，别的账套一个字节都影响不到。

这个文件钉的就是那三件事：**看得见的只有该看见的、改不着的改不动、
改动不扩散**。可见性钉列表和 design，写权限钉 404/400，不扩散钉基线
内容在副本被改之后仍然逐字节不变。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.models import ReportTemplate

#: 「写不进去」这条断言只关心状态码是不是拒绝态。
#: 内置基线走的是 400：基线是公开的（每个账套的列表里都有），回一句
#: 「不能这么改，去设计模式复制一份」不泄露任何新信息 —— 靠 404 挡它反而
#: 只丢一句「报表模板不存在」，用户无从下手。别的账套的私有模板仍然一律
#: 404（见 test_改别的账套的模板是404而不是403，那条不许松）。
REJECTED = (400,)


@pytest.fixture
def baseline_id(db):
    """官方内置资产负债表（``book_id IS NULL``）。

    显式按 ``book_id IS NULL`` 取：某个账套 fork 之后库里会有同 code 的副本，
    只按 code 取可能拿到副本，测的就不是基线了。
    """
    tid = db.scalar(select(ReportTemplate.id).where(
        ReportTemplate.code == "BALANCE_SHEET", ReportTemplate.book_id.is_(None)))
    assert tid, "内置资产负债表模板没建出来，种子数据有问题"
    return tid


@pytest.fixture
def other_book(client, auth):
    """第二个账套，同样归 admin_user 所有。

    关键在「**也是 OWNER**」：这样隔离用例里被拒的原因只可能是**归属**，
    不可能混进角色差异。如果第二个账套里 admin 只是会计，那么一次 403
    既可能是「角色不够」也可能是「不是你的」，用例就白测了。
    """
    tag = uuid.uuid4().hex[:8]
    r = client.post("/api/books", headers=auth,
                    json={"company_name": f"别家{tag}", "name": f"别家账套{tag}",
                          "start_year": 2026})
    assert r.status_code == 201, r.text   # 自己建的账套自动是 OWNER
    return r.json()["id"]


def listing(client, auth, book_id):
    r = client.get(f"/api/reports/templates?book_id={book_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def design(client, auth, template_id, book_id):
    return client.get(f"/api/reports/{template_id}/design?book_id={book_id}",
                      headers=auth).json()


def fork(client, auth, template_id, book_id):
    r = client.post(f"/api/reports/templates/{template_id}/fork?book_id={book_id}",
                    headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _cells_by_addr(design_payload):
    """把 design 返回的 cells 摊成「地址 → 公式」，顺序信息直接抹掉。"""
    return {(c["row_no"], c["col_no"]): c["formula"] for c in design_payload["cells"]}


def _import_payload(fingerprint="0123456789abcdef01234567"):
    """一份最小的外部模板。公式用纯算术、不挂科目 ——
    第二个账套没初始化科目表，挂科目会让这条用例被 ``待匹配科目`` 那道
    校验挡掉，测的就不是隔离了。
    """
    return {
        "templates": [{
            "name": "迁移利润表",
            "type": "INCOME_SHEET",
            "source": {"filename": "利润表.rep", "format": "用友 UFO5.10",
                       "fingerprint": fingerprint},
            "rows": [{"row_no": 1, "label": "营业收入", "style": {}}],
            "cols": [{"col_no": 1, "label": "项目", "kind": "STATIC", "width": 250},
                     {"col_no": 2, "label": "本期数", "kind": "FORMULA", "width": 140}],
            "cells": [{"row_no": 1, "col_no": 2, "formula": "1+1"}],
            "issues": [],
            "subject_mapping": {},
        }],
        "accept_warnings": False,
    }


# ------------------------------------------------------------ 列表的可见性


def test_列表只返回官方基线和当前账套的(client, auth, book, other_book):
    """别人账套的模板不该出现在这里 —— 这是隔离的第一道。

    官方基线（资产负债表 / 利润表）人人可见是设计的一部分：它是所有账套
    的共同起点。真正要守的是「本账套的看得到、别人的看不到」。
    """
    r = client.post(f"/api/reports/templates?book_id={other_book}", headers=auth,
                    json={"name": "别家专属表", "base_code": "INCOME_SHEET"})
    assert r.status_code == 201, r.text

    items = listing(client, auth, book.id)
    codes = [x["code"] for x in items]
    names = [x["name"] for x in items]
    assert "BALANCE_SHEET" in codes and "INCOME_SHEET" in codes, (
        f"官方基线应当人人可见，实际 {codes}")
    assert "别家专属表" not in names, "别的账套的模板漏进列表了"

    # 反过来也成立：别家账套看得到自己那张，看不到本账套建的
    other_items = listing(client, auth, other_book)
    assert "别家专属表" in [x["name"] for x in other_items]
    assert [x["name"] for x in other_items].count("别家专属表") == 1


def test_同code下基线与副本并存时列表只出现一份且是本账套的(client, auth, book, baseline_id):
    """**去重是这一条的核心。**

    fork 之后基线和副本同 code 并存。不去重的话列表里会同时冒出两张
    「资产负债表」，用户点开哪张全凭上下顺序 —— 改过的那份可能根本没被
    打开，而没改过的账套看到的却像是被人动过。

    副本优先：用户改过的永远是改过那份；没动过的账套继续看基线。
    """
    forked = fork(client, auth, baseline_id, book.id)

    bs = [x for x in listing(client, auth, book.id) if x["code"] == "BALANCE_SHEET"]
    assert len(bs) == 1, f"同 code 出现了 {len(bs)} 份：{[x['id'] for x in bs]}"
    assert bs[0]["id"] == forked["id"], (
        f"列表给的是基线 {bs[0]['id']} 而不是本账套副本 {forked['id']}")
    assert bs[0]["book_id"] == book.id


# -------------------------------------------------------------- 写不进去


def test_直接改内置基线必须被拒且基线内容不变(client, auth, db, book, baseline_id):
    """内置基线不接受任何直接写入 —— 这是「改动不扩散」的第一道闸。

    **状态码钉死 400 + 那句能照做的提示**，不再按集合断言。曾经这里是
    ``(400, 404)`` 加一句「已知缺陷」：``_require_owned_template`` 先判
    ``tpl.book_id != book_id``，而 ``book_id`` 已被门禁保证非 None，于是
    基线那种 ``book_id IS NULL`` 必然先被 404 判掉，那句解释怎么改基线的
    400 永远走不到。把基线提到 404 之前才是对的：基线是公开的，回 404 既
    遮住了提示，又没换来任何防枚举的效果（列表里本来就列着它）。

    钉死状态码是为了让**判断顺序**不再能悄悄退化回 404 —— 那正是这个缺陷
    的形状：功能没坏，只是提示再也没人看得到。
    """
    before = design(client, auth, baseline_id, book.id)

    r = client.put(f"/api/reports/{baseline_id}/cells?book_id={book.id}", headers=auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1+1"}])
    assert r.status_code == 400, f"拿到 {r.status_code}：内置基线被改写了"
    assert "复制一份到当前账套" in r.json()["detail"], r.json()

    assert design(client, auth, baseline_id, book.id) == before, "内置基线的内容变了"


def test_改别的账套的模板是404而不是403(client, auth, book, other_book):
    """**隔离**，不是权限不足。

    admin 在两个账套里都是 OWNER，所以被拒的原因只可能是归属。
    回 403 等于告诉对方「这个 id 确实存在，只是你没权限」—— 那本身就是
    信息泄露：拿它就能枚举别的账套建过几张报表、改过什么名字。
    别的模板与不存在的模板必须**长得一模一样**。
    """
    r = client.post(f"/api/reports/templates?book_id={other_book}", headers=auth,
                    json={"name": "别家表", "base_code": "INCOME_SHEET"})
    assert r.status_code == 201, r.text
    other_tpl = r.json()["id"]

    # 读不到：连结构都拿不到
    assert client.get(f"/api/reports/{other_tpl}/design?book_id={book.id}",
                      headers=auth).status_code == 404
    # 改不了，而且是与「不存在的 id」同样的 404
    r = client.put(f"/api/reports/{other_tpl}/cells?book_id={book.id}", headers=auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1+1"}])
    assert r.status_code == 404, f"拿到 {r.status_code}：{r.text[:200]}"
    missing = client.put(f"/api/reports/99999999/cells?book_id={book.id}", headers=auth,
                         json=[{"row_no": 2, "col_no": 3, "formula": "1+1"}])
    assert r.json()["detail"] == missing.json()["detail"], (
        "别人的模板和不存在的模板必须回同一句话，否则 404/403 的差别本身就在泄露")


# ------------------------------------------------------------------ 写时复制


def test_fork_第一次整份复制第二次不重复(client, auth, db, book, baseline_id):
    """fork 要**真的复制**：行、列、单元格公式整份带走，一次都不能少。

    少带一格的后果是报表设计页打开就少一行/少一列，而用户未必看得出来 ——
    这正是这套模板最危险的那种「悄无声息」。所以这里逐段比，不比数量。

    第二次必须返回同一个 id：反复进设计模式如果每次都复制一份，
    用例几轮下来就会攒出一堆同 code 的副本。
    """
    base = design(client, auth, baseline_id, book.id)

    first = fork(client, auth, baseline_id, book.id)
    assert first["forked"] is True, first
    forked_id = first["id"]
    assert forked_id != baseline_id

    copied = design(client, auth, forked_id, book.id)
    assert copied["rows"] == base["rows"], "行没复制全"
    assert copied["cols"] == base["cols"], "列没复制全"
    # 按地址比，不按列表顺序比。这条要断的是「每一格公式都带过来了」，
    # 而列表顺序是接口的实现细节 —— 直接 == 比会随机红：两边是两次独立
    # 查询，数据库按堆顺序吐行，同一批数据两次的排列完全可能不同。
    assert _cells_by_addr(copied) == _cells_by_addr(base), "单元格公式没复制全"
    assert any(c["formula"] for c in copied["cells"]), "复制过来的是一张空表"

    second = fork(client, auth, baseline_id, book.id)
    assert second["id"] == forked_id, "第二次 fork 又复制了一份"
    assert second["forked"] is False, second
    assert db.scalar(select(func.count()).select_from(ReportTemplate).where(
        ReportTemplate.book_id == book.id)) == 1, "本账套名下多出了模板"


def test_副本不是内置所以能删_删掉回到看基线(client, auth, db, book, baseline_id):
    """副本是账套自己维护的表，``is_builtin`` 必须是 false。

    这是「我不想要我的改动」的那条退路：删掉副本，这个账套就回到看基线。
    反过来，官方基线**永远删不掉** —— 它不属于任何账套，别的账套还在看它。
    """
    forked_id = fork(client, auth, baseline_id, book.id)["id"]
    assert db.get(ReportTemplate, forked_id).is_builtin is False, (
        "副本标成内置就删不掉了，那条退路没了")

    r = client.delete(f"/api/reports/templates/{forked_id}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(ReportTemplate, forked_id) is None, "副本没删掉"
    # 回到看基线：列表里又是那张官方表
    assert any(x["id"] == baseline_id
               for x in listing(client, auth, book.id)), "删完副本却看不到基线了"

    # 基线永远删不掉
    r = client.delete(f"/api/reports/templates/{baseline_id}?book_id={book.id}",
                      headers=auth)
    assert r.status_code in REJECTED, f"拿到 {r.status_code}：官方基线被删了"
    db.expire_all()
    assert db.get(ReportTemplate, baseline_id) is not None, "官方基线真的没了"


def test_改副本之后基线逐字节不变(client, auth, db, book, baseline_id):
    """「改动不扩散」的核心断言。

    账套 A 改了公式、改了表名之后，账套 B（以及 A 自己）看到的基线必须
    **一模一样** —— 不是「大致相同」，是 design 返回的整份结构逐字段相等。
    复制隔离最容易出的事故是「看起来隔离了，改完才发现基线被动过」，
    而那种事故在报表上只表现为数字悄悄变了。
    """
    before = design(client, auth, baseline_id, book.id)
    forked_id = fork(client, auth, baseline_id, book.id)["id"]

    r = client.put(f"/api/reports/{forked_id}/cells?book_id={book.id}", headers=auth,
                   json=[{"row_no": 2, "col_no": 3, "formula": "1+1"}])
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/reports/templates/{forked_id}?book_id={book.id}",
                     headers=auth, json={"name": "我改过的资产负债表"})
    assert r.status_code == 200, r.text

    # 基线的结构与公式一个字都没动。cells 按地址比 —— 「一个都没变」是
    # 这条要断的事，列表顺序不是；直接整包 == 会在返回顺序变过时假红。
    after = design(client, auth, baseline_id, book.id)
    assert after["rows"] == before["rows"], "改动扩散到基线的行了"
    assert after["cols"] == before["cols"], "改动扩散到基线的列了"
    assert _cells_by_addr(after) == _cells_by_addr(before), "改动扩散到基线的公式了"
    db.expire_all()
    base = db.get(ReportTemplate, baseline_id)
    assert base.name != "我改过的资产负债表", base.name
    assert base.book_id is None, "基线的归属被写成了某个账套"

    # 副本上的改动是真的生效了 —— 否则这条用例会在「什么都没改」时也绿
    changed = {(c["row_no"], c["col_no"]): c["formula"]
               for c in design(client, auth, forked_id, book.id)["cells"]}
    assert changed[(2, 3)] == "1+1", changed[(2, 3)]


def test_新建与复制的模板一律归属当前账套(client, auth, db, book, baseline_id):
    """导入 / 复制 / 新建的模板都不该再是「全局无主」。

    自建模板没有「先让所有人看到一份原版」的必要，留个归属更省心 ——
    否则它会以基线的身份被所有账套共看，而它的口径只对建它的那个账套成立。
    """
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "自建表", "base_code": "INCOME_SHEET"})
    assert r.status_code == 201, r.text
    assert db.get(ReportTemplate, r.json()["id"]).book_id == book.id

    r = client.post(f"/api/reports/templates/{baseline_id}/copy?book_id={book.id}",
                    headers=auth, json={"name": "基线副本"})
    assert r.status_code == 200, r.text
    assert db.get(ReportTemplate, r.json()["id"]).book_id == book.id


# ------------------------------------------------------- 隔离解开的那个老限制


def test_同一份文件可以导进两个账套(client, auth, book, other_book):
    """导入模板的 ``code`` 是文件内容指纹。

    隔离之前 ``code`` 全局唯一，于是**整个系统只能导同一份文件一次** ——
    第二个账套想用同一张外来报表都导不进来，被告知「已经导入，请打开已有模板」，
    而它压根打不开别人的那张。

    按账套唯一之后这个老限制解开了：每个账套各导各的，互不冲突。
    """
    payload = _import_payload()

    a = client.post(f"/api/reports/templates/import?book_id={book.id}",
                    headers=auth, json=payload)
    assert a.status_code == 201, a.text
    b = client.post(f"/api/reports/templates/import?book_id={other_book}",
                    headers=auth, json=payload)
    assert b.status_code == 201, b.text
    assert a.json()["created"][0]["id"] != b.json()["created"][0]["id"]

    # 但**同一个账套**里仍然只导一次：重复导入要给出「已有模板」的提示，
    # 而不是悄悄多出一张同 code 的表。
    again = client.post(f"/api/reports/templates/import?book_id={book.id}",
                        headers=auth, json=payload)
    assert again.status_code == 409, again.text
