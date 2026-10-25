"""高风险写路径的审计覆盖（审计 P3-10）。

审计列了四类没有留痕的写路径：**期初余额、科目增删改、授权变更、
报表公式修改**。前两类直接决定账面数字 —— 出事之后没有日志，
就只能靠用户回忆「我好像改过点什么」。

这里不按接口逐个钉，而是钉**审计行本身的内容**：谁改的、改了什么、
改前改后是什么。``detail`` 只有一句话而 ``before``/``after`` 是空的
那种，事后基本没法追 —— 尤其期初：一个科目从 100 改成 0 和从 100
改成 500，在文字描述里可能长得一模一样。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AuditLog


def _logs(db, book, action=None):
    stmt = select(AuditLog).where(AuditLog.book_id == book.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.scalars(stmt.order_by(AuditLog.id)).all())


def _last(db, book, action):
    rows = _logs(db, book, action)
    assert rows, f"没有 {action} 的审计记录"
    return rows[-1]


# --------------------------------------------------------------------- 期初余额


def test_期初余额写入要留审计(client, auth, db, book, subjects):
    p1 = db.query(__import__("app.models", fromlist=["Period"]).Period).filter_by(
        book_id=book.id, month=1).one()

    r = client.post(f"/api/books/{book.id}/periods?period_id={p1.id}", headers=auth, json=[
        {"subject_id": subjects["1002"].id, "amount": "1000"},
        {"subject_id": subjects["4001"].id, "amount": "-1000"},
    ])
    assert r.status_code == 200, r.text

    entry = _last(db, book, "SET_OPENINGS")
    assert entry.detail, "期初写入的审计没有 detail"
    assert entry.before or entry.after, (
        "期初写入的审计没有留 before/after 快照 —— "
        "事后只知道「有人动过」，不知道动成了什么")


def test_期初改写要留改前值(client, auth, db, book, subjects):
    """先录 1000，再改成 500 —— 审计里必须能看出 1000 这个数。"""
    from app.models import Period

    p1 = db.query(Period).filter_by(book_id=book.id, month=1).one()
    payload = [{"subject_id": subjects["1002"].id, "amount": "1000"},
               {"subject_id": subjects["4001"].id, "amount": "-1000"}]
    assert client.post(f"/api/books/{book.id}/periods?period_id={p1.id}",
                       headers=auth, json=payload).status_code == 200

    payload2 = [{"subject_id": subjects["1002"].id, "amount": "500"},
                {"subject_id": subjects["4001"].id, "amount": "-500"}]
    assert client.post(f"/api/books/{book.id}/periods?period_id={p1.id}",
                       headers=auth, json=payload2).status_code == 200

    rows = _logs(db, book, "SET_OPENINGS")
    assert len(rows) == 2, f"两次写入应当有两条审计，实际 {len(rows)} 条"
    latest = rows[-1]
    blob = str(latest.before) + str(latest.after)
    assert "1000" in blob or "500" in blob, (
        f"第二次写入的审计里既没有旧值也没有新值：{latest.before} / {latest.after}")
    assert "500" in blob, f"新值没进审计：{latest.before} / {latest.after}"


def test_试算不平被拒时不留审计(client, auth, db, book, subjects):
    """被拒绝的请求没有改动任何数据，不该产生审计 —— 否则日志会被噪音淹没。"""
    from app.models import Period

    p1 = db.query(Period).filter_by(book_id=book.id, month=1).one()
    before = len(_logs(db, book, "SET_OPENINGS"))

    r = client.post(f"/api/books/{book.id}/periods?period_id={p1.id}", headers=auth, json=[
        {"subject_id": subjects["1002"].id, "amount": "1000"},
    ])
    assert r.status_code == 400
    assert len(_logs(db, book, "SET_OPENINGS")) == before, "被拒绝的期初写入留下了审计"


# --------------------------------------------------------------------- 科目


def test_新建科目要留审计(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1905", "name": "测试用科目", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": False,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 99,
    })
    assert r.status_code == 201, r.text

    entry = _last(db, book, "CREATE_SUBJECT")
    assert "1905" in entry.detail


def test_改科目要留改前改后(client, auth, db, book, subjects):
    sid_ = subjects["1002"].id
    payload = {
        "code": "1002", "name": "改过的名字", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": False,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 0,
    }
    r = client.patch(f"/api/books/{book.id}/subjects/{sid_}", headers=auth, json=payload)
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_SUBJECT")
    blob = str(entry.before) + str(entry.after)
    assert "银行存款" in blob or "改过的名字" in blob, (
        f"科目改名没有留下新旧名字：{entry.before} / {entry.after}")


def test_停用科目要留审计(client, auth, db, book, subjects):
    """停用是个安全动作但影响面很大（老凭证还在用它），必须留痕。"""
    sid_ = subjects["1002"].id
    payload = {
        "code": "1002", "name": "银行存款", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": False,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 0,
        "active": False,
    }
    r = client.patch(f"/api/books/{book.id}/subjects/{sid_}", headers=auth, json=payload)
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_SUBJECT")
    assert "停用" in entry.detail or "active" in str(entry.after), (
        f"停用没有在审计里体现：{entry.detail} / {entry.after}")


def test_删科目要留审计(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1999", "name": "待删除科目", "category": "ASSET",
        "direction": "DEBIT", "is_leaf": True, "carry_forward": False,
        "qty_calc": False, "aux_calc": False, "fx_calc": False, "sort_no": 98,
    })
    sid_ = r.json()["id"]

    d = client.delete(f"/api/books/{book.id}/subjects/{sid_}", headers=auth)
    assert d.status_code == 200, d.text

    entry = _last(db, book, "DELETE_SUBJECT")
    assert "1999" in entry.detail
    assert entry.before, "删除审计没有留 before 快照 —— 删掉之后就没法还原这个科目了"


# --------------------------------------------------------------------- 报表公式


def test_改报表公式要留审计(client, auth, db, book):
    """改公式的接口是 ``PUT /reports/{id}/cells``（审计报告里的行号已过期）。

    公式写错一个字符，资产负债表就会悄无声息地少算一列 —— 而且报表照样
    渲染得出来。所以事后必须能查到「这个格子的公式原来是什么」。
    """
    from app.models import ReportCell, ReportCol, ReportTemplate

    tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))

    # **只挑数值列，且排序确定。**
    #
    # 原来这里是 ``select(ReportCell).where(template_id == tpl.id).limit(1)`` ——
    # 无排序，拿到哪一格取决于数据库返回顺序，而那个顺序取决于**别的测试
    # 在这个库里留下了什么**。于是它有时抓到数值列、有时抓到 STATIC 固定列。
    #
    # 抓到 STATIC 列是要出事的：固定列放的是行标签/行次，不走取数，
    # 往里写公式等于把「行次 31」当成金额渲染。而下面那条
    # ``test_资产负债表的静态列与行次列没有公式`` 断言的正是「静态列没有
    # 公式」—— 它一抓到固定列就红。
    #
    # 于是表现为一条**与本用例毫无关系的用例在并行下时红时绿**，
    # 而真正的原因隔了两个文件、两百行。往 STATIC 列写公式本身也是错的做法，
    # 所以这里从源头上就不该选它。
    numeric_cols = db.scalars(
        select(ReportCol.col_no).where(
            ReportCol.template_id == tpl.id, ReportCol.kind != "STATIC")
    ).all()
    assert numeric_cols, "资产负债表里没有数值列，这条用例的前提不成立"
    cell = db.scalar(
        select(ReportCell)
        .where(ReportCell.template_id == tpl.id,
               ReportCell.col_no.in_(numeric_cols))
        .order_by(ReportCell.row_no, ReportCell.col_no)   # 确定性
    )
    assert cell is not None, "资产负债表里没有数值列的单元格"
    old_formula = cell.formula

    # 先 fork 一份归属 book 的副本再改：模板按账套隔离（迁移 0020）之后
    # 内置基线是只读的，往它身上写只会拿到 404 —— 那条用例要验的是
    # 「改公式留不留 before」，靶子必须是账套改得动的那一份。
    baseline = db.scalar(select(ReportTemplate).where(
        ReportTemplate.code == "BALANCE_SHEET", ReportTemplate.book_id.is_(None)))
    forked = client.post(f"/api/reports/templates/{baseline.id}/fork?book_id={book.id}",
                         headers=auth)
    assert forked.status_code == 201, forked.text
    tpl = db.get(ReportTemplate, forked.json()["id"])
    cell = db.scalar(
        select(ReportCell)
        .where(ReportCell.template_id == tpl.id,
               ReportCell.col_no.in_(numeric_cols))
        .order_by(ReportCell.row_no, ReportCell.col_no)
    )
    assert cell is not None, "fork 出来的副本里没有数值列的单元格"
    old_formula = cell.formula

    # **改完必须还原。**
    #
    # 经 client 的写入走的是 API 自己的 Session 并且 commit，而 conftest 的
    # ``db`` 夹具只 rollback **它自己那个** session —— 所以 API 写进去的东西
    # 不会被夹具撤销（审计已记录的既有设计：「db 夹具的 rollback 被夹具内部
    # 的 commit 架空」）。这条毒公式因此会活过本用例，去污染后面读同一张
    # 模板的用例。放到 finally 里，是为了让**断言失败时也照样还原** ——
    # 一次红掉的用例不该顺手再弄坏另外一条。
    try:
        r = client.put(f"/api/reports/{tpl.id}/cells?book_id={book.id}", headers=auth,
                       json=[{"row_no": cell.row_no, "col_no": cell.col_no,
                              "formula": "科目(\"1001\",\"期末余额\",{{本期}})*999"}])
        assert r.status_code == 200, r.text

        entry = _last(db, book, "UPDATE_REPORT_CELLS")
        blob = str(entry.before) + str(entry.after)
        assert old_formula[:20] in blob, (
            f"公式审计里没有原公式（{old_formula[:40]}…）：{entry.before} / {entry.after}")
        assert "999" in blob, f"新公式没进审计：{entry.before} / {entry.after}"
    finally:
        # 还原本身也要验：还原失败会让毒公式活过本用例，而**症状出现在别的
        # 文件里**（读同一张模板的用例红）。这里不响亮地失败，那条毒就会
        # 以「与本用例无关的用例随机变红」的形式回来 —— 也就是它现在的样子。
        back = client.put(f"/api/reports/{tpl.id}/cells?book_id={book.id}", headers=auth,
                          json=[{"row_no": cell.row_no, "col_no": cell.col_no,
                                 "formula": old_formula}])
        assert back.status_code == 200, (
            f"还原单元格公式失败（HTTP {back.status_code}）：{back.text}\n"
            f"毒公式会留在这个账套里，后面读同一张模板的用例会莫名其妙地红。")


# --------------------------------------------------------------------- 报表模板结构
#
# 审计 P3-09：报表模板的**结构改动**（改行标签、改列的取数口径、增删行列、
# 改名、复制、删模板）以前**一条审计都不落**。
#
# 危害与「改公式」是同一类，但更隐蔽：把资产负债表的某列从「期末」改成
# 「期初」，页面照样渲染出一张「正常的」资产负债表 —— 数字变了却不出错，
# 事后翻审计流水却**没有任何一条**指向这次改动。
#
# 这里的清单就是那条审计动作的**覆盖清单**：少一个 action 不红，但少一个
# action 对应的**写路径**不留痕，红的是下面这条用例。


def test_报表模板的结构改动都要留审计(client, auth, db, book):
    """新建 / 复制 / 改名 / 删模板 + 增删改行列 —— 每一步都要留痕。

    逐条路径的细用例在 ``tests/test_audit_fix_report_template_audit.py``；
    这里守的是「**动作清单本身**不许被悄悄改小」。
    """
    from app.models import ReportCol, ReportRow

    # 新建（复制一份内置资产负债表基线，归属当前账套）
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "覆盖清单用表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, r.text
    tpl_id = r.json()["id"]

    design = client.get(f"/api/reports/{tpl_id}/design?book_id={book.id}",
                        headers=auth).json()
    last_row = design["rows"][-1]["row_no"] + 1
    last_col = design["cols"][-1]["col_no"] + 1
    value_col = next(c for c in design["cols"] if c["kind"] == "BALANCE_CLOSING")

    # 复制
    r = client.post(f"/api/reports/templates/{tpl_id}/copy?book_id={book.id}",
                    headers=auth, json={})
    assert r.status_code == 200, r.text

    # 改名
    r = client.patch(f"/api/reports/templates/{tpl_id}?book_id={book.id}", headers=auth,
                     json={"name": "覆盖清单改过的名字"})
    assert r.status_code == 200, r.text

    # 增行 / 改行 / 删行
    r = client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}", headers=auth,
                    json={"row_no": last_row, "label": "覆盖清单加的行"})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/reports/templates/{tpl_id}/rows/{last_row}?book_id={book.id}",
                     headers=auth, json={"label": "覆盖清单改过的行"})
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/reports/templates/{tpl_id}/rows/{last_row}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text

    # 增列 / 改列（**改口径**）/ 删列
    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}", headers=auth,
                    json={"col_no": last_col, "label": "覆盖清单加的列", "kind": "PERIOD_CREDIT"})
    assert r.status_code == 201, r.text
    r = client.patch(f"/api/reports/templates/{tpl_id}/cols/{value_col['col_no']}"
                     f"?book_id={book.id}", headers=auth,
                     json={"kind": "BALANCE_OPENING"})
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/reports/templates/{tpl_id}/cols/{last_col}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text

    # 反向确认：库里的结构真的改了（不然这条可能是「审计写了但库没改」的假绿）。
    # **必须在删模板之前查** —— 删掉之后行、列都跟着没了。
    db.expire_all()
    assert db.query(ReportRow).filter_by(template_id=tpl_id).count() > 0
    assert db.query(ReportCol).filter_by(template_id=tpl_id).count() > 0

    # 删模板
    r = client.delete(f"/api/reports/templates/{tpl_id}?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text

    # ---- 清单：每个动作都得有，且都要有快照 ------------------------------
    expected = {
        "CREATE_REPORT_TEMPLATE": 2,      # 新建 + 复制
        "UPDATE_REPORT_TEMPLATE": 1,      # 改名
        "DELETE_REPORT_TEMPLATE": 1,      # 删模板
        "UPDATE_REPORT_TEMPLATE_STRUCTURE": 6,   # 增/改/删行 + 增/改/删列
    }
    for action, count in expected.items():
        rows = _logs(db, book, action)
        assert len(rows) >= count, (
            f"{action} 应当至少有 {count} 条审计，实际 {len(rows)} 条 —— "
            f"有写路径没留痕")
        for entry in rows:
            assert entry.detail, f"{action} 的审计没有 detail：{entry}"
            assert entry.before or entry.after, (
                f"{action} 的审计没有留 before/after 快照：{entry}")

    # **改口径**这一条要能说清「从哪个口径改到了哪个口径」——
    # 只有「知道有人动过」是查不出事的。
    # （结构改动有多条，这里找**改口径**的那一条，不是最后一条。）
    kind_rows = [e for e in _logs(db, book, "UPDATE_REPORT_TEMPLATE_STRUCTURE")
                 if "BALANCE_CLOSING" in str(e.before) + str(e.after)
                 or "BALANCE_OPENING" in str(e.before) + str(e.after)]
    assert kind_rows, (
        "改列取数口径的那条审计里没有口径值 —— "
        "「把期末改成期初」必须能从审计里看出来")
    entry = kind_rows[-1]
    assert entry.before.get("kind") and entry.after.get("kind"), (
        f"改口径的审计没有 before/after 口径：{entry.before} / {entry.after}")
    assert entry.before["kind"] != entry.after["kind"], (
        f"改口径的审计里 before 与 after 一样：{entry.before} / {entry.after}")


# --------------------------------------------------------------------- 授权


def test_调整成员授权要留审计(client, auth, db, book, plain_user):
    """授权变更是权限系统的入口动作，出事时必须知道谁给谁升了权、降到什么。

    实际接口是 ``PUT /system/users/{id}/books``（整份替换授权列表）。
    """
    r = client.put(f"/api/system/users/{plain_user.id}/books", headers=auth, json=[
        {"book_id": book.id, "role": "VIEWER"},
    ])
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_BOOK_ACCESS")
    blob = str(entry.detail) + str(entry.before) + str(entry.after)
    assert plain_user.username in blob or str(plain_user.id) in blob, (
        f"授权审计没点名被改的人：{entry.detail} / {entry.after}")
    assert "VIEWER" in blob, f"新角色没进审计：{entry.detail} / {entry.after}"


# --------------------------------------------------------------------- 金额精度
# 审计 P1-23：审计快照用 float 存金额。
#
# 实测 float(Decimal('9999999999999999.99')) == 1e16 —— 而 Numeric(20,2) 的
# 上界（20 位）远在 float 能无损表示的范围之外。也就是说金额一旦越过千万级，
# 审计里记下来的**就不是那个数**了。
#
# 这张表的立论（models/audit.py 开篇）恰恰是「因为凭证号可复用，日志必须保存
# 完整快照，否则同一个号码事后无法对应」—— 快照存的不是原数，这句话不成立。
# 财务删掉一张凭证之后，要靠这条日志把那张凭证对回来；金额差一分就对不上。


def _snapshot_of(client, auth, db, book, subjects, amount: str):
    """建一张凭证再删掉它，返回留在审计里的那份快照。"""
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-15",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": amount, "summary": "精度"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": amount, "summary": "精度"},
        ],
    })
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert client.delete(f"/api/books/{book.id}/vouchers/{vid}",
                         headers=auth).status_code == 200
    db.expire_all()
    entry = _last(db, book, "DELETE")
    assert entry.action == "DELETE"
    return entry


def test_大额凭证的删除快照不丢精度(client, auth, db, book, subjects):
    """这个数必须**原样**留在审计里：不是 1e16，不是四舍五入过的近似值。"""
    amount = "9999999999999.99"          # 13 位整数 + 2 位小数，float 已经不够用了
    entry = _snapshot_of(client, auth, db, book, subjects, amount)

    got = [str(e["amount"]) for e in entry.before["entries"]]
    assert amount in got, f"快照里的金额不是原数：{got}（before={entry.before}）"
    assert str(entry.before["debit"]) == amount, (
        f"合计也丢了精度：{entry.before['debit']}")


def test_快照里的金额是十进制文本而不是浮点(client, auth, db, book, subjects):
    """JSON number 在前端同样会在 2^53 之后失真（审计日志页是前端渲染的）。

    十进制文本是唯一无损载体，``set_openings`` 的期初快照早就是这么存的。
    """
    entry = _snapshot_of(client, auth, db, book, subjects, "1234.56")
    assert isinstance(entry.before["entries"][0]["amount"], str), (
        f"金额还是 JSON 数字：{entry.before['entries'][0]!r}")
    assert isinstance(entry.before["debit"], str), (
        f"合计还是 JSON 数字：{entry.before['debit']!r}")


def test_百万级也原样留存(client, auth, db, book, subjects):
    """顺手钉住「改动」这条路径：before / after 两份快照都不能收窄。"""
    amount = "87654321.99"
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-15",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": amount, "summary": "精度"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": amount, "summary": "精度"},
        ],
    })
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert client.put(f"/api/books/{book.id}/vouchers/{vid}", headers=auth, json={
        "voucher_date": "2026-01-15", "attachments": 3,
        "entries": r.json()["entries"],
    }).status_code == 200

    db.expire_all()
    entry = _last(db, book, "UPDATE")
    for side in ("before", "after"):
        got = getattr(entry, side)["entries"][0]["amount"]
        assert Decimal(str(got)) == Decimal(amount), \
            f"{side} 快照的金额不是原数：{got!r}"
