"""报表模板的**取用**闸门：打印、导出、落库三处都要判归属。

报表模板 ``id`` 是自增整数，任何人拿 1、2、3 就能把库里每张报表模板挨个试
一遍。而报表模板里装的是**取数公式**本身：行怎么排、列叫什么、每个格子从哪
张科目表取数 —— 这就是「这家单位怎么算账」。同一个部署里替另一家客户管账的
会计，拿到别家的 id 就能把那张表连公式一起打印出来。

钱是不会漏的：``evaluate`` 取数按调用方的账套过滤科目，打出来的是自己账上
的数。漏的是**口径** —— 报表名、行标签、列标签、取数公式，以及
``persist`` 落库的那份结果（落的是别家的行/列/公式配上自己账上的数，
事后没人说得清那份结果是按哪张表算出来的）。

同一个缺陷有三处同形漏洞，三处都只判了「模板存在」而没判「归属」：

* ``POST/GET .../print/report.html`` 与设计器预览 → ``_render_scope`` 的 REPORT 分支
* ``GET .../print/report.xlsx`` → ``export_report``
* ``POST /api/reports/{id}/persist`` → ``persist``

修法是全部走 ``app.api.reports._visible_template``（与 ``/design``、``/render``
同一个闸），可见 = 官方内置基线（``book_id IS NULL``）**或**归属当前账套。

这个文件钉三件事：

* **跨账套一律 404**，且回的那句话与「不存在的 id」**逐字相同** ——
  403 等于告诉对方「这个 id 确实存在」，拿它就能枚举别家建过几张报表、
  改过什么名字，这条接口本身就成了存在性探测器。
* **响应里一个字都不许漏**：别家的报表名 / 行标签 / 取数公式都不许出现在
  正文里（404 的正文也要查，别的地方可能先渲了标题再报错）。
* **不误伤**：官方内置基线（``book_id IS NULL``）与本账套自己的模板仍然
  打得开、导得出。闸加在错的位置就是事故 —— 报表页直接整页报错。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.models import ReportResult, ReportTemplate

#: 别家那张表的名字。任何一个接口的响应里都不该出现它 ——
#: 出现即等于把「别家有哪些报表、叫什么」交出去了。
SECRET = "别家机密利润表"


@pytest.fixture
def other_book(client, auth):
    """第二个账套，同样归 admin_user 所有（OWNER）。

    与 admin 在两个账套里都是 OWNER，被拒的原因就只可能是**归属**，
    不会混进角色差异 —— 混进来的话这条用例测的就不是隔离了。
    """
    tag = uuid.uuid4().hex[:8]
    r = client.post("/api/books", headers=auth,
                    json={"company_name": f"别家{tag}", "name": f"别家账套{tag}",
                          "start_year": 2026})
    assert r.status_code == 201, r.text   # 自己建的账套自动是 OWNER
    return r.json()["id"]


@pytest.fixture
def foreign_template(client, auth, other_book):
    """归属**别家**账套的一张报表模板（id 已知、内容含机密名）。"""
    r = client.post(f"/api/reports/templates?book_id={other_book}", headers=auth,
                    json={"name": SECRET, "base_code": "INCOME_SHEET"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def own_template(client, auth, book):
    """本账套自己的一张模板 —— 闸不能把它也关上。"""
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "我自己的表", "base_code": "INCOME_SHEET"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def baseline(client, auth, book):
    """官方内置利润表（``book_id IS NULL``）：所有账套共看的那一份。

    显式按 ``book_id IS NULL`` 取：别家一旦 fork 过，库里就有同 code 的副本，
    只按 code 取可能拿到副本，测的就不是基线了。
    """
    r = client.get(f"/api/reports/templates?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text
    ids = [x["id"] for x in r.json() if x["code"] == "INCOME_SHEET"]
    assert ids, f"内置利润表没列出来：{[x['code'] for x in r.json()]}"
    return ids[0]


# ------------------------------------------------------------------ 报表打印 / 导出


def test_打印别家账套的报表模板是404(client, auth, book, periods, foreign_template):
    """``report.html`` 与设计器预览共用 ``_render_scope`` 的 REPORT 分支。"""
    r = client.get(f"/api/books/{book.id}/print/report.html",
                   params={"template_id": foreign_template, "period_id": periods[1].id},
                   headers=auth)
    assert r.status_code == 404, f"拿到 {r.status_code}，别家的报表被打印出来了"
    assert SECRET not in r.text, "响应正文里漏出了别家的报表名"


def test_预览别家账套的报表模板拿不到任何东西(client, auth, book, periods, foreign_template):
    """设计器预览走同一个 REPORT 分支。

    预览接口把非 413 的 ``HTTPException`` 一律翻译成 422（见 ``preview_draft``：
    「参数没选全」不该让预览框变红），所以这里钉的是「渲染不出来 + 一个字都不漏」，
    而不是 404 本身 —— 那道 404 已经由 ``test_打印别家账套的报表模板是404`` 钉在
    同一个 ``_render_scope`` 分支上了。
    """
    r = client.post(f"/api/books/{book.id}/print/preview", headers=auth,
                    json={"scope": "REPORT", "content": {},
                          "report_template_id": foreign_template,
                          "period_id": periods[1].id})
    assert r.status_code >= 400, f"拿到 {r.status_code}：别家的报表被预览出来了"
    assert SECRET not in r.text, "响应正文里漏出了别家的报表名"


def test_导出让家账套的报表模板是404(client, auth, book, periods, foreign_template):
    """Excel 导出会把报表名与取数公式整份写进文件，发出去就收不回来了。"""
    r = client.get(f"/api/books/{book.id}/print/report.xlsx",
                   params={"template_id": foreign_template, "period_id": periods[1].id},
                   headers=auth)
    assert r.status_code == 404, f"拿到 {r.status_code}，别家的报表被导出了"
    assert SECRET not in r.text


def test_404与不存在的id回同一句话(client, auth, book, periods, foreign_template):
    """别家的模板与不存在的模板必须**长得一模一样**。

    否则这条接口就是一个跨账套的存在性探测器：拿 id 挨个试，回 403/200 的那些
    就是别家真实存在的报表，回 404 的就是空号 —— 一轮下来能列出别家的报表清单。
    """
    mine = client.get(f"/api/books/{book.id}/print/report.html",
                      params={"template_id": foreign_template, "period_id": periods[1].id},
                      headers=auth)
    ghost = client.get(f"/api/books/{book.id}/print/report.html",
                       params={"template_id": 99999999, "period_id": periods[1].id},
                       headers=auth)
    assert ghost.status_code == 404, ghost.text
    assert mine.json()["detail"] == ghost.json()["detail"], (
        "别人的模板和不存在的模板必须回同一句话，"
        f"实际 {mine.json()['detail']!r} vs {ghost.json()['detail']!r}")


# ------------------------------------------------------------------ 结果落库


def test_persist别家账套的报表模板是404且不落库(client, auth, db, book, periods,
                                            foreign_template):
    """落库这一处的危害与前两处不同：它会**写**东西。

    落下去的行/列/公式是别家的口径，值是自己账上的数，事后谁也说不清这份
    结果是按哪张表算出来的 —— 而它就躺在 ``report_result`` 里等着被导出。
    """
    before = db.scalar(select(func.count()).select_from(ReportResult))

    r = client.post(f"/api/reports/{foreign_template}/persist",
                    params={"book_id": book.id, "period_id": periods[1].id},
                    headers=auth)
    assert r.status_code == 404, f"拿到 {r.status_code}，别家口径的结果被落库了"

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReportResult)) == before, (
        "被拒的请求仍然写了 report_result")
    db.rollback()
    assert db.get(ReportTemplate, foreign_template).book_id != book.id


def test_persist别家模板的404与不存在的id一致(client, auth, book, periods, foreign_template):
    mine = client.post(f"/api/reports/{foreign_template}/persist",
                       params={"book_id": book.id, "period_id": periods[1].id}, headers=auth)
    ghost = client.post("/api/reports/99999999/persist",
                        params={"book_id": book.id, "period_id": periods[1].id}, headers=auth)
    assert mine.status_code == 404 and ghost.status_code == 404
    assert mine.json()["detail"] == ghost.json()["detail"]


# ------------------------------------------------------------------ 别误伤


def test_本账套的模板照样打得开也导得出(client, auth, book, periods, own_template,
                                        make_voucher):
    """闸只挡「不是你的」，不能顺手把正常路径也关上。

    报表页一报错就是整页空白，而这类闸最容易出的事故就是判据写反 ——
    宁可现在钉住，也别等报表功能整体不可用时再回头找。
    """
    make_voucher("2026-01-05", dr=[("1001", 300, "首月租金")],
                 cr=[("5001", 300, "首月租金")], posted=True)
    params = {"template_id": own_template, "period_id": periods[1].id}

    html = client.get(f"/api/books/{book.id}/print/report.html", params=params, headers=auth)
    assert html.status_code == 200, html.text
    assert "我自己的表" in html.text

    xlsx = client.get(f"/api/books/{book.id}/print/report.xlsx", params=params, headers=auth)
    assert xlsx.status_code == 200, xlsx.text
    assert xlsx.content[:2] == b"PK", "导出返回的不是 xlsx"


def test_官方内置基线对每个账套都打得开(client, auth, book, periods, baseline):
    """内置基线是所有账套共看的那一份（``book_id IS NULL``），闸不能把它挡掉。

    这正是复用 ``_visible_template`` 而不是抄一份判定的原因：它把「基线可见」
    写进了同一个谓词里。抄一份的人很容易顺手写成「必须属于本账套」，
    于是所有新账套的第一次报表渲染就 404，而报表是这套系统的门面功能。
    """
    params = {"template_id": baseline, "period_id": periods[1].id}
    assert client.get(f"/api/books/{book.id}/print/report.html",
                      params=params, headers=auth).status_code == 200
    assert client.get(f"/api/books/{book.id}/print/report.xlsx",
                      params=params, headers=auth).status_code == 200
    saved = client.post(f"/api/reports/{baseline}/persist",
                        params={"book_id": book.id, "period_id": periods[1].id},
                        headers=auth)
    assert saved.status_code == 200, saved.text
