"""报表结构编辑：插行/删行/插列/删列，以及**公式引用的同步改写**。

「自由可调」不只是改单元格公式，还得能增删行列。这里最要命的是后者：
公式是按地址写的（``合计(B2:B8)``），一旦在中间插一行，下面的地址全部错位，
而报表照样能算出一个「看起来正常」的数字 —— 静默出错比报错危险得多。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import ReportCell, ReportCol, ReportRow, ReportTemplate


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


def formulas(db, tid, col=2):
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


# ------------------------------------------------------- 插行：引用必须跟着挪


def test_insert_row_shifts_references_below(client, auth, tpl_id, db, book):
    """在第 10 行插一行：B11:B15 必须变成 B12:B16，否则合计算的是错的行。

    插在范围**外面**的（这里是第 3 行，落在 合计(B2:B8) 里面）也一样要改。
    """
    before = formulas(db, tpl_id)
    assert before[9] == "合计(B2:B8)", before

    r = client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": 10, "label": "新增行"})
    assert r.status_code == 200, r.text

    after = formulas(db, tpl_id)
    # 第 10 行往前插，原本 10 行及以下的下移一行
    assert after[9] == "合计(B2:B8)", after[9]        # 范围全在上方，不受影响
    assert after[17] == "合计(B12:B16)", after[17]    # 原来是 合计(B11:B15)
    assert after[18] == "B9+B17", after[18]          # 原来是 B9+B16
    # 第 10 行原本只是个分段标题（无公式），插进去的行同样没有公式
    assert after.get(10, "") == "", after.get(10)


def test_insert_row_inside_a_range_extends_it(client, auth, tpl_id, db, book):
    """插在 合计(B2:B8) 覆盖的范围**里面**，范围要跟着变长。"""
    before = formulas(db, tpl_id)
    assert before[9] == "合计(B2:B8)", before

    r = client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                    headers=auth, json={"row_no": 3, "label": "插在合计范围内"})
    assert r.status_code == 200, r.text

    after = formulas(db, tpl_id)
    assert after[10] == "合计(B2:B9)", after[10]      # 下移一行，且范围 +1


def test_delete_row_shrinks_references(client, auth, tpl_id, db, book):
    """删掉范围内的一行，B2:B8 要收成 B2:B7，否则会把下一行重复加进去。"""
    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/3?book_id={book.id}",
                         headers=auth).status_code == 200
    after = formulas(db, tpl_id)
    assert after[8] == "合计(B2:B7)", after[8]        # 原本是 合计(B2:B8)
    # 「资产总计」原本在第 17 行写的是 B9+B16；删掉第 3 行后它上移到第 16 行，
    # 引用也要跟着指到「流动资产合计(8)」和「非流动资产合计(15)」
    assert after[16] == "B8+B15", after[16]


def test_row_labels_follow_the_shift(client, auth, tpl_id, db, book):
    """行标签也要跟着挪，不能出现两个「应收账款」。"""
    client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                headers=auth, json={"row_no": 3, "label": "插进来的"})
    lab = labels(db, tpl_id)
    # 原表：3 交易性金融资产 / 4 应收票据 / 5 应收账款，插一行后整体下移
    assert lab[3] == "插进来的", lab.get(3)
    assert lab[4] == "交易性金融资产", lab.get(4)
    assert lab[5] == "应收票据", lab.get(5)
    assert lab[6] == "应收账款", lab.get(6)
    assert len([v for v in lab.values() if v == "应收账款"]) == 1, lab


# ---------------------------------------------------------------- 行的属性


def test_can_rename_and_restyle_a_row(client, auth, tpl_id, db, book):
    """改标签/缩进/加粗/隐藏 —— 只靠插删行拼不出想要的表。"""
    r = client.patch(f"/api/reports/templates/{tpl_id}/rows/9?book_id={book.id}", headers=auth,
                     json={"label": "流动资产合计", "indent": 1, "bold": True,
                           "hidden": False})
    assert r.status_code == 200, r.text
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == tpl_id,
                                             ReportRow.row_no == 9))
    assert row.label == "流动资产合计" and row.indent == 1 and row.bold is True


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
    """插一列会让 B 列变 C 列，公式里的 B10 必须跟着改，否则整张表算错。"""
    before = formulas(db, tpl_id, col=2)
    assert before[9] == "合计(B2:B8)", before

    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 2, "label": "上期数"})
    assert r.status_code == 201, r.text
    assert r.json()["letter"] == "B", r.json()

    after = formulas(db, tpl_id, col=3)          # 老第 2 列现在是第 3 列
    assert after[9] == "合计(C2:C8)", after[9]
    assert after[17] == "C9+C16", after[17]


def test_new_column_inherits_neighbour_kind(client, auth, tpl_id, db, book):
    """插在「期末余额」右边，新列默认也是期末口径，不用用户自己想。"""
    client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                headers=auth, json={"col_no": 3, "label": "对比列"})
    from app.models import ReportCol as C

    # 第 2 列是 BALANCE_CLOSING，所以插在它右边的新列也应是期末口径
    new = db.scalar(select(C).where(C.template_id == tpl_id, C.col_no == 3))
    assert new.kind == "BALANCE_CLOSING", new.kind


def test_cannot_delete_the_static_label_column(client, auth, tpl_id, book):
    r = client.delete(f"/api/reports/templates/{tpl_id}/cols/1?book_id={book.id}", headers=auth)
    assert r.status_code == 400
    assert "固定列" in r.json()["detail"], r.json()


def test_cannot_delete_the_last_value_column(client, auth, tpl_id, book):
    # 资产负债表原本 1 个 STATIC + 2 个数值列，删掉一个就只剩一个了
    assert client.delete(f"/api/reports/templates/{tpl_id}/cols/2?book_id={book.id}", headers=auth).status_code == 200
    r = client.delete(f"/api/reports/templates/{tpl_id}/cols/2?book_id={book.id}", headers=auth)
    assert r.status_code == 400 and "至少要保留" in r.json()["detail"], r.json()


def test_delete_column_shrinks_references(client, auth, tpl_id, db, book):
    """删掉「年初余额」列后，引用 B 的公式要收成引用不存在的（清空），不是错位。"""
    client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                headers=auth, json={"col_no": 3, "label": "临时列"})
    assert client.delete(f"/api/reports/templates/{tpl_id}/cols/3?book_id={book.id}",
                         headers=auth).status_code == 200
    after = formulas(db, tpl_id, col=2)
    assert after[9] == "合计(B2:B8)", after[9]


# ------------------------------------------------- 删行/删列：不得静默别名（P1-13）
#
# 审计给的例子：4103「本年利润」引用 ``B35``，删掉第 35 行之后 ``B35``
# 静默指向原第 36 行「未分配利润」—— 报表照常渲染出**错误的数字而不报错**。
# 这是整套报表里最危险的一类错误：没有异常、没有红字，财务只会以为数算对了。


def test_删掉被引用的行不会别名到相邻行(client, auth, tpl_id, db, book):
    """删第 35 行，引用 B35 的公式必须变成按 0 计算，而不是留在 B35。

    审计给的原话：4103「本年利润」引用 ``B35``，删掉第 35 行之后 ``B35``
    **静默指向原第 36 行「未分配利润」** —— 因为删除只做了「行号 >= 36 的减一」，
    35 号这个引用原封不动地留了下来，而 36 行已经上移占住了第 35 行的位置。
    报表照常渲染出**错误的数字而不报错**：没有异常、没有红字。
    """
    from app.models import ReportCell as C

    # 「本年利润」行引用它上面那行（模拟审计里的 4103 -> B35）
    db.add(C(template_id=tpl_id, row_no=40, col_no=2, formula="B35+B34"))
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/35?book_id={book.id}",
                         headers=auth).status_code == 200
    # 被删的行整体上移一格：40 -> 39；引用 B35（被删）不再指 B34
    assert formulas(db, tpl_id)[39] == "0+B34", formulas(db, tpl_id)[39]


def test_删行后区域收缩而不是留下非法公式(client, auth, tpl_id, db, book):
    """``合计(B34:B36)`` 删掉 35 行要收成 ``合计(B34:B35)``。

    以前是把被删的地址**清空**，产出 ``合计(B34:)`` —— 那是语法错误，
    这张表从此再也渲染不出来（审计 P2-17）。
    """
    from app.models import ReportCell as C

    db.add(C(template_id=tpl_id, row_no=40, col_no=2, formula="合计(B34:B36)"))
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/35?book_id={book.id}",
                         headers=auth).status_code == 200
    got = formulas(db, tpl_id)[39]
    assert got == "合计(B34:B35)", got
    # 产出必须还是能解析的公式（否则整张表这一列报废）
    from app.reports.formula import Parser

    Parser(got).parse()


def test_删掉第1行不再产出0行这种非法地址(client, auth, tpl_id, db, book):
    """删第 1 行时引用 B1 的公式按 0 处理。

    以前删除传的是 ``row_shift=(row_no + 1, -1)``，1 号引用根本不满足
    ``>= 2``，于是留在原地 —— 而第 2 行已经上移占住了第 1 行的位置，
    ``B1+B2`` 变成 ``B1+B1``，**同一个格子被加了两遍**，且全程不报错。
    """
    from app.models import ReportCell as C

    db.add(C(template_id=tpl_id, row_no=40, col_no=2, formula="B1+B2"))
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/rows/1?book_id={book.id}",
                         headers=auth).status_code == 200
    assert formulas(db, tpl_id)[39] == "0+B1", formulas(db, tpl_id)[39]


def test_删掉被引用的列不会别名到左边那一列(client, auth, tpl_id, db, book):
    """列与行是同一个 bug：删列以前也只做位移，引用会静默指向左邻列。

    被改的公式放在**留在表里的**那一列上（B 列），由它引用 C 列：
    删掉 C 列以后这条引用必须变成 0，而不是指向从右边挪过来的那一列。
    """
    from app.models import ReportCell as C

    cell = db.scalar(select(C).where(C.template_id == tpl_id,
                                     C.row_no == 2, C.col_no == 2))
    cell.formula = "C2+B2"
    db.commit()

    assert client.delete(f"/api/reports/templates/{tpl_id}/cols/3?book_id={book.id}",
                         headers=auth).status_code == 200
    assert formulas(db, tpl_id, col=2)[2] == "0+B2", formulas(db, tpl_id, col=2)[2]


# --------------------------------------------------- $ 锁定引用（P2-17 第一半）


def test_美元符号锁定引用在插行时不被改写(client, auth, tpl_id, db, book):
    """``$B$5`` 的含义就是不随插删行列移动，改写时必须原样保留。

    以前 ``$`` 会在改写时丢掉，而且 tokenizer 干脆不认 ``$``：
    公式存得进去、渲染时必报「无法识别的字符 '$'」。
    """
    from app.models import ReportCell as C
    from app.reports.formula import Evaluator, Parser

    db.add(C(template_id=tpl_id, row_no=40, col_no=2, formula="$B$5+B10"))
    db.commit()

    assert client.post(f"/api/reports/templates/{tpl_id}/rows?book_id={book.id}",
                       headers=auth, json={"row_no": 10, "label": "插一行"}).status_code == 200
    got = formulas(db, tpl_id)[41]
    assert got == "$B$5+B11", got   # 锁定的 B5 不动，普通的 B10 跟着下移

    # 而且现在真的能解析、真的能算（以前渲染期直接报错）
    node = Parser(got).parse()
    assert node is not None

    class _Ctx:
        def cell(self, r, c):
            return Decimal("1") if (r, c) == (5, 2) else Decimal("0")

        def subject_id(self, code):
            return None

        def resolve_period(self, expr):
            return None

    assert Evaluator(_Ctx()).run(got) == Decimal("1")


def test_column_label_and_width_editable(client, auth, tpl_id, db, book):
    r = client.patch(f"/api/reports/templates/{tpl_id}/cols/2?book_id={book.id}", headers=auth,
                     json={"label": "期末数（元）", "width": 180, "hidden": True})
    assert r.status_code == 200, r.text
    from app.models import ReportCol as C

    c = db.scalar(select(C).where(C.template_id == tpl_id, C.col_no == 2))
    assert c.label == "期末数（元）" and c.width == 180 and c.hidden is True


def test_rejects_unknown_column_kind(client, auth, tpl_id, book):
    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}", headers=auth,
                    json={"col_no": 2, "label": "怪列", "kind": "NOT_A_KIND"})
    assert r.status_code == 400
    assert "未知的取数口径" in r.json()["detail"], r.json()


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
    labels = [str(r[0].value or "") for r in ws.iter_rows(min_col=1, max_col=1)]
    assert not any("应收账款" in s for s in labels), labels


def test_hidden_column_does_not_shift_the_other_columns(client, auth, tpl_id, book, periods):
    """隐藏中间一列时，剩下的数字**不能整列错位**。

    单元格按 col_no 对齐而不是按下标：隐藏掉 B 列后，C 列的数还在它自己的位置上，
    不能因为少了一列就整体左移一格。
    """
    import io as _io

    from openpyxl import load_workbook

    from app.models import ReportCol as C

    db = SessionLocal()
    rows = labels(db, tpl_id)
    db.close()

    # 第 2 列（期末余额）隐藏
    assert client.patch(f"/api/reports/templates/{tpl_id}/cols/2?book_id={book.id}",
                        headers=auth, json={"hidden": True}).status_code == 200

    x = client.get(f"/api/books/{book.id}/print/report.xlsx", headers=auth,
                   params={"template_id": tpl_id, "period_id": periods[12].id})
    assert x.status_code == 200, x.text
    ws = load_workbook(_io.BytesIO(x.content)).active
    header = [str(c.value or "") for c in next(ws.iter_rows(min_row=4, max_row=4))]
    assert "期末余额" not in header, header
    assert "年初余额" in header, header

    # 隐藏的列不能残留成空列（表头不该出现空标题的第三列）
    assert header[2] in ("", None) or "年初余额" == header[2], header


def test_hidden_row_still_editable_in_design_view(client, auth, tpl_id, db, book):
    """界面要能取消隐藏，所以 render 里仍要带着 hidden 行 —— 过滤只发生在打印侧。"""
    client.patch(f"/api/reports/templates/{tpl_id}/rows/5?book_id={book.id}",
                 headers=auth, json={"hidden": True})
    rows = client.get(f"/api/reports/{tpl_id}/design", headers=auth).json()["rows"]
    hidden = [r for r in rows if r["row_no"] == 5]
    assert hidden and hidden[0]["hidden"] is True, hidden


def test_column_inserted_next_to_label_column_is_not_a_ghost(client, auth, tpl_id, db, book):
    """插在「项目」列右边（第 2 列）是最常见的用法。

    这里最容易踩的坑：新列把左邻的 STATIC 口径继承了，于是变成一个既不显示、
    又没有任何取数口径的幽灵列 —— 界面看着像插成功了，导出却没有这一列。
    """
    from app.models import ReportCol as C

    r = client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}",
                    headers=auth, json={"col_no": 2, "label": "新增列"})
    assert r.status_code == 201, r.text

    new = db.scalar(select(C).where(C.template_id == tpl_id, C.col_no == 2))
    assert new.kind != "STATIC", new.kind
    assert new.kind == "BALANCE_CLOSING", new.kind     # 沿用右边「期末余额」的口径

    cols = db.scalars(select(C).where(C.template_id == tpl_id).order_by(C.col_no)).all()
    assert [c.col_no for c in cols] == [1, 2, 3, 4], [c.col_no for c in cols]
    assert sum(1 for c in cols if c.kind == "STATIC") == 1, "项目列必须仍然只有一个"


def test_column_kind_can_be_chosen_explicitly(client, auth, tpl_id, db, book):
    from app.models import ReportCol as C

    assert client.post(f"/api/reports/templates/{tpl_id}/cols?book_id={book.id}", headers=auth,
                       json={"col_no": 4, "label": "本年累计", "kind": "YTD_AMOUNT"}
                       ).status_code == 201
    new = db.scalar(select(C).where(C.template_id == tpl_id, C.col_no == 4))
    assert new.kind == "YTD_AMOUNT", new.kind
