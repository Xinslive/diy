"""外部报表模板迁移的无数据库校验。"""
import struct

import pytest

from app.reports.importer import TemplateImportError, _translate, parse_ufo_report


def _cell(text="", formula="", missing=False):
    return {"text": text, "formula": formula, "missing": missing}


# ------------------------------------------------------------------ UFO 二进制
#
# ``_grid`` 是从二进制里按固定偏移读结构的（见 importer.py 的注释），
# 所以要测**它自己**的判定就不能 mock 掉它 —— mock 掉之后测的是
# ``parse_ufo_report`` 的下游逻辑，正是被改坏的那一层之外的东西。
# 下面按同一套布局拼一个最小 UFO~REP 流出来，字节级对齐：
#
#   0      : 1816 字节头（版本号在 [1:8]，行/列数在 452，字体数在 44）
#   1816+  : 行块 row_count*16 + 列块 col_count*12（维度记录，importer 按这两个
#            偏移定位网格起点，**漏掉任何一个都会整体错位**）
#   ...    : 网格 row_count*col_count*12（每格 12 字节）
#   ...    : 字体块 font_count*360
#   ...    : 文本区，每条 76 字节（4 字节 id + 72 字节 GBK 文本）
#   ...    : 公式区，每条 22 字节头 + GBK 正文（正文的头 4 字节恒为 1）

_HEADER_SIZE = 1816
_TEXT_RECORD = 76


def _ufo_stream(cells: dict, rows: int, cols: int, version: bytes = b"UFO8.00") -> bytes:
    """``cells`` 是 ``(row, col) -> (文本, 公式)``，下标从 1 起。"""
    grid = bytearray()
    texts: list[tuple[int, bytes]] = []
    formulas: list[bytes] = []
    ident = 0
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            text, formula = cells.get((row, col), ("", ""))
            if not text and not formula:
                grid += struct.pack("<III", 0, 0, 0)
                continue
            ident += 1
            if formula:
                grid += struct.pack("<III", ident, 0x4000, 0)
                body = formula.encode("gbk")
                formulas.append(struct.pack("<IHHHHHHIH", ident, 0, row, col, 0, row, col, 1, len(body)) + body)
            else:
                grid += struct.pack("<III", ident, 0, 0)
                texts.append((ident, text.encode("gbk")))

    header = bytearray(_HEADER_SIZE)
    header[0:9] = b"\x00" + version + b"\x00"
    header[44:48] = struct.pack("<I", 1)          # 字体数取 1
    header[452:456] = struct.pack("<HH", rows, cols)
    text_area = b"".join(
        struct.pack("<I", key) + raw.ljust(_TEXT_RECORD - 4, b"\x00") for key, raw in texts)
    # 维度记录：行块 + 列块，字节数必须与 importer 的 grid_start 偏移一致
    dimensions = b"\x00" * (rows * 16 + cols * 12)
    return bytes(header) + dimensions + bytes(grid) + b"\x00" * 360 + text_area + b"".join(formulas)


def _wide_income_cells() -> dict:
    """一张 7 列的利润表：前 4 列有内容，第 5、6、7 列整列留白。

    真实来源是「从更宽的模板另存出来的利润表」，UFO 把网格宽度也一并存了。
    """
    return {
        (1, 1): ("项     目", ""), (1, 2): ("行次", ""),
        (1, 3): ("本月金额", ""), (1, 4): ("本年累计金额", ""),
        (2, 1): ("主营业务收入", ""), (2, 2): ("一", ""),
        (2, 3): ("", 'fs(5001,月,"贷",,年)'), (2, 4): ("", 'lfs(5001,月,"贷",,年)'),
        (3, 1): ("  房租收入", ""), (3, 2): ("1", ""),
        (3, 3): ("", 'fs(500101,月,"贷",,年)'), (3, 4): ("", 'lfs(500101,月,"贷",,年)'),
    }


def _income_grid():
    return [
        [_cell("项     目"), _cell("行次"), _cell("本期数"), _cell("本年累计数")],
        [_cell("收入"), _cell("1"), _cell(formula='fs(5001,月,"贷",,年)'), _cell(formula='lfs(5001,月,"贷",,年)')],
        [_cell("小计"), _cell("2"), _cell(formula="C2+D2"), _cell(formula="C2")],
    ]


def _balance_grid():
    return [
        [_cell("资  产"), _cell("行次"), _cell("期末余额"), _cell("年初余额"), _cell("负债和所有者权益"), _cell("行次"), _cell("期末余额"), _cell("年初余额")],
        [_cell("货币资金"), _cell("1"), _cell(formula='QM("1001",月,,,年,,)'), _cell(formula='QC("1001",全年,,,年,,)'), _cell("短期借款"), _cell("31"), _cell(formula='QM("2001",月,,,年,,)'), _cell(formula='QC("2001",全年,,,年,,)')],
    ]


def test_利润表跳过行次列并保留三列(monkeypatch):
    import app.reports.importer as importer

    monkeypatch.setattr(importer, "_stream", lambda raw: b"fixture")
    monkeypatch.setattr(importer, "_grid", lambda data: (_income_grid(), "UFO5.10"))

    parsed = parse_ufo_report(b"fixture", "迁移利润表.rep")

    assert [(col["col_no"], col["kind"]) for col in parsed["cols"]] == [
        (1, "STATIC"), (2, "FORMULA"), (3, "FORMULA")
    ]
    assert parsed["stats"]["formulas"] == 4
    assert parsed["cells"][:2] == [
        {"row_no": 1, "col_no": 2, "formula": '科目("5001","本期贷方",{{本期}})'},
        {"row_no": 1, "col_no": 3, "formula": '科目("5001","贷方累计",{{本期}})'},
    ]
    assert parsed["cells"][2]["formula"] == "B1+C1"
    assert parsed["cells"][3]["formula"] == "B1"


def test_资产负债表保留双栏及余额公式(monkeypatch):
    import app.reports.importer as importer

    monkeypatch.setattr(importer, "_stream", lambda raw: b"fixture")
    monkeypatch.setattr(importer, "_grid", lambda data: (_balance_grid(), "UFO8.00"))

    parsed = parse_ufo_report(b"fixture", "迁移资产负债表.rbk")

    assert len(parsed["cols"]) == 8
    assert parsed["cols"][2]["kind"] == "BALANCE_CLOSING"
    assert parsed["cols"][3]["kind"] == "BALANCE_OPENING"
    assert parsed["cols"][6]["kind"] == "BALANCE_CLOSING"
    assert parsed["cells"][0]["formula"] == '科目("1001","期末余额",{{本期}})'
    assert parsed["cells"][1]["formula"] == '科目("1001","期初余额",{{年初}})'
    assert parsed["cells"][2]["col_no"] == 7
    assert parsed["cells"][3]["col_no"] == 8


def test_外部单元格引用会随利润表列布局迁移():
    assert _translate("$C$16+$D17", 5, 20, 4, {1: 1, 3: 2, 4: 3}) == "$B$12+$C13"


def test_确认导入会写入模板骨架和公式(client, auth, book, db):
    from sqlalchemy import select
    from app.models import ReportCell, ReportCol, ReportRow, ReportTemplate

    payload = {
        "templates": [{
            "name": "迁移资产负债表",
            "type": "BALANCE_SHEET",
            "source": {"filename": "资产负债表.rep", "format": "用友 UFO8.00", "fingerprint": "0123456789abcdef01234567"},
            "rows": [{"row_no": 1, "label": "货币资金", "style": {"line_left": "1", "kind": "ITEM"}}],
            "cols": [
                {"col_no": 1, "label": "资产", "kind": "STATIC", "width": 250},
                {"col_no": 2, "label": "期末余额", "kind": "FORMULA", "width": 140},
            ],
            "cells": [{"row_no": 1, "col_no": 2, "formula": '科目("1001","期末余额",{{本期}})'}],
            "issues": [],
            "subject_mapping": {},
        }],
        "accept_warnings": False,
    }

    response = client.post(f"/api/reports/templates/import?book_id={book.id}", headers=auth, json=payload)
    assert response.status_code == 201, response.text
    created = response.json()["created"][0]
    template = db.get(ReportTemplate, created["id"])
    assert template is not None
    assert db.scalar(select(ReportRow).where(ReportRow.template_id == template.id)).label == "货币资金"
    assert db.scalar(select(ReportCol).where(ReportCol.template_id == template.id, ReportCol.col_no == 2)).kind == "FORMULA"
    assert db.scalar(select(ReportCell).where(ReportCell.template_id == template.id)).formula.startswith("科目(\"1001\"")


# ------------------------------------------------------- 尾部空列（网格预留宽度）


def test_尾部三列留白的利润表按四列导入(monkeypatch):
    """UFO 存的是**网格宽度**，不是「用了几列」。

    7 列、第 5 列往后一个格子都没有内容的利润表，与 4 列那张在结构上完全一样；
    按声明列数把它拒掉，理由（不支持）站不住。真实来源：用户的开元寺店模板。
    """
    import app.reports.importer as importer

    stream = _ufo_stream(_wide_income_cells(), rows=3, cols=7)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    parsed = parse_ufo_report(b"fixture", "开元寺店利润表.rep")

    assert parsed["type"] == "INCOME_SHEET"
    # 项目名里的空格是 UFO 自己排版用的，导入器原样保留（表头识别时才折叠空白）
    assert [(c["col_no"], c["label"], c["kind"]) for c in parsed["cols"]] == [
        (1, "项     目", "STATIC"), (2, "本月金额", "FORMULA"), (3, "本年累计金额", "FORMULA"),
    ]
    assert parsed["cells"][0]["formula"] == '科目("5001","本期贷方",{{本期}})'
    assert parsed["cells"][1]["formula"] == '科目("5001","贷方累计",{{本期}})'
    assert parsed["issues"] == []


def test_最后有空列的资产负债表仍按八列导入(monkeypatch):
    """会小企01表的双栏版式是 8 列。同样允许尾部再多预留几列留白。"""
    import app.reports.importer as importer

    cells = {
        (1, 1): ("资  产", ""), (1, 2): ("行次", ""),
        (1, 3): ("期末余额", ""), (1, 4): ("年初余额", ""),
        (1, 5): ("负债和所有者权益", ""), (1, 6): ("行次", ""),
        (1, 7): ("期末余额", ""), (1, 8): ("年初余额", ""),
        (2, 1): ("库存现金", ""), (2, 2): ("1", ""),
        (2, 3): ("", 'QM("1001",月,,,年,,)'), (2, 4): ("", 'QC("1001",全年,,,年,,)'),
        (2, 5): ("应付账款", ""), (2, 6): ("32", ""),
        (2, 7): ("", 'QM("2001",月,,,年,,)'), (2, 8): ("", 'QC("2001",全年,,,年,,)'),
    }

    stream = _ufo_stream(cells, rows=2, cols=9)   # 第 9 列整列留白
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    parsed = parse_ufo_report(b"fixture", "宽版资产负债表.rep")

    assert parsed["type"] == "BALANCE_SHEET"
    assert len(parsed["cols"]) == 8
    assert parsed["cells"][0]["col_no"] == 3


def test_有内容的第5列仍然拒收不静默丢列(monkeypatch):
    """裁的只是**尾部留白**。第 5 列有内容说明那是另一种版式（比如带比较数），
    悄悄丢掉一列金额比拒绝危险得多 —— 人会拿着一张对不上的利润表去做账。
    """
    import app.reports.importer as importer

    cells = _wide_income_cells()
    cells[(2, 5)] = ("上年同期", "")
    cells[(2, 6)] = ("", 'fs(5001,月,"贷",,年)')

    stream = _ufo_stream(cells, rows=3, cols=7)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    with pytest.raises(TemplateImportError, match="4 列利润表和 8 列资产负债表"):
        parse_ufo_report(b"fixture", "带比较列的利润表.rep")


# ------------------------------------------- 末行大类行：行次列走的是中文数字


def _chinese_line_income_cells() -> dict:
    """一张行次列**大类走中文数字**的利润表 —— 标准《会小企02表》的真实版式。

    大类行：一、主营业务收入 / 减：1、管理费用 / 减：2、财务费用 / 四、利润总额
    明细行：1、2、3 ……
    """
    return {
        (1, 1): ("项     目", ""), (1, 2): ("行次", ""),
        (1, 3): ("本月金额", ""), (1, 4): ("本年累计金额", ""),
        (2, 1): ("一、主营业务收入", ""), (2, 2): ("一", ""),
        (2, 3): ("", 'fs(5001,月,"贷",,年)'), (2, 4): ("", 'lfs(5001,月,"贷",,年)'),
        (3, 1): ("       携程收入", ""), (3, 2): ("1", ""),
        (3, 3): ("", 'fs(500101,月,"贷",,年)'), (3, 4): ("", 'lfs(500101,月,"贷",,年)'),
        (4, 1): ("  减：1、管理费用", ""), (4, 2): ("二", ""),
        (4, 3): ("", 'fs(5602,月,"借",,年)'), (4, 4): ("", 'lfs(5602,月,"借",,年)'),
        (5, 1): ("  减：2、财务费用", ""), (5, 2): ("三", ""),
        (5, 3): ("", 'fs(5603,月,"借",,年)'), (5, 4): ("", 'lfs(5603,月,"借",,年)'),
        (6, 1): ("四、利润总额（亏损总额以“-”号填列）", ""), (6, 2): ("四", ""),
        (6, 3): ("", 'fs(5001,月,"贷",,年)-fs(5602,月,"借",,年)'),
        (6, 4): ("", 'lfs(5001,月,"贷",,年)-lfs(5602,月,"借",,年)'),
    }


def test_末行大类行走中文行次时不被切掉(monkeypatch):
    """UFO 行次列里**大类行走中文数字**（「一、二、三、四」），明细行才走阿拉伯数字。

    真实来源：用户的《2025开元寺店（优境）+利润表格式.rep》。那张表的大类行全是
    「一、二、三、四」，只认 `\\d+` 的话，末行大类落在表体范围之外会被整行切掉 ——
    「减：2、财务费用」和「四、利润总额」两行凭空消失，导进来只剩 23 行的半张表，
    而且**不报任何错**。人只会以为「模板就这么多行」。
    """
    import app.reports.importer as importer

    stream = _ufo_stream(_chinese_line_income_cells(), rows=6, cols=4)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    parsed = parse_ufo_report(b"fixture", "开元寺店利润表.rep")

    assert parsed["stats"]["rows"] == 5
    assert [row["label"] for row in parsed["rows"]] == [
        "一、主营业务收入", "携程收入", "减：1、管理费用", "减：2、财务费用",
        "四、利润总额（亏损总额以“-”号填列）",
    ]
    # 末行的行次也要落进 style（界面上的行次列）
    assert parsed["rows"][-1]["style"]["line_left"] == "四"
    assert parsed["rows"][-1]["bold"] is True
    # 末行两个金额格都得有公式，别只导进来一个空壳
    assert [cell["formula"] for cell in parsed["cells"] if cell["row_no"] == 5] == [
        '科目("5001","本期贷方",{{本期}})-科目("5602","本期借方",{{本期}})',
        '科目("5001","贷方累计",{{本期}})-科目("5602","借方累计",{{本期}})',
    ]
    assert parsed["issues"] == []


def test_行次列整列为空时按金额公式定位表体(monkeypatch):
    """行次列有表头、正文却一个行号都没填的模板（自己做的表常见）。

    表体的真实边界是「最后一个带金额公式的行」，不是行次。
    """
    import app.reports.importer as importer

    cells = {
        (1, 1): ("项     目", ""), (1, 2): ("行次", ""),
        (1, 3): ("本月金额", ""), (1, 4): ("本年累计金额", ""),
        (2, 1): ("房租收入", ""), (2, 2): ("", ""),
        (2, 3): ("", 'fs(500101,月,"贷",,年)'), (2, 4): ("", 'lfs(500101,月,"贷",,年)'),
        (3, 1): ("合计", ""), (3, 2): ("", ""),
        (3, 3): ("", 'fs(5001,月,"贷",,年)'), (3, 4): ("", 'lfs(5001,月,"贷",,年)'),
    }

    stream = _ufo_stream(cells, rows=3, cols=4)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    parsed = parse_ufo_report(b"fixture", "无行次利润表.rep")

    assert parsed["stats"]["rows"] == 2
    assert [row["label"] for row in parsed["rows"]] == ["房租收入", "合计"]


def test_表尾说明不会被当成表体(monkeypatch):
    """表尾的「单位负责人 / 会计主管」是文本行，没有金额公式 —— 不进表体。

    它也不带行次标记，所以行次与公式两条判据都不会把它捞进来。
    """
    import app.reports.importer as importer

    cells = _chinese_line_income_cells()
    cells[(7, 1)] = ("单位负责人：  会计主管：", "")

    stream = _ufo_stream(cells, rows=7, cols=4)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    parsed = parse_ufo_report(b"fixture", "带表尾的利润表.rep")

    assert parsed["stats"]["rows"] == 5
    assert all("单位负责人" not in row["label"] for row in parsed["rows"])


def test_右栏表头写法不同也保得住右边的行次(monkeypatch):
    """资产负债表右栏的行次列表头未必写「行次」。

    定位行次列不能只认表头文字 —— 认不到就按 UFO 版式的固定位置回落
    （左栏第 2 列、右栏第 6 列），否则右边那半张表的行次会整列丢掉。
    """
    import app.reports.importer as importer

    cells = {
        (1, 1): ("资  产", ""), (1, 2): ("行次", ""),
        (1, 3): ("期末余额", ""), (1, 4): ("年初余额", ""),
        (1, 5): ("负债和所有者权益", ""), (1, 6): ("行号", ""),
        (1, 7): ("期末余额", ""), (1, 8): ("年初余额", ""),
        (2, 1): ("库存现金", ""), (2, 2): ("1", ""),
        (2, 3): ("", 'QM("1001",月,,,年,,)'), (2, 4): ("", 'QC("1001",全年,,,年,,)'),
        (2, 5): ("应付账款", ""), (2, 6): ("32", ""),
        (2, 7): ("", 'QM("2001",月,,,年,,)'), (2, 8): ("", 'QC("2001",全年,,,年,,)'),
    }

    stream = _ufo_stream(cells, rows=2, cols=8)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    parsed = parse_ufo_report(b"fixture", "右栏表头异体.rep")

    assert parsed["type"] == "BALANCE_SHEET"
    assert parsed["stats"]["rows"] == 1
    style = parsed["rows"][0]["style"]
    assert style["line_left"] == "1"
    assert style["line_right"] == "32"
