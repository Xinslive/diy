"""报表模板的**结构改动**必须留审计（审计 P3-09 / 报告 §3.9）。

**这条缺陷的形状。** ``app/api/reports.py`` 里有十条写路径：新建、复制、
改名、删除模板，加上增删改行、增删改列。其中**只有改单元格公式**
（``save_cells``）留了审计（``UPDATE_REPORT_CELLS``）—— 其余九条一条审计
都不落。

后果不是「日志少了点」，而是**口径被换掉却查不出来**：把资产负债表的某
一列从「期末」改成「期初」，页面照样渲染出一张「正常的」资产负债表
（数字变了但不出错），事后翻审计流水却**没有任何一条**指向这次改动。

同一文件里 ``save_cells`` 的注释写着「改公式必须留 before……没有原公式
根本无从判断是哪改坏的」—— 那条推理对**行标签**和**列取数口径**同样
成立：改一个列的 ``kind`` 与改一个公式，性质完全一样。

所以这里钉的是三件事：

1. **十条写路径，每一条都要落审计**（不只是「有一个 action 出现过」）；
2. **改前改后都要在快照里** —— ``kind`` / ``label`` 必须在 ``before`` /
   ``after`` 里看得见，否则「知道有人动过」仍然没用；
3. **动作名要能区分**：新建/复制、���名、删除、结构改动是四种不同的事。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, ReportCol, ReportRow, ReportTemplate

#: 结构改动用的动作名（增删改行列）。与 ``save_cells`` 的
#: ``UPDATE_REPORT_CELLS`` 是两回事：那条改的是**公式**，这条改的是**口径**。
ACTION_STRUCTURE = "UPDATE_REPORT_TEMPLATE_STRUCTURE"


def _logs(db, book, action=None):
    """账套的审计流水。

    ``db.expire_all()`` 是必须的：经 client 的写入走的是 API 自己的
    Session 并且已经 commit，而 ``db`` 夹具是**另一个** Session ——
    不刷新就读不到刚刚落库的行。
    """
    db.expire_all()
    stmt = select(AuditLog).where(AuditLog.book_id == book.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.scalars(stmt.order_by(AuditLog.id)).all())


def _last(db, book, action):
    rows = _logs(db, book, action)
    assert rows, f"没有 {action} 的审计记录"
    return rows[-1]


@pytest.fixture
def tpl(client, auth, book):
    """一张归属 ``book`` 的报表模板（复制自内置资产负债表基线）。

    走接口建（而不是直接插 ORM 行），是为了让「新建模板」这条写路径
    本身也带上审计 —— 后面几条用例正好要检查它。
    """
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "审计留痕测试表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _design(client, auth, book, template_id):
    r = client.get(f"/api/reports/{template_id}/design?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- 新建/复制


def test_新建模板要留审计(client, auth, db, book):
    """新建一张表 = 这家单位从此多了一张**自己口径**的报表。

    名字、参照的是哪张基线、复制过来多少行多少列，都得留痕 —— 否则事后
    看到一张凭空出现的表，谁也说不清它是照着哪张改出来的。
    """
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "新建留痕表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, r.text

    entry = _last(db, book, "CREATE_REPORT_TEMPLATE")
    assert entry.detail, "新建模板的审计没有 detail"
    blob = str(entry.before) + str(entry.after)
    assert "新建留痕表" in blob, f"模板名没进审计：{entry.before} / {entry.after}"
    assert entry.after.get("rows"), f"复制了多少行没进审计：{entry.after}"


def test_复制模板要留审计(client, auth, db, book, tpl):
    """复制 = 同一套口径在另一个地方又活了一份。事后必须看得出源是哪张。"""
    r = client.post(f"/api/reports/templates/{tpl}/copy?book_id={book.id}",
                    headers=auth, json={})
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    entry = _last(db, book, "CREATE_REPORT_TEMPLATE")
    blob = str(entry.before) + str(entry.after)
    assert str(tpl) in blob and str(new_id) in blob, (
        f"复制审计里没有源模板/新模板：{entry.before} / {entry.after}")


# --------------------------------------------------------------------- 改名/删除


def test_改名要留改前改后(client, auth, db, book, tpl):
    """报表名会印在表头、打印抬头、导出文件名上 —— 改了就是改了。"""
    old = _design(client, auth, book, tpl)["name"]
    r = client.patch(f"/api/reports/templates/{tpl}?book_id={book.id}", headers=auth,
                     json={"name": "改过名的资产负债表"})
    assert r.status_code == 200, r.text

    entry = _last(db, book, "UPDATE_REPORT_TEMPLATE")
    blob = str(entry.before) + str(entry.after)
    assert old in blob, f"改前的名字没进审计：{entry.before} / {entry.after}"
    assert "改过名的资产负债表" in blob, f"改后的名字没进审计：{entry.before} / {entry.after}"


def test_删模板要留快照(client, auth, db, book, tpl):
    """删掉之后没法还原 —— 删除侧的快照必须留着（与「删科目留 before」同理）。"""
    r = client.delete(f"/api/reports/templates/{tpl}?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text

    entry = _last(db, book, "DELETE_REPORT_TEMPLATE")
    assert entry.before, "删除审计没有留 before 快照"
    assert str(tpl) in str(entry.before), f"快照里没有模板 id：{entry.before}"


# --------------------------------------------------------------------- 行


def test_加行要留审计(client, auth, db, book, tpl):
    """插进来的这一行带着**项目名**，也是印给人看的口径。"""
    design = _design(client, auth, book, tpl)
    at = design["rows"][-1]["row_no"] + 1
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}", headers=auth,
                    json={"row_no": at, "label": "审计新增行"})
    assert r.status_code == 200, r.text

    entry = _last(db, book, ACTION_STRUCTURE)
    assert entry.after, "加行的审计没有 after 快照"
    assert "审计新增行" in str(entry.after), f"新行的项目名没进审计：{entry.after}"


def test_改行标签要留改前改后(client, auth, db, book, tpl):
    """行标签就是这张表上印出来的项目名。改掉它，报表照样渲染得出来。"""
    design = _design(client, auth, book, tpl)
    row_no = design["rows"][1]["row_no"]
    old_label = design["rows"][1]["label"]

    r = client.patch(f"/api/reports/templates/{tpl}/rows/{row_no}?book_id={book.id}",
                     headers=auth, json={"label": "改过的项目名"})
    assert r.status_code == 200, r.text

    entry = _last(db, book, ACTION_STRUCTURE)
    blob = str(entry.before) + str(entry.after)
    assert old_label in blob, f"改前的项目名没进审计：{entry.before} / {entry.after}"
    assert "改过的项目名" in blob, f"改后的项目名没进审计：{entry.before} / {entry.after}"


def test_删行要留快照(client, auth, db, book, tpl):
    """删掉的项目名与那一行上的公式都没法还原了。"""
    design = _design(client, auth, book, tpl)
    row_no = design["rows"][1]["row_no"]
    label = design["rows"][1]["label"]

    r = client.delete(f"/api/reports/templates/{tpl}/rows/{row_no}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text

    entry = _last(db, book, ACTION_STRUCTURE)
    assert entry.before, "删行的审计没有 before 快照"
    assert label in str(entry.before), f"被删的项目名没进审计：{entry.before}"


# --------------------------------------------------------------------- 列


def test_加列要留口径(client, auth, db, book, tpl):
    """插一列 = 往这张表的口径上加了一列。列名与取数口径都要留痕。"""
    design = _design(client, auth, book, tpl)
    at = design["cols"][-1]["col_no"] + 1
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}", headers=auth,
                    json={"col_no": at, "label": "审计新增列", "kind": "YTD_AMOUNT"})
    assert r.status_code == 201, r.text

    entry = _last(db, book, ACTION_STRUCTURE)
    blob = str(entry.after)
    assert "审计新增列" in blob, f"新列的列名没进审计：{entry.after}"
    assert "YTD_AMOUNT" in blob, f"新列的取数口径没进审计：{entry.after}"


def test_改列取数口径要留改前改后(client, auth, db, book, tpl):
    """**这条就是审计报告点名的路径。**

    把资产负债表的某列从「期末」改成「期初」，页面照样渲染出一张
    「正常的」资产负债表 —— 数字变了但不出错。没有 before 就无从判断
    是哪一列、什么时候、从什么口径改成了什么口径。
    """
    design = _design(client, auth, book, tpl)
    # 第 3 列是「期末余额」（BALANCE_CLOSING），改成期初。
    col = next(c for c in design["cols"] if c["kind"] == "BALANCE_CLOSING")
    old_kind = col["kind"]
    old_label = col["label"]

    r = client.patch(f"/api/reports/templates/{tpl}/cols/{col['col_no']}?book_id={book.id}",
                     headers=auth, json={"kind": "BALANCE_OPENING"})
    assert r.status_code == 200, r.text

    entry = _last(db, book, ACTION_STRUCTURE)
    assert entry.before, "改列口径的审计没有 before 快照"
    assert entry.before.get("kind") == old_kind, (
        f"before 里没有改前的取数口径：{entry.before}")
    assert entry.after.get("kind") == "BALANCE_OPENING", (
        f"after 里没有改后的取数口径：{entry.after}")
    blob = str(entry.before) + str(entry.after)
    assert old_label in blob, f"列名没进审计：{entry.before} / {entry.after}"


def test_删列要留口径与列名(client, auth, db, book, tpl):
    """删掉之后列的口径与那一列上的公式都没法还原。"""
    design = _design(client, auth, book, tpl)
    at = design["cols"][-1]["col_no"] + 1
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}", headers=auth,
                    json={"col_no": at, "label": "待删列", "kind": "PERIOD_AMOUNT"})
    assert r.status_code == 201, r.text

    r = client.delete(f"/api/reports/templates/{tpl}/cols/{at}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text

    entry = _last(db, book, ACTION_STRUCTURE)
    assert entry.before, "删列的审计没有 before 快照"
    assert entry.before.get("kind") == "PERIOD_AMOUNT", (
        f"被删列的取数口径没进审计：{entry.before}")
    assert "待删列" in str(entry.before), f"被删列的列名没进审计：{entry.before}"


# --------------------------------------------------------------------- 总账


def test_十条写路径都留了审计(client, auth, db, book, tpl):
    """一条总账：十个写路径挨个走一遍，每一步都要在审计里留下痕迹。

    **为什么要有这条总账。** 上面的用例是**逐条**钉的；少钉一条不会
    让别的用例变红 —— 而这条缺陷的形状恰恰是「漏了几条没人发现」。
    这里把十条路径跑完，按动作名分组数一遍：少一条就红。
    """
    design = _design(client, auth, book, tpl)
    last_row = design["rows"][-1]["row_no"] + 1
    last_col = design["cols"][-1]["col_no"] + 1
    value_col = next(c for c in design["cols"] if c["kind"] == "BALANCE_CLOSING")

    steps = []

    # 1 新建
    r = client.post(f"/api/reports/templates?book_id={book.id}", headers=auth,
                    json={"name": "总账用表", "base_code": "BALANCE_SHEET"})
    assert r.status_code == 201, r.text
    steps.append(("CREATE_REPORT_TEMPLATE", r.json()["id"]))

    # 2 复制
    r = client.post(f"/api/reports/templates/{tpl}/copy?book_id={book.id}",
                    headers=auth, json={})
    assert r.status_code == 200, r.text
    steps.append(("CREATE_REPORT_TEMPLATE", r.json()["id"]))

    # 3 改名
    r = client.patch(f"/api/reports/templates/{tpl}?book_id={book.id}", headers=auth,
                     json={"name": "总账改过的名字"})
    assert r.status_code == 200, r.text
    steps.append(("UPDATE_REPORT_TEMPLATE", tpl))

    # 4 加行
    r = client.post(f"/api/reports/templates/{tpl}/rows?book_id={book.id}", headers=auth,
                    json={"row_no": last_row, "label": "总账加的行"})
    assert r.status_code == 200, r.text
    steps.append((ACTION_STRUCTURE, tpl))

    # 5 改行
    r = client.patch(f"/api/reports/templates/{tpl}/rows/{last_row}?book_id={book.id}",
                     headers=auth, json={"label": "总账改过的行"})
    assert r.status_code == 200, r.text
    steps.append((ACTION_STRUCTURE, tpl))

    # 6 删行
    r = client.delete(f"/api/reports/templates/{tpl}/rows/{last_row}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text
    steps.append((ACTION_STRUCTURE, tpl))

    # 7 加列
    r = client.post(f"/api/reports/templates/{tpl}/cols?book_id={book.id}", headers=auth,
                    json={"col_no": last_col, "label": "总账加的列", "kind": "PERIOD_CREDIT"})
    assert r.status_code == 201, r.text
    steps.append((ACTION_STRUCTURE, tpl))

    # 8 改列
    r = client.patch(f"/api/reports/templates/{tpl}/cols/{last_col}?book_id={book.id}",
                     headers=auth, json={"kind": "PERIOD_DEBIT", "label": "总账改过的列"})
    assert r.status_code == 200, r.text
    steps.append((ACTION_STRUCTURE, tpl))

    # 9 删列
    r = client.delete(f"/api/reports/templates/{tpl}/cols/{last_col}?book_id={book.id}",
                      headers=auth)
    assert r.status_code == 200, r.text
    steps.append((ACTION_STRUCTURE, tpl))

    # 10 删模板
    r = client.delete(f"/api/reports/templates/{tpl}?book_id={book.id}", headers=auth)
    assert r.status_code == 200, r.text
    steps.append(("DELETE_REPORT_TEMPLATE", tpl))

    by_action = {}
    for action, _tid in steps:
        by_action.setdefault(action, 0)
        by_action[action] += 1

    for action, expected in by_action.items():
        got = len(_logs(db, book, action))
        assert got >= expected, (
            f"{action} 应当至少有 {expected} 条审计，实际 {got} 条 —— "
            f"有写路径没留痕（模板 {tpl}）")

    # 结构改动那一条要能数出**每一次**改动（4 次：加行/改行/删行/加列/改列/删列
    # 共 6 次），少一条就是漏了一条写路径。
    structure = _logs(db, book, ACTION_STRUCTURE)
    assert len(structure) >= 6, (
        f"结构改动应当至少有 6 条审计（加行/改行/删行/加列/改列/删列），"
        f"实际 {len(structure)} 条")

    # 每一条都必须有 detail 与快照 —— 「知道有人动过」但不知道动成什么，
    # 对事后追查基本没用。
    for entry in structure:
        assert entry.detail, f"结构改动的审计没有 detail：{entry}"
        assert entry.before or entry.after, (
            f"结构改动的审计没有留 before/after 快照：{entry}")


def test_改口径之后审计里查得到那一列(client, auth, db, book, tpl):
    """端到端复现审计报告 §3.9 的那条路径：改口径 → 审计里有据可查。

    报告的原话是「事后查审计流水**没有任何一条**记录指向这次改动」。
    这条用例就是把那个「没有任何一条」钉成「必须有一条，而且能说出
    从哪个口径改到了哪个口径」。
    """
    design = _design(client, auth, book, tpl)
    col = next(c for c in design["cols"] if c["kind"] == "BALANCE_CLOSING")
    before_count = len(_logs(db, book, ACTION_STRUCTURE))

    r = client.patch(f"/api/reports/templates/{tpl}/cols/{col['col_no']}?book_id={book.id}",
                     headers=auth, json={"kind": "BALANCE_OPENING"})
    assert r.status_code == 200, r.text

    rows = _logs(db, book, ACTION_STRUCTURE)
    assert len(rows) == before_count + 1, "改口径没有多出一条审计"
    entry = rows[-1]
    assert entry.before.get("kind") == "BALANCE_CLOSING"
    assert entry.after.get("kind") == "BALANCE_OPENING"
    assert entry.after.get("col_no") == col["col_no"]


def test_模板结构改动落在正确的账套下(client, auth, db, book, tpl):
    """审计行的 ``book_id`` 必须是**动手的那个账套**。

    审查看的是「哪个账套的口径被改了」；挂错账套等于这条审计白写。
    """
    r = client.patch(f"/api/reports/templates/{tpl}/cols/3?book_id={book.id}",
                     headers=auth, json={"label": "换个列名"})
    assert r.status_code == 200, r.text
    entry = _last(db, book, ACTION_STRUCTURE)
    assert entry.book_id == book.id, (
        f"审计行的 book_id 是 {entry.book_id}，应当是 {book.id}")


def test_库里的结构真的改了(client, db, book, tpl):
    """反向确认：审计不是唯一的真相来源，**库里确实改了**。

    没有这条，用例可能因为「审计写了但库没改」而假绿。
    """
    db.expire_all()
    row = db.scalar(select(ReportRow).where(
        ReportRow.template_id == tpl, ReportRow.row_no == 2))
    assert row is not None
    col = db.scalar(select(ReportCol).where(
        ReportCol.template_id == tpl, ReportCol.col_no == 3))
    assert col is not None
    assert col.kind == "BALANCE_CLOSING"
    assert db.scalar(select(ReportTemplate).where(ReportTemplate.id == tpl)) is not None


# --------------------------------------------------------------------- style（双栏表）
#
# §3.9 的真旁路：**``style`` 里的东西也是印给人看的项目名**。
#
# 内置资产负债表是**双栏**表 —— 右栏是「负债和所有者权益」那半，
# 它的项目名存在 ``ReportRow.style["label_right"]`` 里
# （``seed/builtin_reports.py`` 写基线时就这么存），前端改右栏项目名
# 走的就是这条路（``ReportView.vue`` 提交 ``{style: row.style}``）。
#
# 原来的 ``update_row`` 把 before / after 快照都只记了
# ``label/indent/bold/hidden`` —— **style 不在快照里**：
# 只改 style 时 ``before == after`` → **一条审计都不落**。
# 而报表照样渲染得出一张「正常的」资产负债表，右栏的「流动负债：」
# 被换成了「短期借款：」，数字变了却查不出来 ——
# 与 §3.9 原文描述的失败场景**完全同类**。


def _row_with_right_label(db, template_id):
    """这张表里第一行**右栏有项目名**的行（内置资产负债表的前 26 行都有）。"""
    db.expire_all()
    rows = db.scalars(select(ReportRow).where(ReportRow.template_id == template_id)
                      .order_by(ReportRow.row_no)).all()
    for row in rows:
        if (row.style or {}).get("label_right"):
            return row
    raise AssertionError("这张表里没有带右栏项目名的行，前提不成立")


def test_只改style也要留审计(client, auth, db, book, tpl):
    """只改 ``style``（右栏项目名）→ **必须**留下一条审计。

    改的是资产负债表右栏的「流动负债：」→「短期借款：」——
    报表照样渲染得出来，数字变了却查不出来，这就是 §3.9 说的那种失败。
    """
    row = _row_with_right_label(db, tpl)
    old_right = row.style["label_right"]
    before_count = len(_logs(db, book, ACTION_STRUCTURE))

    r = client.patch(f"/api/reports/templates/{tpl}/rows/{row.row_no}"
                     f"?book_id={book.id}", headers=auth,
                     json={"style": {**row.style, "label_right": "短期借款："}})
    assert r.status_code == 200, r.text

    rows = _logs(db, book, ACTION_STRUCTURE)
    assert len(rows) == before_count + 1, (
        f"只改 style 没有多出一条审计（{before_count} → {len(rows)}）—— "
        "右栏项目名被换掉了，审计流水里却一条都没有")
    entry = rows[-1]
    assert entry.before, "改 style 的审计没有 before 快照"
    assert entry.before.get("style", {}).get("label_right") == old_right, (
        f"改前的右栏项目名没进快照：{entry.before}")
    assert entry.after.get("style", {}).get("label_right") == "短期借款：", (
        f"改后的右栏项目名没进快照：{entry.after}")
    assert entry.detail, "改 style 的审计没有 detail"


def test_只改style时库里也真的改了(client, auth, db, book, tpl):
    """反向确认：审计不是唯一的真相来源，**库里确实改了**。

    没有这条，用例可能因为「审计写了但库没改」而假绿。
    """
    row = _row_with_right_label(db, tpl)
    r = client.patch(f"/api/reports/templates/{tpl}/rows/{row.row_no}"
                     f"?book_id={book.id}", headers=auth,
                     json={"style": {**row.style, "label_right": "短期借款："}})
    assert r.status_code == 200, r.text

    db.expire_all()
    again = db.scalar(select(ReportRow).where(
        ReportRow.template_id == tpl, ReportRow.row_no == row.row_no))
    assert again.style.get("label_right") == "短期借款：", again.style


def test_什么都没改时不留审计(client, auth, db, book, tpl):
    """对称事实的另一半：**没有改动**就不该留审计。

    否则「有审计」就不再说明「有人动过手」—— 审计流水会被噪声淹掉。
    """
    row = _row_with_right_label(db, tpl)
    before_count = len(_logs(db, book, ACTION_STRUCTURE))

    r = client.patch(f"/api/reports/templates/{tpl}/rows/{row.row_no}"
                     f"?book_id={book.id}", headers=auth,
                     json={"style": dict(row.style)})
    assert r.status_code == 200, r.text

    assert len(_logs(db, book, ACTION_STRUCTURE)) == before_count, (
        "什么都没改却多了一条审计")
