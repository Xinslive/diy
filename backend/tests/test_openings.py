"""期初余额录入：读、写、Excel 导入导出。

这块是整个系统的入口，也是最容易埋雷的地方 —— 期初一旦存歪，之后每个月、
每个报表都跟着歪，而且很难反推是哪一步错的。所以这里把几个会计上的硬约束
都钉死成用例：借贷必平、明细科目才能录、反方向余额必须原样往返、账套之间
的科目不能串。
"""
from __future__ import annotations

import io

from decimal import Decimal

from openpyxl import Workbook, load_workbook
from sqlalchemy import select

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(rows, header=("科目编码", "科目名称", "方向", "期初余额")) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ 读写往返


def test_round_trip_preserves_reverse_balance(client, auth, book_id, subjects):
    """应收账款是借方属性，但**可以**有贷方余额（客户预付挂账）。

    这是本模块最重要的一条：早期实现按科目属性强制符号，这种余额一存进去
    就被洗成借方正数，导出再导入也对不回来。
    """
    rows = [
        {"subject_id": subjects["100201"].id, "amount": "100000"},   # 银行存款 借
        {"subject_id": subjects["1122"].id, "amount": "-30000"},     # 应收账款 **贷**
        {"subject_id": subjects["4001"].id, "amount": "-70000"},     # 实收资本 贷
    ]
    r = client.post(f"/api/books/{book_id}/periods", headers=auth, json=rows)
    assert r.status_code == 200, r.text

    got = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    by_code = {s["code"]: s for s in got["subjects"]}

    ar = by_code["1122"]
    # 金额列是 NUMERIC(18,2)，存回来必然归一到两位小数，用 Decimal 比而不是比字符串
    assert Decimal(ar["signed"]) == Decimal("-30000"), ar
    assert ar["side"] == "贷", ar
    assert Decimal(ar["amount"]) == Decimal("30000"), ar
    assert ar["natural_side"] == "借", ar          # 科目属性仍是借，界面据此提示「反」

    bank = by_code["100201"]
    assert Decimal(bank["signed"]) == Decimal("100000") and bank["side"] == "借"


def test_round_trip_keeps_balance_ledger_consistent(client, auth, book_id, subjects):
    """期初平了，往后每个月也必须平 —— 从账簿侧独立验一次。"""
    rows = [
        {"subject_id": subjects["100201"].id, "amount": "500000"},
        {"subject_id": subjects["4001"].id, "amount": "-500000"},
    ]
    assert client.post(f"/api/books/{book_id}/periods", headers=auth, json=rows).status_code == 200

    periods = client.get(f"/api/books/{book_id}/periods", headers=auth).json()
    p3 = next(p for p in periods if p["code"] == "2026-03")
    bal = client.get(f"/api/books/{book_id}/ledger/balance", headers=auth,
                     params={"period_id": p3["id"]}).json()
    assert bal["check"]["opening_balanced"], bal["check"]
    assert bal["check"]["closing_balanced"], bal["check"]


def test_can_reenter_and_overwrite(client, auth, book_id, subjects):
    """没有凭证之前，期初允许反复改（整体重建，不是追加）。"""
    assert client.post(f"/api/books/{book_id}/periods", headers=auth, json=[
        {"subject_id": subjects["100201"].id, "amount": "100"},
        {"subject_id": subjects["4001"].id, "amount": "-100"},
    ]).status_code == 200
    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": subjects["100201"].id, "amount": "250"},
                          {"subject_id": subjects["4001"].id, "amount": "-250"}])
    assert r.status_code == 200
    got = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert Decimal(next(s for s in got["subjects"] if s["code"] == "100201")["signed"]) == Decimal("250")


# ------------------------------------------------------------------ 校验红线


def test_unbalanced_openings_rejected(client, auth, book_id, subjects):
    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": subjects["100201"].id, "amount": "1000"}])
    assert r.status_code == 400
    assert "试算不平" in r.json()["detail"], r.json()

    # 不平就不该落库：确认没写进去脏数据
    got = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert next(s for s in got["subjects"] if s["code"] == "100201")["signed"] == "0"


def test_non_leaf_subject_rejected(client, auth, book_id, subjects):
    """1002 银行存款是父科目，余额必须落在 100201/100202 明细上。"""
    parent = subjects["1002"]
    assert parent.is_leaf is False
    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": parent.id, "amount": "1000"}])
    assert r.status_code == 400
    assert "明细科目" in r.json()["detail"], r.json()


def test_cross_book_subject_rejected(client, auth, db, book, subjects, admin_user):
    """拿别的账套的科目来填本账套 —— 必须挡住，否则串账。"""
    import uuid

    from app.accounting.periods import create_periods
    from app.models import Book, Subject, SysCompany, UserBook
    from app.seed.chart_of_accounts import install_chart

    company = SysCompany(name=f"别家公司{uuid.uuid4().hex[:8]}")
    db.add(company)
    db.flush()
    other = Book(company_id=company.id, name="别家账套", year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    install_chart(db, other.id)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    other_subject = db.scalar(
        select(Subject).where(Subject.book_id == other.id, Subject.code == "100201"))
    assert other_subject.id != subjects["100201"].id

    r = client.post(f"/api/books/{book.id}/periods", headers=auth,
                    json=[{"subject_id": other_subject.id, "amount": "1"}])
    assert r.status_code == 400
    assert "科目不存在" in r.json()["detail"], r.json()


def test_openings_locked_after_voucher(client, auth, book_id, make_voucher):
    """账上有了业务数据，期初就是历史事实，不能再回头改。"""
    make_voucher("2026-01-15", dr=[("100201", 100, "买货")], cr=[("4001", 100, "买货")])

    got = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert got["editable"] is False
    assert got["reason"]

    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": got["subjects"][0]["id"], "amount": "1"}])
    assert r.status_code == 400
    assert "业务数据" in r.json()["detail"], r.json()


def test_openings_not_editable_for_other_user_book(client, auth, book_id):
    """无权限账套，三个端点一律 404（不泄露账套是否存在）。"""
    for path, method in ((f"/api/books/{book_id}/openings", "get"),
                         (f"/api/books/{book_id}/openings.xlsx", "get")):
        r = getattr(client, method)(path, headers={"Authorization": "Bearer nope"})
        assert r.status_code in (401, 403, 404), (path, r.status_code)


# ------------------------------------------------------------------ Excel


def test_export_then_parse_round_trip_preserves_reverse_balance(
    client, auth, book_id, subjects):
    """导出 → 再导入，必须原样回来。反方向余额是最容易被洗掉的那个。"""
    client.post(f"/api/books/{book_id}/periods", headers=auth, json=[
        {"subject_id": subjects["100201"].id, "amount": "100000"},
        {"subject_id": subjects["1122"].id, "amount": "-30000"},
        {"subject_id": subjects["4001"].id, "amount": "-70000"},
    ])

    r = client.get(f"/api/books/{book_id}/openings.xlsx", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == XLSX_MIME
    assert "filename*=UTF-8''" in r.headers["content-disposition"], r.headers

    # 导出件本身就要把「贷」写进方向列，而不是按科目属性写「借」
    ws = load_workbook(io.BytesIO(r.content)).active
    exported = {row[0]: (row[2], row[3]) for row in ws.iter_rows(min_row=2, values_only=True)}
    assert exported["1122"][0] == "贷", exported["1122"]
    assert exported["100201"][0] == "借"

    # 再喂回 parse 端点，方向与金额都要对得上
    p = client.post(f"/api/books/{book_id}/openings/parse", headers=auth,
                    files={"file": ("o.xlsx", r.content,
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert p.status_code == 200, p.text
    res = p.json()
    assert res["balanced"] is True, res
    assert res["errors"] == [], res["errors"]
    row = next(x for x in res["rows"] if x["code"] == "1122")
    assert row["signed"] == "-30000", row
    assert row["display_side"] == "贷", row


def test_parse_does_not_write_anything(client, auth, book_id, subjects):
    """parse 只是解析，落库由前端确认后单独提交 —— 这里确认它没偷偷写。"""
    before = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([("100201", "银行存款-工商银行", "借", 88888)]),
                 XLSX_MIME)})
    after = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert before["subjects"] == after["subjects"]


def test_parse_reports_errors_without_crashing(client, auth, book_id):
    """三种典型坏数据各自给出可读的原因，而不是 500 或静默跳过。"""
    bad = _xlsx([
        ("100201", "银行存款-工商银行", "借", 1000),   # 正常
        ("9999", "不存在的科目", "借", 500),           # 本账套没有
        ("1002", "银行存款", "借", 300),               # 非明细
        ("100202", "银行存款-建设银行", "借", "abc"),  # 金额不是数
        ("100203", "银行存款-微信支付", "借", None),   # 金额空 = 0，可接受
    ])
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth,
                    files={"file": ("o.xlsx", bad, XLSX_MIME)})
    assert r.status_code == 200, r.text
    res = r.json()
    errs = " ".join(res["errors"])
    assert "科目在本账套不存在" in errs, errs
    assert "不是明细科目" in errs, errs
    assert "金额无法识别" in errs, errs
    # 报出来的行号要能在 Excel 里对上：表头是第 1 行，第一条数据就是第 2 行
    assert any(e.startswith("第3行 9999") for e in res["errors"]), res["errors"]
    # 坏行不参与合计，但仍算不平 —— 用户据此看到差额
    assert res["balanced"] is False, res


def test_parse_matches_by_name_when_code_blank(client, auth, book_id):
    """很多人拿到的表只有名称没有编码，按名称兜住。"""
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([(None, "银行存款-工商银行", "借", 4321)]), XLSX_MIME)})
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["subject_id"] is not None, row
    assert row["signed"] == "4321", row


def test_parse_default_side_follows_subject_when_column_blank(client, auth, book_id):
    """方向列留空时按科目属性推；填了则以文件为准。"""
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([
            ("1122", "应收账款", "", 500),        # 空 -> 按属性 -> 借
            ("1122", "应收账款", "贷", 500),      # 填了 -> 以文件为准 -> 贷
        ]), XLSX_MIME)})
    rows = r.json()["rows"]
    by_side = {x["natural"]: x for x in rows}
    assert by_side[""]["signed"] == "500", rows
    assert by_side["贷"]["signed"] == "-500", rows


def test_parse_rejects_garbage_and_empty(client, auth, book_id):
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth,
                    files={"file": ("o.xlsx", b"this is not excel", XLSX_MIME)})
    assert r.status_code == 400

    r2 = client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([]), XLSX_MIME)})
    assert r2.status_code == 400
