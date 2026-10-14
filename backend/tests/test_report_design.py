"""报表结构编辑：插行/删行/插列/删列，以及**公式引用的同步改写**。

「自由可调」不只是改单元格公式，还得能增删行列。这里最要命的是后者：
公式是按地址写的（``合计(C28:C30)``、``C16+C31``），一旦在中间插一行，
下面的地址全部错位，而报表照样能算出一个「看起来正常」的数字 —— 静默出错比报错危险得多。

**坐标跟着版式走。**
------------------
内置资产负债表是一行两栏的会小企01表：8 列、33 行，
资产侧第 3/4 列（期末/年初）、负债和所有者权益侧第 7/8 列。
本文件所有坐标都按这个版式写 —— 第 1/2/5/6 列是 STATIC/LINE_NO，
内容在行上、**没有公式**，所以要拿一条真公式去测改写，一律用第 3 列。
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import ReportCell, ReportCol, ReportRow, ReportTemplate

#: 资产侧的「期末余额」列 —— 全文件默认操作的数值列。
CLOSING = 3
#: 负债和所有者权益侧的「期末余额」列。
R_CLOSING = 7


@pytest.fixture
def tpl_id(db, book):
    """一张干净的资产负债表模板（每个用例一份，互不干扰）。

    依赖 ``book`` 是为了写权限：报表模板是**全局**资源，改它要求显式给出
    ``book_id`` 且在该账套是 OWNER（审计 P1-5 的 fail-closed 门禁）。
    下面每个写接口都必须带 ``?book_id={book.id}`` —— 缺省会被 400 挡掉。
    """
    import uuid

    src = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    tag = uuid.uuid4().hex[:8]

    def cp(code):
        return db.scalar(select(ReportTemplate).where(ReportTemplate.code == code))

    t = ReportTemplate(code=f"TEST_{tag}", name=f"测试报表{tag}", type="CUSTOM", remark="")
    db.add(t)
    db.flush()
    for r in db.scalars(select(ReportRow).where(ReportRow.template_id == src.id)).all():
        db.add(ReportRow(template_id=t.id, row_no=r.row_no, label=r.label,
                         indent=r.indent, bold=r.bold, hidden=r.hidden, style=r.style))
    for c in db.scalars(select(ReportCol).where(ReportCol.template_id == src.id)).all():
        db.add(ReportCol(template_id=t.id, col_no=c.col_no, label=c.label,
                         kind=c.kind, hidden=c.hidden, width=c.width))
    for c in db.scalars(select(ReportCell).where(ReportCell.template_id == src.id)).all():
        db.add(ReportCell(template_id=t.id, row_no=c.row_no, col_no=c.col_no,
                          formula=c.formula))
    db.commit()
    db.refresh(t)
    return t.id


def formulas(db, tid, col=CLOSING):
    """读回模板里某一列的公式。

    ``expire_all`` 不是可有可无：写接口跑在**另一个** Session 里，本测试的
    Session 身份映射里还留着改写前的那个 ReportCell 对象，直接查会拿到
    旧值 —— 于是一条真修好了的用例会红成「好像没修」，或者反过来，
    一条没修的用例会绿成「好像修好了」。两种都是假绿。
    """
    db.expire_all()
    return {c.row_no: c.formula for c in db.scalars(
        select(ReportCell).where(ReportCell.template_id == tid,
                                 ReportCell.col_no == col)).all()}


def labels(db, tid):
    db.expire_all()
    return {r.row_no: r.label for r in db.scalars(
        select(ReportRow).where(ReportRow.template_id == tid).order_by(ReportRow.row_no)).all()}


def cols(db, tid):
    db.expire_all()
    return list(db.scalars(select(ReportCol).where(ReportCol.template_id == tid)
                          .order_by(ReportCol.col_no)).all())


def kinds(db, tid):
    return [c.kind for c in cols(db, tid)]


_ADDR_RE = re.compile(r"[A-Z]+(\d+)")


def summed_items(db, tid, row_no, col=CLOSING):
    """一条合计公式**实际加的是哪几个项目**。

    比字符串更耐改版式：改写对了，加的还是原来那几行；改写错了
    （少一项、串到别处），这里立刻看得出项目名变了。
    """
    lab = labels(db, tid)
    formula = formulas(db, tid, col=col)[row_no]
    return sorted(lab[int(m)] for m in _ADDR_RE.findall(formula) if int(m) in lab)


# ------------------------------------------------------- 插行：引用必须跟着挪


def test_insert_row_shifts_references_below(client, auth, tpl_id, db, book):
    """在第 10 行插一行：合计里的 C10/C15 必须变成 C11/C16，否则合计算的是错的行。

    双栏表**两侧都得对**：资产侧的 C16 和负债侧的 G12 一起下移。
    只测一侧的话，右半边整体错位一格 —— 而它照样渲染得出一张漂亮的表。
    """
    before = formulas(db, tpl_id)
    assert before[16] == "C2+C3+C4+C5+C6+C7+C8+C9+C10+C15", before

    r = client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": 10, "label": "新增行"})
    assert r.status_code == 200, r.text

    after = formulas(db, tpl_id)
    # 第 10 行往前插，原本 10 行及以下的下移一行；引用 >= 10 的跟着挪
    assert after[17] == "C2+C3+C4+C5+C6+C7+C8+C9+C11+C16", after[17]
    # 右半边：流动负债合计（原本第 12 行）下移到 13，行引用同样整体下移
    assert formulas(db, tpl_id, col=R_CLOSING)[13] == \
        "G2+G3+G4+G5+G6+G7+G8+G9+G11+G12"
    # 第 2 行的公式是科目取数、不含行引用，插行不该碰它
    assert after[2] == before[2], after[2]
    # 第 10 行原本只是个明细行（无公式），插进去的行同样没有公式
    assert after.get(10, "") == "", after.get(10)


def test_insert_row_inside_a_range_does_not_change_what_is_summed(client, auth, tpl_id, db, book):
    """插在合计覆盖的范围**里面**，合计加的仍然是原来那几项。

    旧版式的合计是 ``合计(B2:B8)`` 区域，插在范围里区域会变长（连新行一起加进去）。
    双栏版式改成逐项相加（``C2+C3+…+C15``），插进来的那一行是个**空行** ——
    把它算进流动资产合计反而是错的。所以这条守住的是更有价值的那一半：
    「加的还是原来那几项，一项不多一项不少」，行引用整体位移了但**指向没变**。
    """
    before = summed_items(db, tpl_id, 16)
    assert "其他流动资产" in before, before

    r = client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": 3, "label": "插在合计范围内"})
    assert r.status_code == 200, r.text

    lab = labels(db, tpl_id)
    assert lab[3] == "插在合计范围内", lab.get(3)     # 行确实插进去了
    after = summed_items(db, tpl_id, 17)             # 合计行自己下移到 17
    got = formulas(db, tpl_id)[17]
    assert after == before, f"第 17 行公式 {got} 加的是 {after}；应当还是 {before}"


def test_delete_row_shrinks_references(client, auth, tpl_id, db, book):
    """删掉合计范围内的一行，C2:C15 的加项要收掉，否则会把下一行重复加进去。

    新版式是单点相加，没有「区域收缩」这条规则可测；真正要守的是
    **被删的那一项按 0 计**、其余项重新指对行。
    """
    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/4?book_id={book.id}",
                         headers=auth).status_code == 200
    after = formulas(db, tpl_id)
    # 原本 C2+C3+C4+…+C10+C15；第 4 行（应收票据）被删 → 按 0 计，其余下移一位
    assert after[15] == "C2+C3+0+C4+C5+C6+C7+C8+C9+C14", after[15]
    # 被删掉的那一项确实不在合计里了，其余项一个没丢
    items = summed_items(db, tpl_id, 15)
    assert "应收票据" not in items, items
    assert "其他流动资产" in items and "货币资金" in items, items
    # 「资产总计」原本在第 32 行写的是 C16+C31，删行后上移到 31 并改写引用
    assert after[31] == "C15+C30", after[31]


def test_row_labels_follow_the_shift(client, auth, tpl_id, db, book):
    """行标签也要跟着挪，不能出现两个「应收账款」。"""
    client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                headers=auth, json={"row_no": 3, "label": "插进来的"})
    lab = labels(db, tpl_id)
    # 双栏版式：2 货币资金 / 3 短期投资 / 4 应收票据 / 5 应收账款，插一行后整体下移
    assert lab[3] == "插进来的", lab.get(3)
    assert lab[4] == "短期投资", lab.get(4)
    assert lab[5] == "应收票据", lab.get(5)
    assert lab[6] == "应收账款", lab.get(6)
    assert len([v for v in lab.values() if v == "应收账款"]) == 1, lab


# ---------------------------------------------------------------- 行的属性


def test_can_rename_and_restyle_a_row(client, auth, tpl_id, db, book):
    """改标签/缩进/加粗/隐藏 —— 只靠插删行拼不出想要的表。"""
    r = client.patch(f"/api/reports/templates/{tpl_id}/rows/9?book_id={book.id}", headers=auth,
                     json={"label": "其他应收款（改过名）", "indent": 1, "bold": True,
                           "hidden": False})
    assert r.status_code == 200, r.text
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == tpl_id,
                                             ReportRow.row_no == 9))
    assert row.label == "其他应收款（改过名）" and row.indent == 1 and row.bold is True


def test_hidden_row_disappears_from_render(client, auth, tpl_id, book):
    """隐藏行不该出现在渲染结果里（打印时也少一行）。"""
    before = client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()
    assert any(r["row_no"] == 9 for r in before["rows"])

    assert client.patch(f"/api/reports/templates/{tpl_id}/rows/9?book_id={book.id}",
                        headers=auth, json={"hidden": True}).status_code == 200
    assert not client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()["rows"] \
        or all(r["row_no"] != 9 or r["hidden"] for r in
               client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()["rows"])


# --------------------------------------------------------------------- 列


def test_insert_column_reletters_references(client, auth, tpl_id, db, book):
    """插一列会让整张表右移一格，公式里的列字母必须跟着改，否则整张表算错。

    双栏表要盯住两侧：资产侧的 C16 变 D16，负债侧的 G12 变 H12。
    """
    before = formulas(db, tpl_id)
    assert before[16] == "C2+C3+C4+C5+C6+C7+C8+C9+C10+C15", before

    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 2, "label": "上期数"})
    assert r.status_code == 201, r.text
    assert r.json()["letter"] == "B", r.json()

    after = formulas(db, tpl_id, col=4)          # 老第 3 列（C）现在是第 4 列（D）
    assert after[16] == "D2+D3+D4+D5+D6+D7+D8+D9+D10+D15", after[16]
    # 老第 7 列（G，负债侧期末余额）现在是第 8 列（H）
    assert formulas(db, tpl_id, col=8)[12] == \
        "H2+H3+H4+H5+H6+H7+H8+H9+H10+H11"


def test_new_column_inherits_neighbour_kind(client, auth, tpl_id, db, book):
    """插在「期末余额」右边，新列默认也是期末口径，不用用户自己想。"""
    client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                headers=auth, json={"col_no": 3, "label": "对比列"})

    # 第 3 列左边的「数值列」是第 3 列自己（前面两列是 STATIC 和 LINE_NO，
    # 都不算数值列），所以插在它右边的新列沿用期末口径
    new = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl_id,
                                            ReportCol.col_no == 3))
    assert new.kind == "BALANCE_CLOSING", new.kind


def test_行次列不会被新列继承(client, auth, tpl_id, db, book):
    """**行次列（LINE_NO）不能被继承成新列的取数口径** —— 两侧都要挡住。

    继承了会造出一个幽灵列：它永远算不出数（行次存在行的 style 里，不来自科目），
    而更要命的是界面会把行次列里那些「1」「31」当成金额显示在表上。

    正确的继承规则是「左边最近的**数值列**」：插在第 3 列右边继承期末口径，
    插完那一列之后第 7 列右边最近的数值列已经是年初余额了（第 5 列），
    所以拿到 BALANCE_OPENING —— 规则没坏，只是左邻换了人。
    """
    from app.reports.formula import NON_FORMULA_KINDS

    # 第 1 次：插在资产侧行次列（第 2 列）右边，左边最近的数值列就是第 3 列（期末）
    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 3, "label": "插在行次右边"})
    assert r.status_code == 201, r.text
    first = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl_id, ReportCol.col_no == 3))
    assert first.kind not in NON_FORMULA_KINDS, first.kind
    assert first.kind == "BALANCE_CLOSING", first.kind

    # 第 2 次：插在负债侧行次列（插完上一列后它正好是第 7 列）右边
    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 7, "label": "插在行次右边"})
    assert r.status_code == 201, r.text
    second = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl_id,
                                                ReportCol.col_no == 7))
    assert second.kind not in NON_FORMULA_KINDS, second.kind
    assert second.kind == "BALANCE_OPENING", second.kind


def test_不能显式插出一个行次列或项目列(client, auth, tpl_id, book):
    """STATIC / LINE_NO 只能由模板播种产生，接口不接受它们当新列的口径。

    没有这条，用户可以在设计模式里手搓出第三、第四个「行次列」——
    那样这张表既有多个 STATIC 又有多个 LINE_NO，播种器和求值器
    「一栏一个」的假设就全散了。
    """
    for kind in ("LINE_NO", "STATIC"):
        r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                        headers=auth, json={"col_no": 3, "label": "怪列", "kind": kind})
        assert r.status_code == 400, kind
        assert "未知的取数口径" in r.json()["detail"], r.json()


def test_cannot_delete_the_static_label_column(client, auth, tpl_id, book):
    """双栏表有两列 STATIC（资产 / 负债和所有者权益），两列都不能删。"""
    for col_no in (1, 5):
        r = client.delete(f"/api/reports/templates/{tpl_id}/cols/{col_no}?book_id={book.id}",
                          headers=auth)
        assert r.status_code == 400, col_no
        assert "固定列" in r.json()["detail"], r.json()


def test_行次列同样不能删(client, auth, tpl_id, book):
    """两列 LINE_NO 也是固定列：内容在行的 style 里，删了表就没有行次了。

    当年只有项目列是「固定列」，行次列是新加的；
    这条钉住的是它们**同等待遇**，不是「行次列忘了保护」。
    """
    for col_no in (2, 6):
        r = client.delete(f"/api/reports/templates/{tpl_id}/cols/{col_no}?book_id={book.id}",
                          headers=auth)
        assert r.status_code == 400, col_no
        assert "行次" in r.json()["detail"], r.json()


def test_cannot_delete_the_last_value_column(client, auth, tpl_id, book):
    """数值列（余额/发生额口径）删光了，剩下一张一格数都算不出来的表，必须挡住。

    8 列里只有第 3/4/7/8 列是数值列；第 1/2/5/6 列是 STATIC/LINE_NO，
    **不参与计数** —— 把它们算进去的话，一张只剩标签列和行次列的表也能通过校验。
    资产侧的两列数值列都排在第 3 列上（删一次，后面几列都要重新编号），
    删光资产侧之后只剩负债和所有者权益侧那两列。
    """
    for i in range(2):
        r = client.delete(f"/api/reports/templates/{tpl_id}/cols/3?book_id={book.id}",
                          headers=auth)
        assert r.status_code == 200, f"第 {i + 1} 次删除：{r.status_code} {r.text[:120]}"
    # 现在剩下 1 STATIC / 2 LINE_NO / 3 STATIC / 4 LINE_NO / 5 期末余额 / 6 年初余额
    assert client.delete(f"/api/reports/templates/{tpl_id}/cols/5?book_id={book.id}",
                         headers=auth).status_code == 200
    r = client.delete(f"/api/reports/templates/{tpl_id}/cols/5?book_id={book.id}", headers=auth)
    assert r.status_code == 400 and "至少要保留" in r.json()["detail"], r.json()


def test_delete_column_shrinks_references(client, auth, tpl_id, db, book):
    """插一列又删掉它，第 3 列的引用必须原样回到 C，不能被改得面目全非。"""
    client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                headers=auth, json={"col_no": 3, "label": "临时列"})
    assert client.delete(f"/api/reports/templates/{tpl_id}/cols/3?book_id={book.id}",
                         headers=auth).status_code == 200
    after = formulas(db, tpl_id, col=3)
    assert after[16] == "C2+C3+C4+C5+C6+C7+C8+C9+C10+C15", after[16]


# ------------------------------------------------- 删行/删列：不得静默别名（P1-13）
#
# 审计给的例子：4103「本年利润」引用 ``B35``，删掉第 35 行之后 ``B35``
# 静默指向原第 36 行「未分配利润」—— 报表照常渲染出**错误的数字而不报错**。
# 这是整套报表里最危险的一类错误：没有异常、没有红字，财务只会以为数算对了。


def test_删掉被引用的行不会别名到相邻行(client, auth, tpl_id, db, book):
    """删第 29 行，引用 C29 的公式必须变成按 0 计算，而不是留在 C29。

    审计给的原话：4103「本年利润」引用 ``B35``，删掉第 35 行之后 ``B35``
    **静默指向原第 36 行「未分配利润」** —— 因为删除只做了「行号 >= 36 的减一」，
    35 号这个引用原封不动地留了下来，而 36 行已经上移占住了第 35 行的位置。
    报表照常渲染出**错误的数字而不报错**：没有异常、没有红字。

    这里照着会小企01表的坐标复现同一件事：第 30 行引用它上面的第 29 行。
    """
    cell = db.scalar(select(ReportCell).where(ReportCell.template_id == tpl_id,
                                               ReportCell.row_no == 30, ReportCell.col_no == CLOSING))
    cell.formula = "C29+C28"
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/29?book_id={book.id}",
                         headers=auth).status_code == 200
    # 被删的行整体上移一格：30 -> 29；引用 C29（被删）不再指 C28
    assert formulas(db, tpl_id)[29] == "0+C28", formulas(db, tpl_id)[29]


def test_删行后区域收缩而不是留下非法公式(client, auth, tpl_id, db, book):
    """``合计(C28:C30)`` 删掉 29 行要收成 ``合计(C28:C29)``。

    以前是把被删的地址**清空**，产出 ``合计(C28:)`` —— 那是语法错误，
    这张表从此再也渲染不出来（审计 P2-17）。

    内置模板现在用的是单点相加，这条得手工摆一个区域公式进去 ——
    区域收缩是改写器自己的规则，跟用哪种模板无关。
    """
    from app.reports.formula import Parser

    cell = db.scalar(select(ReportCell).where(ReportCell.template_id == tpl_id,
                                               ReportCell.row_no == 30, ReportCell.col_no == CLOSING))
    cell.formula = "合计(C28:C30)"
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/29?book_id={book.id}",
                         headers=auth).status_code == 200
    got = formulas(db, tpl_id)[29]
    assert got == "合计(C28:C29)", got
    # 产出必须还是能解析的公式（否则整张表这一列报废）
    Parser(got).parse()


def test_删掉第1行不再产出0行这种非法地址(client, auth, tpl_id, db, book):
    """删第 1 行时引用 C1 的公式按 0 处理。

    以前删除传的是 ``row_shift=(row_no + 1, -1)``，1 号引用根本不满足
    ``>= 2``，于是留在原地 —— 而第 2 行已经上移占住了第 1 行的位置，
    ``C1+C2`` 变成 ``C1+C1``，**同一个格子被加了两遍**，且全程不报错。
    """
    cell = db.scalar(select(ReportCell).where(ReportCell.template_id == tpl_id,
                                               ReportCell.row_no == 33, ReportCell.col_no == CLOSING))
    cell.formula = "C1+C2"
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/1?book_id={book.id}",
                         headers=auth).status_code == 200
    assert formulas(db, tpl_id)[32] == "0+C1", formulas(db, tpl_id)[32]


def test_删掉被引用的列不会别名到左边那一列(client, auth, tpl_id, db, book):
    """列与行是同一个 bug：删列以前也只做位移，引用会静默指向左邻列。

    被改的公式放在**留在表里的**那一列上（第 3 列），由它引用第 4 列：
    删掉第 4 列以后这条引用必须变成 0，而不是指向从右边挪过来的那一列。
    """
    cell = db.scalar(select(ReportCell).where(ReportCell.template_id == tpl_id,
                                               ReportCell.row_no == 2, ReportCell.col_no == CLOSING))
    cell.formula = "D3+C3"
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/cols/4?book_id={book.id}",
                         headers=auth).status_code == 200
    assert formulas(db, tpl_id, col=3)[2] == "0+C3", formulas(db, tpl_id, col=3)[2]


# --------------------------------------------------- $ 锁定引用（P2-17 第一半）


def test_美元符号锁定引用在插行时不被改写(client, auth, tpl_id, db, book):
    """``$C$5`` 的含义就是不随插删行列移动，改写时必须原样保留。

    以前 ``$`` 会在改写时丢掉，而且 tokenizer 干脆不认 ``$``：
    公式存得进去、渲染时必报「无法识别的字符 '$'」。
    """
    from app.reports.formula import Evaluator, Parser

    cell = db.scalar(select(ReportCell).where(ReportCell.template_id == tpl_id,
                                               ReportCell.row_no == 33, ReportCell.col_no == CLOSING))
    cell.formula = "$C$5+C10"
    db.commit()

    assert client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                       headers=auth, json={"row_no": 10, "label": "插一行"}).status_code == 200
    got = formulas(db, tpl_id)[34]
    assert got == "$C$5+C11", got   # 锁定的 C5 不动，普通的 C10 跟着下移

    # 而且现在真的能解析、真的能算（以前渲染期直接报错）
    node = Parser(got).parse()
    assert node is not None

    class _Ctx:
        def cell(self, r, c):
            return Decimal("1") if (r, c) == (5, 3) else Decimal("0")

        def subject_id(self, code):
            return None

        def resolve_period(self, expr):
            return None

    assert Evaluator(_Ctx()).run(got) == Decimal("1")


def test_column_label_and_width_editable(client, auth, tpl_id, db, book):
    r = client.patch(f"/api/reports/templates/{tpl_id}/cols/3?book_id={book.id}", headers=auth,
                     json={"label": "期末数（元）", "width": 180, "hidden": True})
    assert r.status_code == 200, r.text
    c = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl_id, ReportCol.col_no == 3))
    assert c.label == "期末数（元）" and c.width == 180 and c.hidden is True


def test_rejects_unknown_column_kind(client, auth, tpl_id, book):
    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}", headers=auth,
                    json={"col_no": 2, "label": "怪列", "kind": "NOT_A_KIND"})
    assert r.status_code == 400
    assert "未知的取数口径" in r.json()["detail"], r.json()


# ------------------------------------------- 双栏版式本身的不变量（不许被拆掉）


def test_内置资产负债表是镜像的八列(db, book):
    """一行两栏的会小企01表：8 列，左右两半是**镜像**。

    ``[STATIC, LINE_NO, BALANCE_CLOSING, BALANCE_OPENING] * 2`` ——
    当年这里断言的是「恰好 4 列、恰好一个 STATIC 项目列」。
    那个前提随着双栏版式一起作废了：会小企01表本来就是左右各一栏，
    两个 STATIC（资产 / 负债和所有者权益）和两个 LINE_NO 才是对的。
    真正要守的不变量变成了「两半互为镜像」——
    多一列少一列会让右侧的合计公式引用到错误的列。
    """
    src = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    got = [(c.col_no, c.label, c.kind) for c in cols(db, src.id)]
    assert [g[0] for g in got] == [1, 2, 3, 4, 5, 6, 7, 8], got
    assert [g[2] for g in got] == ["STATIC", "LINE_NO", "BALANCE_CLOSING",
                                   "BALANCE_OPENING"] * 2, got
    assert [g[1] for g in got] == ["资  产", "行次", "期末余额", "年初余额",
                                   "负债和所有者权益", "行次", "期末余额", "年初余额"], got

    # 两个 STATIC + 两个 LINE_NO：一个都不能多、一个都不能少
    ks = kinds(db, src.id)
    assert ks.count("STATIC") == 2 and ks.count("LINE_NO") == 2, ks
    assert len(src_rows(db, src.id)) == 33


def src_rows(db, tid):
    db.expire_all()
    return list(db.scalars(select(ReportRow).where(ReportRow.template_id == tid)
                          .order_by(ReportRow.row_no)).all())


# -------------------------------------------------- 隐藏：得真的不打印出来


def test_hidden_row_is_dropped_from_print_and_excel(client, auth, tpl_id, book, periods):
    """hidden 是给设计者用的开关，打印和导出都必须尊重它。

    之前是「字段传下去了但没人用」：界面上标了隐藏，打印照打不误。
    """
    import io as _io

    from openpyxl import load_workbook

    assert client.patch(f"/api/reports/templates/{tpl_id}/rows/5?book_id={book.id}",
                        headers=auth, json={"hidden": True}).status_code == 200

    html = client.get(f"/api/books/{book.id}/print/report.html", headers=auth,
                      params={"template_id": tpl_id, "period_id": periods[12].id})
    assert html.status_code == 200, html.text
    assert "应收账款" not in html.text, "隐藏行仍出现在打印页里"

    x = client.get(f"/api/books/{book.id}/print/report.xlsx", headers=auth,
                   params={"template_id": tpl_id, "period_id": periods[12].id})
    assert x.status_code == 200, x.text
    ws = load_workbook(_io.BytesIO(x.content)).active
    # 应收账款在第 5 行、资产侧（第 1 列），右半边的第 5 列是「应付利润」，
    # 不受影响 —— 所以这里只看第 1 列就够了
    first_col = [str(r[0].value or "") for r in ws.iter_rows(min_col=1, max_col=1)]
    assert not any("应收账款" in s for s in first_col), first_col


def test_hidden_column_does_not_shift_the_other_columns(client, auth, tpl_id, book, periods):
    """隐藏中间一列时，剩下的数字**不能整列错位**。

    单元格按 col_no 对齐而不是按下标：隐藏掉资产侧的「期末余额」列后，
    同侧的「年初余额」还在它自己的位置上，不能因为少了一列就整体左移一格 ——
    而左移一格的话，数字会跑到「负债和所有者权益」那个标题底下去，
    打印出来是一张左右两栏**串味**的表。

    导出侧把隐藏列整列**去掉**（不是留空、也不是压掉一行），
    所以表头 = 原 8 列表头去掉第 3 列。
    """
    import io as _io

    from openpyxl import load_workbook

    # 资产侧的「期末余额」（第 3 列）隐藏
    assert client.patch(f"/api/reports/templates/{tpl_id}/cols/3?book_id={book.id}",
                        headers=auth, json={"hidden": True}).status_code == 200

    x = client.get(f"/api/books/{book.id}/print/report.xlsx", headers=auth,
                   params={"template_id": tpl_id, "period_id": periods[12].id})
    assert x.status_code == 200, x.text
    ws = load_workbook(_io.BytesIO(x.content)).active
    header = [str(c.value or "") for c in next(ws.iter_rows(min_row=4, max_row=4))]
    assert header == ["资  产", "行次", "年初余额",
                      "负债和所有者权益", "行次", "期末余额", "年初余额"], header
    # 顺序原样保留：资产侧剩下的两列仍在「负债和所有者权益」之前
    assert header.index("资  产") < header.index("年初余额") < header.index("负债和所有者权益")


def test_hidden_row_still_editable_in_design_view(client, auth, tpl_id, db, book):
    """界面要能取消隐藏，所以 render 里仍要带着 hidden 行 —— 过滤只发生在打印侧。"""
    client.patch(f"/api/reports/templates/{tpl_id}/rows/5?book_id={book.id}",
                 headers=auth, json={"hidden": True})
    rows = client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()["rows"]
    hidden = [r for r in rows if r["row_no"] == 5]
    assert hidden and hidden[0]["hidden"] is True, hidden


def test_column_inserted_next_to_label_column_is_not_a_ghost(client, auth, tpl_id, db, book):
    """插在「资产」列右边（第 2 列）是最常见的用法。

    这里最容易踩的坑：新列把左邻的 STATIC 口径继承了，于是变成一个既不显示、
    又没有任何取数口径的幽灵列 —— 界面看着像插成功了，导出却没有这一列。
    双栏版式里左邻还可能是行次列（LINE_NO），那更糟（见下面两条）。
    """
    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 2, "label": "新增列"})
    assert r.status_code == 201, r.text

    new = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl_id, ReportCol.col_no == 2))
    assert new.kind not in ("STATIC", "LINE_NO"), new.kind
    assert new.kind == "BALANCE_CLOSING", new.kind     # 沿用右边「期末余额」的口径

    got = cols(db, tpl_id)
    assert [c.col_no for c in got] == [1, 2, 3, 4, 5, 6, 7, 8, 9], [c.col_no for c in got]
    # 插一列不许凭空多出第三个 STATIC / LINE_NO —— 插完仍然是「两个项目列 + 两个行次列」，
    # 只是位置整体右移了。当年的断言是「项目列必须仍然只有一个」，
    # 双栏版式下正确的不变量是「每种仍然各有两个」。
    ks = [c.kind for c in got]
    assert ks.count("STATIC") == 2 and ks.count("LINE_NO") == 2, ks
    assert ks == ["STATIC", "BALANCE_CLOSING", "LINE_NO", "BALANCE_CLOSING",
                  "BALANCE_OPENING", "STATIC", "LINE_NO", "BALANCE_CLOSING",
                  "BALANCE_OPENING"], ks


def test_column_kind_can_be_chosen_explicitly(client, auth, tpl_id, db, book):
    assert client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}", headers=auth,
                       json={"col_no": 4, "label": "本年累计", "kind": "YTD_AMOUNT"}
                       ).status_code == 201
    new = db.scalar(select(ReportCol).where(ReportCol.template_id == tpl_id, ReportCol.col_no == 4))
    assert new.kind == "YTD_AMOUNT", new.kind
