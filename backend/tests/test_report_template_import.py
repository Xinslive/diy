"""外部报表模板迁移的无数据库校验。"""
from app.reports.importer import _translate, parse_ufo_report


def _cell(text="", formula="", missing=False):
    return {"text": text, "formula": formula, "missing": missing}


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
