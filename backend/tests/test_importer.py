"""Excel 导入：暂存 → 预览 → 确认入库。

导入是最容易出「脏数据」的地方：Excel 里什么都能塞。
所以这里钉住三条底线：
    1. 解析阶段绝不写库（财务要能先看清楚再确认）
    2. 借贷不平衡 / 科目不存在的行必须被标出来，而不是硬写进去
    3. 确认入库时任一张失败就整体回滚
"""
import io
from datetime import date

import pytest
from sqlalchemy import select

from app.accounting.importer import commit, discard, map_columns, parse_workbook, stage
from app.models import ImportBatch, Voucher


def _xlsx(header: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- 表头


def test_表头别名容错():
    mapping, unknown = map_columns(["制单日期", "凭证字号", "摘要", "科目代码", "借方金额", "贷方金额"])
    assert mapping["voucher_date"] == 0
    assert mapping["voucher_no"] == 1
    assert mapping["subject"] == 3
    assert mapping["debit"] == 4
    assert unknown == []


def test_未识别表头会被报告():
    mapping, unknown = map_columns(["日期", "科目编码", "借方", "贷方", "随便一列"])
    assert unknown == ["随便一列"]


def test_缺必需表头报错(db, book):
    data = _xlsx(["日期", "摘要", "借方"], [["2026-01-05", "x", 10]])
    with pytest.raises(ValueError, match="缺少必需的表头列"):
        parse_workbook(data, book.id, db)


def test_缺金额列报错(db, book):
    data = _xlsx(["日期", "科目编码", "摘要"], [["2026-01-05", "100201", "x"]])
    with pytest.raises(ValueError, match="缺少金额列"):
        parse_workbook(data, book.id, db)


def test_空文件报错(db, book):
    with pytest.raises(ValueError, match="无法读取 Excel 文件"):
        parse_workbook(b"not-an-xlsx", book.id, db)


# --------------------------------------------------------------------- 解析


def _good_rows():
    return [
        ["2026-01-05", "记-1", "收租金", "100201", 1000, 0],
        [None, None, "收租金", "600101", 0, 1000],
        ["2026-01-06", "记-2", "付水电", "6602", 200, 0],
        [None, None, "付水电", "100201", 0, 200],
    ]


def test_日期或凭证号为空视为接上一张(db, book):
    """Excel 里一张凭证常连着几行分录，空单元格不该被当成新凭证。"""
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    r = parse_workbook(data, book.id, db)

    assert len(r["vouchers"]) == 2
    assert len(r["vouchers"][0]["entries"]) == 2
    assert r["vouchers"][1]["voucher_no"] == "记-2"


def test_借贷不平衡被标为不通过(db, book):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], [
        ["2026-01-05", "记-1", "x", "100201", 1000, 0],
        [None, None, "x", "600101", 0, 999],
    ])
    r = parse_workbook(data, book.id, db)
    v = r["vouchers"][0]
    assert v["ok"] is False
    assert any("借贷不平衡" in e for e in v["errors"])


def test_科目不存在被标为不通过(db, book):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], [
        ["2026-01-05", "记-1", "x", "999999", 1000, 0],
        [None, None, "x", "600101", 0, 1000],
    ])
    r = parse_workbook(data, book.id, db)
    assert r["vouchers"][0]["ok"] is False
    assert any("999999" in e for e in r["vouchers"][0]["errors"])


def test_解析阶段不写库(db, book, admin_user):
    """最关键的一条：预览阶段数据库必须纹丝不动。"""
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    r = parse_workbook(data, book.id, db)
    assert all(v["ok"] for v in r["vouchers"])
    assert db.scalar(select(Voucher).where(Voucher.book_id == book.id)) is None


def test_单列金额靠方向列判断借贷(db, book):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "金额", "方向"], [
        ["2026-01-05", "记-1", "x", "100201", 500, "借"],
        [None, None, "x", "600101", 500, "贷"],
    ])
    r = parse_workbook(data, book.id, db)
    assert r["vouchers"][0]["ok"] is True
    assert r["vouchers"][0]["entries"][0]["direction"] == "DEBIT"
    assert r["vouchers"][0]["entries"][1]["direction"] == "CREDIT"


def test_单列金额若全落贷方会被判成不平衡(db, book):
    """这条钉的是上面那个 bug：方向列没注册时，借方金额会悄悄变成贷方。"""
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "金额"], [
        ["2026-01-05", "记-1", "x", "100201", 500],
        [None, None, "x", "600101", 500],
    ])
    r = parse_workbook(data, book.id, db)
    assert r["vouchers"][0]["ok"] is False
    assert any("借贷不平衡" in e for e in r["vouchers"][0]["errors"])


# --------------------------------------------------------------------- 暂存 / 提交


def test_提交后写入凭证(db, book, admin_user):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    db.refresh(batch)
    assert batch.status == "PENDING"

    r = commit(db, batch, admin_user)
    assert r["ok"] is True and r["created"] == 2

    rows = db.scalars(select(Voucher).where(Voucher.book_id == book.id)).all()
    assert len(rows) == 2
    assert all(v.source == "IMPORT" for v in rows)
    assert all(v.status == "UNPOSTED" for v in rows)


def test_重复提交被拒(db, book, admin_user):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    commit(db, batch, admin_user)
    db.refresh(batch)

    with pytest.raises(ValueError, match="不能重复提交"):
        commit(db, batch, admin_user)
    db.rollback()


def test_作废后不能再提交(db, book, admin_user):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    discard(db, batch)
    db.refresh(batch)
    assert batch.status == "DISCARDED"

    with pytest.raises(ValueError, match="不能重复提交"):
        commit(db, batch, admin_user)
    db.rollback()


def test_作废不写库(db, book, admin_user):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    discard(db, batch)
    assert db.scalar(select(Voucher).where(Voucher.book_id == book.id)) is None


def test_有问题的凭证不会入库(db, book, admin_user):
    """通过的进、不通过的不进 —— 而不是整批卡死。"""
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], [
        ["2026-01-05", "记-1", "好的", "100201", 1000, 0],
        [None, None, "好的", "600101", 0, 1000],
        ["2026-01-06", "记-2", "坏的", "100201", 1000, 0],
        [None, None, "坏的", "600101", 0, 999],
    ])
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    r = commit(db, batch, admin_user)

    assert r["created"] == 1
    assert len(db.scalars(select(Voucher).where(Voucher.book_id == book.id)).all()) == 1


def test_提交时的意外失败整体回滚(db, book, admin_user, subjects):
    """入库阶段的失败（已结账期间等）必须整体回滚，一张都不留。"""
    from app.accounting.periods import carry_forward_profit, close_period
    from app.accounting.vouchers import create_voucher, post_vouchers
    from app.models import Period

    # 1 月造一笔已记账的业务并结转结账，让 1 月进入已结账状态
    v = create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 10),
                       entries=[{"subject_id": subjects["100201"].id, "direction": "DEBIT",
                                 "amount": "10", "summary": "x"},
                                {"subject_id": subjects["600101"].id, "direction": "CREDIT",
                                 "amount": "10", "summary": "x"}])
    post_vouchers(db, [v.id], admin_user)
    jan = db.scalar(select(Period).where(Period.book_id == book.id, Period.month == 1))
    carry_forward_profit(db, book.id, jan, admin_user)
    close_period(db, jan, admin_user)

    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], [
        ["2026-03-05", "记-1", "能进", "100201", 100, 0],
        [None, None, "能进", "600101", 0, 100],
        ["2026-01-15", "记-2", "进不去", "100201", 50, 0],
        [None, None, "进不去", "600101", 0, 50],
    ])
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    r = commit(db, batch, admin_user)

    assert r["ok"] is False
    assert r["created"] == 0
    assert r["failed"] and "记-2" in str(r["failed"])
    assert db.scalar(select(Voucher).where(Voucher.book_id == book.id,
                                           Voucher.source == "IMPORT")) is None


def test_导入批次可查(db, book, admin_user):
    data = _xlsx(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"], _good_rows())
    batch = stage(db, book.id, admin_user, "导入.xlsx", data)
    row = db.scalar(select(ImportBatch).where(ImportBatch.book_id == book.id))
    assert row.id == batch.id
    assert row.filename == "导入.xlsx"
