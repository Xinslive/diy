"""外部科目表导入：解析、完整覆盖与业务数据保护。"""
from __future__ import annotations

import io

from openpyxl import Workbook
from sqlalchemy import select

from app.models import AuditLog, Subject


def _xlsx(rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _subjects_file() -> bytes:
    return _xlsx([
        ["类型", "级次", "科目编码", "科目名称", "助记码", "外币币种", "计量单位", "辅助账类型", "账页格式", "余额方向"],
        ["资产", 1, "1001", "现金", "XJ", "", "", "", "金额式", "借"],
        ["资产", 2, "100101", "备用金", "BWJ", "", "", "", "金额式", "借"],
        ["负债", 1, "2001", "短期借款", "DQJK", "", "", "", "金额式", "贷"],
    ])


def test_科目表预览识别用友列并重建父子关系(db):
    from app.accounting.subject_importer import parse_subject_workbook

    result = parse_subject_workbook(_subjects_file(), "会计科目.xlsx")

    assert result["ok"] is True
    assert result["total"] == 3
    assert result["code_rule"] == "4-2"
    assert result["subjects"][1]["parent_code"] == "1001"
    assert result["subjects"][0]["is_leaf"] is False
    assert result["subjects"][2]["category"] == "LIABILITY"


def test_确认导入会完整覆盖自带科目并更新编码规则(client, auth, book, db):
    body = _subjects_file()
    upload = {"file": ("会计科目.xlsx", body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

    preview = client.post(f"/api/books/{book.id}/subjects/import/preview", files=upload, headers=auth)
    assert preview.status_code == 200, preview.text
    assert preview.json()["current"]["subject_count"] > 3
    assert preview.json()["current"]["can_overwrite"] is True

    upload = {"file": ("会计科目.xlsx", body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    committed = client.post(f"/api/books/{book.id}/subjects/import", files=upload, headers=auth)
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 3
    assert committed.json()["code_rule"] == "4-2"

    rows = db.scalars(select(Subject).where(Subject.book_id == book.id).order_by(Subject.code)).all()
    assert [(row.code, row.name, row.level, row.is_leaf) for row in rows] == [
        ("1001", "现金", 1, False),
        ("100101", "备用金", 2, True),
        ("2001", "短期借款", 1, True),
    ]
    assert db.scalar(select(Subject).where(Subject.book_id == book.id, Subject.code == "600101")) is None
    audit = db.scalar(select(AuditLog).where(
        AuditLog.book_id == book.id, AuditLog.action == "IMPORT_SUBJECTS"
    ).order_by(AuditLog.id.desc()))
    assert audit is not None


def test_已有凭证的账套拒绝覆盖且不改动科目(client, auth, book, make_voucher, db):
    make_voucher("2026-01-05", dr=[("1001", 10, "迁移保护")], cr=[("600101", 10, "迁移保护")])
    body = _subjects_file()
    upload = {"file": ("会计科目.xlsx", body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

    preview = client.post(f"/api/books/{book.id}/subjects/import/preview", files=upload, headers=auth)
    assert preview.status_code == 200, preview.text
    assert preview.json()["current"]["can_overwrite"] is False

    upload = {"file": ("会计科目.xlsx", body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    response = client.post(f"/api/books/{book.id}/subjects/import", files=upload, headers=auth)
    assert response.status_code == 409, response.text
    assert db.scalar(select(Subject).where(Subject.book_id == book.id, Subject.code == "1001")) is not None
    assert db.scalar(select(Subject).where(Subject.book_id == book.id, Subject.code == "100101")) is None
