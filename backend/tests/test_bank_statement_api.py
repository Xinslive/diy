"""流水对账的**接口契约**：路径、权限、返回形状。

这一组盯的是「接口这一层」而不是业务逻辑（业务逻辑在
``test_bank_statement_service.py`` 里）：

  · 路径挂在 ``/api/books/{book_id}/bank-statements`` 下；
  · 写接口挂了 ``require_book_write``（只读账号不能上传、不能制证）；
  · 列表接口带上「账面余额 / 差额」—— 页面不用再发一轮请求去拼这些数；
  * 行级接口能改对方科目、能定方向、能忽略。
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.models import BankStatement, BankStatementRow, Subject

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def cash(db, book):
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    parent.is_leaf = False
    db.flush()
    sub = Subject(book_id=book.id, year=book.start_year, code="100201", name="基本户",
                  parent_id=parent.id, level=parent.level + 1, category="ASSET",
                  direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=1,
                  active=True)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _upload(client, auth, book, cash, name="cmb_csv.xlsx"):
    data = (FIXTURES / name).read_bytes()
    return client.post(
        f"/api/books/{book.id}/bank-statements",
        headers=auth,
        files={"file": (name, data, "application/octet-stream")},
        data={"account_subject_id": str(cash.id)},
    )


def _blank_pdf() -> bytes:
    """一份只有空白页、没有文本层的 PDF。"""
    import io

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument.new()
    try:
        document.new_page(595, 842)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    finally:
        document.close()


# ---------------------------------------------------------------------- 上传


def test_超大小的文件被拒(client, auth, book, cash, store, monkeypatch):
    """文件大小沿用 ``core/uploads.py`` 那把尺 —— 超了直接 413，不进内存。

    把上限调到很小来测：真的造一个几十 MB 的文件既慢又占地方，
    而这道闸看的就是「声明的大小 vs 上限」。
    """
    monkeypatch.setattr(settings, "import_max_bytes", 64)
    r = _upload(client, auth, book, cash)
    assert r.status_code == 413, r.text
    assert "超过上限" in r.json()["detail"], r.text


def test_上传后能列出对账单(client, auth, db, book, cash, store):
    r = _upload(client, auth, book, cash)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["row_count"] == 5
    assert body["container_format"] == "CSV"

    r = client.get(f"/api/books/{book.id}/bank-statements", headers=auth)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["filename"] == "cmb_csv.xlsx"
    # **列表里就带着差额** —— 页面不用再发一轮请求去拼这些数字
    assert "reconcile" in items[0]
    assert "status_counts" in items[0]


def test_解析失败接口仍保留FAILED台账与原件(client, auth, db, book, cash, store):
    r = client.post(
        f"/api/books/{book.id}/bank-statements",
        headers=auth,
        files={"file": ("扫描件.pdf", _blank_pdf(), "application/pdf")},
        data={"account_subject_id": str(cash.id)},
    )
    assert r.status_code == 400, r.text
    assert "原件已保留" in r.json()["detail"], r.text

    db.expire_all()
    statement = db.scalars(select(BankStatement)).one()
    assert statement.parse_status == "FAILED"
    assert "没有文本层" in statement.parse_message
    assert (store / statement.file_path).exists()


def test_上传时要选现金科目(client, auth, book, cash, store):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    r = client.post(
        f"/api/books/{book.id}/bank-statements",
        headers=auth,
        files={"file": ("x.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 422, r.text


def test_重复上传被拒(client, auth, book, cash, store):
    assert _upload(client, auth, book, cash).status_code == 201
    r = _upload(client, auth, book, cash)
    assert r.status_code == 400
    assert "已经有一份对账单" in r.json()["detail"]


def test_只读账号不能上传(client, db, book, cash, store, plain_auth):
    r = _upload(client, plain_auth, book, cash)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------- 明细


def test_取流水行(client, auth, book, cash, store):
    created = _upload(client, auth, book, cash).json()
    r = client.get(f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
                   headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 5
    assert rows[0]["seq"] == 1
    assert rows[0]["voucher_state"] in ("READY", "PENDING", "UNKNOWN", "NOAMOUNT")


def test_改对方科目(client, auth, db, book, cash, store, subjects):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    assert r.status_code == 200, r.text
    assert r.json()["plan_status"] == "MANUAL"


def test_人工指定方向(client, auth, db, book, cash, store):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.post(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}/direction",
        headers=auth, json={"direction": "IN"})
    assert r.status_code == 200, r.text
    assert r.json()["direction"] == "IN"


def test_忽略一行(client, auth, db, book, cash, store):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"status": "IGNORED"})
    assert r.status_code == 200
    assert r.json()["voucher_state"] == "IGNORED"


# ---------------------------------------------------------------------- 凭证摘要


def test_行接口带出按规则算出来的摘要(client, auth, db, book, cash, store):
    """界面上那几个「凭证摘要」输入框的**默认值**从这儿来。

    它必须是**后端算出来的那一句**（规则级模板 → 内置口径），
    不是前端自己拼的 —— 前端另拼一份，两边迟早漂：输入框里一句话、
    生成的凭证上另一句话。

    **默认摘要本身是行级的**：模板管的是「这一类流水通常怎么写摘要」，
    而每一个对方科目各有各的一句 —— 那是 ``plan_subjects`` 里每一项的
    ``summary_override``。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    # 默认摘要有内容（这份对账单没设模板，走内置口径：用途优先、摘要兜底）
    assert rows[0]["default_summary"], "没有算出默认摘要"
    # **行级不再有「整张凭证的摘要」了** —— 摘要的粒度是对方科目。
    # 断言它**不在**，是为了让旧形状回不来。
    assert "summary_override" not in rows[0], (
        "行上又有整张凭证的摘要了 —— 摘要应该按对方科目分别设置")


def test_每个对方科目可以有自己的摘要(client, auth, db, book, cash, store, subjects):
    """**每一个对方科目一句话**，各写各的。

    一笔 5000 的付款拆成「业务招待费 800 + 房屋租赁费 200」，这两个科目
    该写的话本来就不一样 ——「招待客户」「办公室房租」。一个框管全部，
    就必然有一边写不对。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    amount = row["amount"]
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": str(round(float(amount) * 0.8, 2)), "summary": "招待客户"},
            {"subject_id": subjects["560207"].id,
             "amount": str(round(float(amount) * 0.2, 2)), "summary": "办公室房租"},
        ]})
    assert r.status_code == 200, r.text
    plans = r.json()["plan_subjects"]
    assert [p["summary_override"] for p in plans] == ["招待客户", "办公室房租"], plans

    # 再读一次，确认**存下来了**（不是只在返回里好看一下）
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    plans = rows[0]["plan_subjects"]
    assert [p["summary_override"] for p in plans] == ["招待客户", "办公室房租"], plans


def test_指定科目时可以带摘要(client, auth, db, book, cash, store, subjects):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": row["amount"], "summary": "备用金"}]})
    assert r.status_code == 200, r.text
    assert r.json()["plan_subjects"][0]["summary_override"] == "备用金"
    # **默认摘要还在**：那是「没改的话会是什么」，输入框要靠它当底稿
    assert r.json()["default_summary"]


def test_摘要留空等于交回规则(client, auth, db, book, cash, store, subjects):
    """清空那个输入框 = 「我不想改」= 跟着规则走，**不是**「摘要留空」。

    而「没带 summary」与「带了空串」是**两件事**：
    前者 = 沿用这个科目上原来那句，后者 = 交回规则。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": row["amount"], "summary": "备用金"}]})
    # **带了空串** = 清空 = 交回规则
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": row["amount"], "summary": ""}]})
    assert r.status_code == 200, r.text
    assert r.json()["plan_subjects"][0]["summary_override"] == ""


def test_人工摘要优先于规则算出来的那句(client, auth, db, book, cash, store, subjects):
    """人工写的那句**就是那一条对方科目分录上的摘要**。

    这是这个功能的核心：输入框里写什么，那条分录上就是什么。
    """
    from app.models import Entry

    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": row["amount"], "summary": "备用金"}]})
    client.post(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}/direction",
        headers=auth, json={"direction": "IN"})
    client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=auth)

    db.expire_all()
    row = db.get(BankStatementRow, rows[0]["id"])
    assert row.matched_voucher_id, "这一行应该已经制证了"
    entries = db.scalars(
        select(Entry).where(Entry.voucher_id == row.matched_voucher_id)).all()
    # **对方那一条** = 人工写的那句
    other = [e for e in entries if e.subject_id == subjects["560202"].id]
    assert len(other) == 1, entries
    assert other[0].summary == "备用金", other[0].summary
    # **现金那一条** = 规则算出来的那句（它是整笔的描述，与科目无关）
    cash_entries = [e for e in entries if e.subject_id != subjects["560202"].id]
    assert len(cash_entries) == 1, entries
    assert cash_entries[0].summary != "备用金", (
        "现金那一侧被人工摘要盖住了 —— 它该用规则算出来的那句")
    assert cash_entries[0].summary, "现金那一侧的摘要不该是空的"


def test_凭证上每个对方科目各写各的(client, auth, db, book, cash, store, subjects):
    """两个对方科目、两句摘要 —— 凭证上就得分开。

    **这一条是整个改动的验收点**：以前一句话管全部分录，
    现在每个对方科目各有各的。
    """
    from app.models import Entry

    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    amount = float(row["amount"])
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": str(round(amount * 0.8, 2)), "summary": "招待客户"},
            {"subject_id": subjects["560207"].id,
             "amount": str(round(amount * 0.2, 2)), "summary": "办公室房租"},
        ]})
    client.post(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}/direction",
        headers=auth, json={"direction": "IN"})
    client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=auth)

    db.expire_all()
    row = db.get(BankStatementRow, rows[0]["id"])
    entries = db.scalars(
        select(Entry).where(Entry.voucher_id == row.matched_voucher_id)).all()
    by_subject = {e.subject_id: e.summary for e in entries}
    assert by_subject[subjects["560202"].id] == "招待客户", by_subject
    assert by_subject[subjects["560207"].id] == "办公室房租", by_subject


def test_摘要跟着同一个科目走(client, auth, db, book, cash, store, subjects):
    """重开一次弹窗、只改金额，**人工写的那句摘要不能丢**。

    而**换了科目就不带走** —— 那是另一个科目的话，不该接着上一句。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": row["amount"], "summary": "备用金"}]})
    # **同一个科目、这次没带 summary** → 沿用原来那句
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": row["amount"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["plan_subjects"][0]["summary_override"] == "备用金", (
        "同一个科目上的手工摘要被冲掉了")

    # **换了科目、也没带 summary** → 不带走
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560207"].id, "amount": row["amount"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["plan_subjects"][0]["summary_override"] == "", (
        "换了科目却把上一个科目的摘要带过来了")


def test_旧的行级摘要写法被明确拒绝(client, auth, db, book, cash, store):
    """行级的「整张凭证摘要」已经没有意义了 —— 但要**说清楚**，不能静默丢掉。

    请求体在进服务层之前就被 pydantic 过滤过了：不认识的字段会被
    **静默丢掉** —— 接口回 200、库里什么都没变。「写了却看不见，
    等于没写」。所以这里要收到一句明确的话。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"summary": "备用金"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "plan_subjects" in detail, detail
    assert "对方科目" in detail, detail


def test_已入账的行不许改摘要(client, auth, db, book, cash, store, subjects):
    """凭证已经写死了摘要 —— 改流水行**不会**回头改凭证，两边会对不上。

    所以这条与「对方科目不许改」是同一条闸（同一个判据
    ``matched_voucher_id``）：摘要是那条分录上的字段，凭证已经定死了。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    client.post(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}/direction",
        headers=auth, json={"direction": "IN"})
    client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=auth)

    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id,
             "amount": row["amount"], "summary": "改成别的"}]})
    assert r.status_code == 400, r.text
    assert "凭证" in r.json()["detail"]


# ------------------------------------------------------------------ 发票占用


def _占用一张发票(db, book, row_id) -> None:
    """把一行流水标成「被这张发票占用」。

    **发票那张凭证的分录是发票自己算出来的**（``app/invoice/service.py``
    用的是预览时算出来的 ``resolution.entries``），流水这一侧只贡献**结算
    账户** —— 所以流水行上填的对方科目**根本不会进那张凭证**。
    """
    from app.models import InvoiceBatch, InvoiceRecord

    batch = InvoiceBatch(book_id=book.id, filename="测试发票.pdf",
                         file_path="test/invoice.pdf")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(batch_id=batch.id, book_id=book.id, page_index=1)
    db.add(record)
    db.flush()
    row = db.get(BankStatementRow, row_id)
    row.matched_invoice_record_id = record.id
    db.commit()


def test_被发票占用的行不许指定科目(client, auth, db, book, cash, store, subjects):
    """发票占用的行，对方科目**由发票决定** —— 流水这边不许填。

    **为什么**：发票那张凭证的分录是发票自己算出来的，流水行上填的对方科目
    根本不进那张凭证。让用户去填一个不生效的东西，是骗人；填了还会以为
    「就这么记了」。

    **判据与前端同一条**（``matched_invoice_record_id``）：两边各判一次的话，
    迟早出现「界面能点、点了被拒」或反过来的毛病。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    _占用一张发票(db, book, row["id"])

    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "发票" in detail, detail

    # **没被占用的那一行照样能改** —— 这道闸只挡被占用的行
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[1]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    assert r.status_code == 200, r.text


def test_被发票占用的行不会被制证(client, auth, db, book, cash, store, subjects):
    """被发票占用的行**不进制证候选** —— 它由发票那张凭证管。

    （这条是既有行为，钉在这里是因为「发票占用 → 流水这边什么都别做」
    是一整套：不能指定科目、不能制证。）

    **这条不能空跑**：那行如果本来就没有对方科目，它会被「对方科目待确认」
    挡掉 —— 那跟发票没关系，断言照样过。所以先把这一行**做成合法的制证
    候选**（对方科目齐、方向也定了），确认它真的进了候选池，再让发票来
    占住它 —— 这时候它掉出候选池才**只可能**是发票占用的功劳。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    row = rows[0]
    # 先把它做成**合法的制证候选**
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}",
        headers=auth, json={"plan_subjects": [
            {"subject_id": subjects["560202"].id, "amount": row["amount"]}]})
    assert r.status_code == 200, r.text
    client.post(
        f"/api/books/{book.id}/bank-statements/rows/{row['id']}/direction",
        headers=auth, json={"direction": "IN"})
    plan = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/plan-vouchers",
        headers=auth).json()
    assert any(c["id"] == row["id"] for c in plan["candidates"]), (
        "这一行本来就不是制证候选 —— 后面那句断言就成空跑了")

    # 现在这张发票来占住它
    _占用一张发票(db, book, row["id"])

    plan = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/plan-vouchers",
        headers=auth).json()
    assert not any(c["id"] == row["id"] for c in plan["candidates"]), (
        "被发票占用的行不该进制证候选 —— 那会与发票那张凭证记同一笔钱")
    skipped = [s for s in plan["skipped"] if s["id"] == row["id"]]
    assert skipped and "发票" in skipped[0]["reason"], plan

    client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=auth)

    db.expire_all()
    row = db.get(BankStatementRow, rows[0]["id"])
    assert row.matched_voucher_id is None, (
        "被发票占用的行不该被流水制证 —— 那会与发票那张凭证记同一笔钱")


# ---------------------------------------------------------------------- 制证


def test_计划与提交凭证(client, auth, db, book, cash, store, subjects):
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    client.post(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}/direction",
        headers=auth, json={"direction": "IN"})

    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/plan-vouchers",
        headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["candidate_count"] >= 1

    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["created_count"] >= 1


def test_只读账号不能制证(client, auth, db, book, cash, store, plain_auth, subjects):
    created = _upload(client, auth, book, cash).json()
    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/commit-vouchers",
        headers=plain_auth)
    assert r.status_code == 403, r.text


def test_只读账号连可制证清单都调不了(client, auth, db, book, cash, store,
                                       plain_user, plain_auth):
    """**后端这道闸不许放松**（2026-10-28 修只读账号的假错误时钉的）。

    「可制证清单」那个接口（``plan-vouchers``）**会写库** —— 它要持久化去重
    标记，所以后端给它挂了写权限。于是 VIEWER 打开「流水明细」时那一趟必然
    403，而**流水行其实好好地显示着**。

    修的是**前端**（只读账号压根不调它；万一调了也静默降级），不是这道闸：
    闸门放松了，只读账号就能改库里的去重标记了 —— 那才是真的出事了。
    """
    from app.models import UserBook

    created = _upload(client, auth, book, cash).json()
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()

    r = client.post(
        f"/api/books/{book.id}/bank-statements/{created['id']}/plan-vouchers",
        headers=plain_auth)
    assert r.status_code == 403, (
        f"只读账号竟然调得了可制证清单：{r.status_code} {r.text[:200]}")


# ---------------------------------------------------------------------- 规则与画像


def test_流水科目规则的增删(client, auth, db, book, subjects):
    r = client.post(f"/api/books/{book.id}/bank-statement-rules", headers=auth, json={
        "match_field": "SUMMARY", "keyword": "运费",
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    assert r.status_code == 201, r.text
    rule_id = r.json()["id"]
    assert r.json()["match_label"] == "摘要"

    r = client.get(f"/api/books/{book.id}/bank-statement-rules", headers=auth)
    assert len(r.json()) == 1

    r = client.delete(f"/api/books/{book.id}/bank-statement-rules/{rule_id}",
                      headers=auth)
    assert r.status_code == 200
    r = client.get(f"/api/books/{book.id}/bank-statement-rules", headers=auth)
    assert r.json() == []


def test_格式画像的存与删(client, auth, db, book):
    r = client.post(f"/api/books/{book.id}/bank-statement-profiles", headers=auth,
                    json={"fingerprint": "abc123", "column_mapping": {"DATE": 0}})
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]
    r = client.get(f"/api/books/{book.id}/bank-statement-profiles", headers=auth)
    assert len(r.json()) == 1
    r = client.delete(f"/api/books/{book.id}/bank-statement-profiles/{profile_id}",
                      headers=auth)
    assert r.status_code == 200


# ---------------------------------------------------------------------- 隔离


def test_跨账套看不到别人的对账单(client, auth, db, book, cash, store, admin_user):
    from app.models import Book, UserBook

    created = _upload(client, auth, book, cash).json()
    other = Book(company_name="别家", name="别家账套", start_year=2026,
                 start_month=1, subject_code_rule="4-2-2-2", status="ACTIVE")
    db.add(other)
    db.commit()
    db.refresh(other)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    r = client.get(f"/api/books/{other.id}/bank-statements", headers=auth)
    assert r.status_code == 200
    assert r.json() == []
    # 直接按 id 去取别的账套的那一份也不行
    r = client.get(f"/api/books/{other.id}/bank-statements/{created['id']}",
                   headers=auth)
    assert r.status_code == 404


def test_只改状态不该顺手清掉对方科目(client, auth, db, book, cash, store, subjects):
    """改一个字段不许抹掉另一个。

    少了 ``exclude_unset`` 的话，``model_dump()`` 会把没传的字段也带上
    （值是 None）—— 「只改状态」那次调用顺手把对方科目清空了。
    """
    created = _upload(client, auth, book, cash).json()
    rows = client.get(
        f"/api/books/{book.id}/bank-statements/{created['id']}/rows",
        headers=auth).json()
    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"plan_subject_id": subjects["560202"].id})
    assert [i["subject_id"] for i in r.json()["plan_subjects"]] == [subjects["560202"].id]

    r = client.patch(
        f"/api/books/{book.id}/bank-statements/rows/{rows[0]['id']}",
        headers=auth, json={"status": "IGNORED"})
    assert r.status_code == 200, r.text
    assert [i["subject_id"] for i in r.json()["plan_subjects"]] == [subjects["560202"].id], (
        "只改状态就把对方科目清空了")
    assert r.json()["status"] == "IGNORED"
