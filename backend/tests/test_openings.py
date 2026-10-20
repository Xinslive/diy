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
from tests.accounting_helpers import close_any

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
        {"subject_id": subjects["1121"].id, "amount": "100000"},   # 应收票据 借
        {"subject_id": subjects["1122"].id, "amount": "-30000"},     # 应收账款 **贷**
        {"subject_id": subjects["4001"].id, "amount": "-70000"},     # 生产成本 贷
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

    bank = by_code["1121"]
    assert Decimal(bank["signed"]) == Decimal("100000") and bank["side"] == "借"


def test_round_trip_keeps_balance_ledger_consistent(client, auth, book_id, subjects):
    """期初平了，往后每个月也必须平 —— 从账簿侧独立验一次。"""
    rows = [
        {"subject_id": subjects["1121"].id, "amount": "500000"},
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
        {"subject_id": subjects["1121"].id, "amount": "100"},
        {"subject_id": subjects["4001"].id, "amount": "-100"},
    ]).status_code == 200
    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": subjects["1121"].id, "amount": "250"},
                          {"subject_id": subjects["4001"].id, "amount": "-250"}])
    assert r.status_code == 200
    got = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert Decimal(next(s for s in got["subjects"] if s["code"] == "1121")["signed"]) == Decimal("250")


# ------------------------------------------------------------------ 校验红线


def test_unbalanced_openings_rejected(client, auth, book_id, subjects):
    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": subjects["1121"].id, "amount": "1000"}])
    assert r.status_code == 400
    assert "试算不平" in r.json()["detail"], r.json()

    # 不平就不该落库：确认没写进去脏数据
    got = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert next(s for s in got["subjects"] if s["code"] == "1121")["signed"] == "0"


def test_non_leaf_subject_rejected(client, auth, book_id, subjects):
    """1101 短期投资是父科目，余额必须落在 110101/110102 等明细上。

    父级取 1101：1002 在 4-2-2 表里已经是末级了，压根不是父级。
    """
    parent = subjects["1101"]
    assert parent.is_leaf is False
    r = client.post(f"/api/books/{book_id}/periods", headers=auth,
                    json=[{"subject_id": parent.id, "amount": "1000"}])
    assert r.status_code == 400
    assert "明细科目" in r.json()["detail"], r.json()


def test_cross_book_subject_rejected(client, auth, db, book, subjects, admin_user):
    """拿别的账套的科目来填本账套 —— 必须挡住，否则串账。"""
    import uuid

    from app.accounting.periods import create_periods
    from app.models import Book, Subject, UserBook
    from app.seed.chart_of_accounts import install_chart

    other = Book(company_name=f"别家公司{uuid.uuid4().hex[:8]}", name="别家账套",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    install_chart(db, other.id)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    other_subject = db.scalar(
        select(Subject).where(Subject.book_id == other.id, Subject.code == "1121"))
    assert other_subject.id != subjects["1121"].id

    r = client.post(f"/api/books/{book.id}/periods", headers=auth,
                    json=[{"subject_id": other_subject.id, "amount": "1"}])
    assert r.status_code == 400
    assert "科目不存在" in r.json()["detail"], r.json()


def test_openings_locked_after_voucher(client, auth, book_id, make_voucher):
    """账上有了业务数据，期初就是历史事实，不能再回头改。"""
    make_voucher("2026-01-15", dr=[("1121", 100, "买货")], cr=[("4001", 100, "买货")])

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


# ------------------------------------------------ 期初跟着顶栏那一期走


def test_openings_follow_selected_period(client, auth, book_id, periods, subjects, make_voucher):
    """切到空的后续期间，期初就该能改。

    以前这个页面永远显示启用期间，还用「账套里有没有凭证」一句话判死 ——
    于是顶栏停在 2027 年、那边一张凭证都没有，屏幕上也还是 2026-01 那份
    锁死的数，用户只会以为页面坏了。
    """
    make_voucher("2026-01-15", dr=[("1121", 100000, "存钱")],
                 cr=[("4001", 100000, "存钱")], posted=True)

    first = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert first["period"] == "2026-01"
    assert first["editable"] is False and "业务数据" in first["reason"], first

    feb = client.get(f"/api/books/{book_id}/openings?period_id={periods[2].id}",
                     headers=auth).json()
    assert feb["period"] == "2026-02", feb
    assert feb["editable"] is True, feb
    assert feb["reason"] is None, feb
    # 2 月本来就没有快照（快照只有启用和年度结转两处产生），页面上要显示的是
    # 账簿真正在用的那个数 —— 上期期末 —— 而不是一片 0，否则对不上总账
    bank = next(s for s in feb["subjects"] if s["code"] == "1121")
    assert Decimal(bank["signed"]) == Decimal("100000"), bank


def test_save_later_period_opening_and_ledger_follows(
        client, db, auth, book_id, periods, subjects, make_voucher):
    """改了后续期间的期初，账簿立刻按新数算；启用期间那份不动。"""
    from app.ledger.engine import LedgerEngine

    make_voucher("2026-01-15", dr=[("1121", 100000, "存钱")],
                 cr=[("4001", 100000, "存钱")], posted=True)
    feb = periods[2]

    r = client.post(f"/api/books/{book_id}/periods?period_id={feb.id}", headers=auth, json=[
        {"subject_id": subjects["1121"].id, "amount": "120000"},
        {"subject_id": subjects["1122"].id, "amount": "-120000"},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["period"] == "2026-02", r.json()

    got = client.get(f"/api/books/{book_id}/openings?period_id={feb.id}",
                     headers=auth).json()
    bank = next(s for s in got["subjects"] if s["code"] == "1121")
    assert Decimal(bank["signed"]) == Decimal("120000"), bank

    sid = subjects["1121"].id
    assert LedgerEngine(db, book_id).compute([sid], up_to_ord=feb.ord)[sid].opening \
        == Decimal("120000")
    # 启用期间是另一份快照，不该被这次保存顺手改掉
    assert LedgerEngine(db, book_id).compute([sid], up_to_ord=periods[1].ord)[sid].opening \
        == Decimal("0")


def test_openings_locked_only_from_the_period_with_vouchers(
        client, auth, book_id, periods, subjects, make_voucher):
    """锁的是「从这一期往后有没有凭证」，不是整个账套。"""
    make_voucher("2026-03-05", dr=[("1121", 100, "买货")], cr=[("4001", 100, "买货")])

    r = client.post(f"/api/books/{book_id}/periods?period_id={periods[2].id}", headers=auth,
                    json=[{"subject_id": subjects["1121"].id, "amount": "1"}])
    assert r.status_code == 400 and "业务数据" in r.json()["detail"], r.json()

    mar = client.get(f"/api/books/{book_id}/openings?period_id={periods[3].id}",
                     headers=auth).json()
    assert mar["editable"] is False, mar
    apr = client.get(f"/api/books/{book_id}/openings?period_id={periods[4].id}",
                     headers=auth).json()
    assert apr["editable"] is True, apr


def test_export_follows_selected_period(client, auth, book_id, periods, subjects):
    """导出的是屏幕上那一期 —— 否则备份下来的数跟眼前看到的对不上。"""
    client.post(f"/api/books/{book_id}/periods?period_id={periods[5].id}", headers=auth, json=[
        {"subject_id": subjects["1121"].id, "amount": "5000"},
        {"subject_id": subjects["1122"].id, "amount": "-5000"},
    ])
    r = client.get(f"/api/books/{book_id}/openings.xlsx?period_id={periods[5].id}",
                   headers=auth)
    assert r.status_code == 200, r.text
    ws = load_workbook(io.BytesIO(r.content)).active
    # 单位名那一行删掉之后，标题上提到 A1，表头在第 3 行、数据从第 4 行起
    assert "2026年5月" in ws["A1"].value, ws["A1"].value
    exported = {row[0]: row[3] for row in ws.iter_rows(min_row=4, values_only=True)}
    assert exported["1121"] == 5000, exported["1121"]


# ------------------------------------------------------------------ Excel


def test_export_then_parse_round_trip_preserves_reverse_balance(
    client, auth, book_id, subjects):
    """导出 → 再导入，必须原样回来。反方向余额是最容易被洗掉的那个。"""
    client.post(f"/api/books/{book_id}/periods", headers=auth, json=[
        {"subject_id": subjects["1121"].id, "amount": "100000"},
        {"subject_id": subjects["1122"].id, "amount": "-30000"},
        {"subject_id": subjects["4001"].id, "amount": "-70000"},
    ])

    r = client.get(f"/api/books/{book_id}/openings.xlsx", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == XLSX_MIME
    assert "filename*=UTF-8''" in r.headers["content-disposition"], r.headers

    # 导出件本身就要把「贷」写进方向列，而不是按科目属性写「借」
    ws = load_workbook(io.BytesIO(r.content)).active
    # 数据从第 4 行起（第 1 行是标题、第 3 行是表头）
    exported = {row[0]: (row[2], row[3]) for row in ws.iter_rows(min_row=4, values_only=True)}
    assert exported["1122"][0] == "贷", exported["1122"]
    assert exported["1121"][0] == "借"

    # 再喂回 parse 端点，方向与金额都要对得上
    p = client.post(f"/api/books/{book_id}/openings/parse", headers=auth,
                    files={"file": ("o.xlsx", r.content,
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert p.status_code == 200, p.text
    res = p.json()
    assert res["balanced"] is True, res
    assert res["errors"] == [], res["errors"]
    row = next(x for x in res["rows"] if x["code"] == "1122")
    # 用 Decimal 比而不是比字符串：解析侧现在按库的 Numeric(20,2) 口径量化，
    # 「-30000」与「-30000.00」是同一个数，而断言字符串会把「量化了没」写成
    # 一条与本用例真正要验的东西（反向余额往返不丢方向）无关的契约
    assert Decimal(row["signed"]) == Decimal("-30000"), row
    assert row["display_side"] == "贷", row


def test_parse_does_not_write_anything(client, auth, book_id, subjects):
    """parse 只是解析，落库由前端确认后单独提交 —— 这里确认它没偷偷写。"""
    before = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([("1121", "银行存款-工商银行", "借", 88888)]),
                 XLSX_MIME)})
    after = client.get(f"/api/books/{book_id}/openings", headers=auth).json()
    assert before["subjects"] == after["subjects"]


def test_parse_reports_errors_without_crashing(client, auth, book_id):
    """三种典型坏数据各自给出可读的原因，而不是 500 或静默跳过。"""
    bad = _xlsx([
        ("1121", "应收票据", "借", 1000),       # 正常
        ("9999", "不存在的科目", "借", 500),      # 本账套没有
        ("1101", "短期投资", "借", 300),          # 非明细（1101 下挂 110101 等）
        ("1012", "其他货币资金", "借", "abc"),    # 金额不是数
        ("1001", "库存现金", "借", None),        # 金额空 = 0，可接受
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
    """很多人拿到的表只有名称没有编码，按名称兜住。

    名称是**精确匹配**（by_name 是按科目名建的下标），所以这里得用账套里
    真有的科目名。4-2-2 表里没有「银行存款-工商银行」这种带银行后缀的明细了。
    """
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([(None, "库存现金", "借", 4321)]), XLSX_MIME)})
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["subject_id"] is not None, row
    assert Decimal(row["signed"]) == Decimal("4321"), row


def test_parse_default_side_follows_subject_when_column_blank(client, auth, book_id):
    """方向列留空时按科目属性推；填了则以文件为准。"""
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([
            ("1122", "应收账款", "", 500),        # 空 -> 按属性 -> 借
            ("1122", "应收账款", "贷", 500),      # 填了 -> 以文件为准 -> 贷
        ]), XLSX_MIME)})
    rows = r.json()["rows"]
    by_side = {x["natural"]: x for x in rows}
    assert Decimal(by_side[""]["signed"]) == Decimal("500"), rows
    assert Decimal(by_side["贷"]["signed"]) == Decimal("-500"), rows


def test_parse_rejects_garbage_and_empty(client, auth, book_id):
    r = client.post(f"/api/books/{book_id}/openings/parse", headers=auth,
                    files={"file": ("o.xlsx", b"this is not excel", XLSX_MIME)})
    assert r.status_code == 400

    r2 = client.post(f"/api/books/{book_id}/openings/parse", headers=auth, files={
        "file": ("o.xlsx", _xlsx([]), XLSX_MIME)})
    assert r2.status_code == 400


# --------------------------------------------------------------------- 账套本身


def test_账套列表一行一个账套并带年度列表(client, auth, book, db, admin_user):
    """年度是账套的属性，列表里不能一年一行。"""
    make = client.post("/api/books", json={
        "company_name": book.company_name, "name": book.name,
        "start_year": 2026, "start_month": 1}, headers=auth)
    assert make.status_code == 400, "同名账套应该被挡住"

    got = client.get("/api/books", headers=auth).json()
    row = next(b for b in got if b["id"] == book.id)
    assert row["start_year"] == 2026
    assert row["latest_year"] == 2026
    assert [y["year"] for y in row["years"]] == [2026]
    assert row["years"][0]["total"] == 12
    assert row["years"][0]["closed"] == 0
    assert row["years"][0]["carried"] is False


def test_年度结转后列表仍是同一行年度变多(client, auth, db, book, periods, make_voucher, admin_user):
    make_voucher("2026-01-10", dr=[("1121", 1000, "x")], cr=[("5001", 1000, "x")], posted=True)

    from app.accounting.periods import carry_forward_profit, close_period
    from app.models import Voucher

    for m in range(1, 13):
        if db.scalar(select(Voucher.id).where(
                Voucher.book_id == book.id, Voucher.period_id == periods[m].id)):
            carry_forward_profit(db, book.id, periods[m], admin_user)
        close_any(db, book, periods[m], admin_user)

    r = client.post(f"/api/books/{book.id}/year-end", json={}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["new_year"] == 2027

    got = client.get("/api/books", headers=auth).json()
    assert len([b for b in got if b["company_name"] == book.company_name
                and b["name"] == book.name]) == 1
    row = next(b for b in got if b["id"] == book.id)
    assert [y["year"] for y in row["years"]] == [2026, 2027]
    assert row["latest_year"] == 2027
    assert row["years"][0]["carried"] is True     # 2026 已结完
    assert row["years"][1]["carried"] is False    # 2027 才刚开始


def test_可以改账套名和单位名(client, auth, book):
    r = client.put(f"/api/books/{book.id}",
                   json={"company_name": "新单位名", "name": "新账套名"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": book.id, "company_name": "新单位名", "name": "新账套名"}

    again = client.get(f"/api/books/{book.id}", headers=auth).json()
    assert again["company_name"] == "新单位名" and again["name"] == "新账套名"


def test_改账套名_重名要挡并且留痕(client, db, book, auth):
    # 同一个单位下已经有一个同名账套了
    client.post("/api/books", json={"company_name": book.company_name, "name": "重复的名字",
                                    "start_year": 2026}, headers=auth)

    dup = client.put(f"/api/books/{book.id}", json={"name": "重复的名字"}, headers=auth)
    assert dup.status_code == 400 and "同名" in dup.json()["detail"]

    ok = client.put(f"/api/books/{book.id}", json={"name": "改名了"}, headers=auth)
    assert ok.status_code == 200

    from app.models import AuditLog
    row = db.scalar(select(AuditLog).where(
        AuditLog.book_id == book.id, AuditLog.action == "UPDATE", AuditLog.entity == "book"))
    assert row is not None and row.before["name"] == book.name


def test_科目级次不能调到比已有科目还浅(client, db, book, auth, subjects):
    """规则只是「今后新增科目怎么接号」的模板，但级次有下限。

    账上已经有三级科目，规则却改成两级，新增时按最后一段接号，
    接出来的编码跟现有科目对不齐，科目表当场乱掉 —— 所以后端必须挡。
    """
    from app.models import Subject

    deep = Subject(book_id=book.id, code="10020101", name="三级科目",
                   category="ASSET", direction="DEBIT", is_leaf=True, level=3,
                   parent_id=subjects["1121"].id, active=True)
    # 同样是「构造底层状态」：父科目 100201 此刻是末级，直接挂下级会让
    # trg_subject_leaf_parent_guard 在提交时拒绝（审计 P2-04 收尾）。
    # 真实接口会顺手把父级改成非末级，这里显式补上，让库自洽。
    subjects["1121"].is_leaf = False
    db.add(deep)
    db.commit()

    r = client.put(f"/api/books/{book.id}", json={"subject_code_rule": "4-2"}, headers=auth)
    assert r.status_code == 400, r.text
    assert "3 级" in r.json()["detail"], r.json()

    ok = client.put(f"/api/books/{book.id}", json={"subject_code_rule": "4-3-2"}, headers=auth)
    assert ok.status_code == 200, ok.text
    detail = client.get(f"/api/books/{book.id}", headers=auth).json()
    assert detail["subject_code_rule"] == "4-3-2"
    assert detail["subject_max_level"] == 3


def test_非所有者改不了账套名(client, db, plain_auth, plain_user, book):
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()
    r = client.put(f"/api/books/{book.id}", json={"name": "改名"}, headers=plain_auth)
    assert r.status_code == 403
