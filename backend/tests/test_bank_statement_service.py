"""流水对账的服务层：对账单台账、流水行、规则、画像、去重与制证。

这些用例跑的是真的脱敏样例（``tests/fixtures/statements/``）+ 真的账套，
不是打桩后的假绿 —— 流水制证这一段最容易出错的地方是**借贷方向**与
**重复入账**，而这两件事只有对着真的凭证表才验得出来。
"""
from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bankstatement import service
from app.core.config import settings
from app.models import (BankStatement, BankStatementRow, InvoiceBatch,
                         InvoiceRecord, Subject, Voucher)
from app.models.bankstatement import (
    DIRECTION_IN,
    DIRECTION_OUT,
    PLAN_MANUAL,
    PLAN_PENDING,
    PLAN_RULE,
    STATUS_IGNORED,
    STATUS_OPEN,
    STATUS_POSTED,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"


def plan_subject_ids(row) -> list[int]:
    """这一行的对方科目 id 列表（可能不止一个）。

    **对方科目是子表了**（一行流水可以拆到好几个科目上），所以断言不再看
    单个 id，而是看这个列表。
    """
    return [item["subject_id"] for item in row["plan_subjects"]]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """对账单原件落到临时目录 —— 绝不写进仓库的 data/statements。"""
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


def add_sub_subject(db, book, parent_code: str, code: str, name: str,
                    sort_no: int = 1):
    """在某个科目下挂一个明细科目。

    **必须先把父科目的 ``is_leaf`` 改成 False**：库层有
    ``fn_subject_leaf_parent_guard``，父科目还是末级时挂下级会被拒
    （``is_leaf`` 由服务端按子级关系维护，见 db_init.sql §5b）。
    """
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == parent_code))
    assert parent is not None, f"账套里没有 {parent_code} 科目"
    parent.is_leaf = False
    db.flush()
    sub = Subject(
        book_id=book.id, year=book.start_year, code=code, name=name,
        parent_id=parent.id, level=parent.level + 1, category="ASSET",
        direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=sort_no,
        active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


@pytest.fixture
def cash(db, book):
    """一个 1002 的**明细**科目（真实用户就是这么用的：1002 下面挂好几个
    子科目，一账户一份对账单）。"""
    return add_sub_subject(db, book, "1002", "100201", "基本户", 1)


@pytest.fixture
def uploaded(db, book, admin_user, cash, store):
    """上传一份招商 CSV（5 行）后的对账单。"""
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    return service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                    data, cash.id)


# ----------------------------------------------------------------------
# 上传与台账
# ----------------------------------------------------------------------

def test_上传后对账单与流水行都落库(uploaded, db):
    assert uploaded["row_count"] == 5
    assert uploaded["container_format"] == "CSV", (
        "扩展名是 .xlsx 但内容是 CSV —— 必须按文件头认容器")
    statement = db.get(BankStatement, uploaded["id"])
    assert statement is not None
    rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id)).all()
    assert len(rows) == 5


def test_原件留存在磁盘上(uploaded, store):
    assert (store / str(uploaded["id"])).exists() or list(store.rglob("*.csv")), (
        "原件没落盘 —— 解析器改了之后就没有重来的余地了")


def test_同一账户同一期间重复上传被拒(db, book, admin_user, cash, store, uploaded):
    """同一份流水传两遍会生成两遍凭证 —— 钱在账上多记一次。"""
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    with pytest.raises(ValueError) as excinfo:
        service.upload_statement(db, book.id, admin_user, "招商9月(2).xlsx",
                                 data, cash.id)
    assert "已经有一份对账单" in str(excinfo.value)


def test_不同账户同一期间可以各传一份(db, book, admin_user, cash, store):
    """守卫是「同账户 + 同期间」，不是「同期间」。"""
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    service.upload_statement(db, book.id, admin_user, "招商9月.xlsx", data, cash.id)

    other = add_sub_subject(db, book, "1002", "100202", "一般户", 2)
    service.upload_statement(db, book.id, admin_user, "招商9月-一般户.xlsx",
                             data, other.id)


def test_改了科目编码后台账显示新码(db, book, admin_user, uploaded):
    """**科目编码是可以改的**（``SubjectEditIn.code``）—— 改完台账不能还摆着旧码。

    快照列（``account_subject_code``）是上传那一刻的副本，用户改码它不会跟着变。
    直接拿快照显示的话，台账上就是「旧码 + 新名」：1001 库存现金 与
    100201 银行存款只差一位，肉眼扫过去根本看不出哪里不对
    （实测：dev 库里一份绑在 100201 上的对账单，界面上摆的是「1001」）。

    **名称一直取活科目，码也该从同一个地方来** —— 两边不一致比哪边旧更糟。
    """
    subject = db.get(Subject, uploaded["account_subject_id"])
    statement = db.get(BankStatement, uploaded["id"])
    assert subject.code == "100201"
    subject.code = "100203"
    db.commit()

    data = service.serialize_statement(db, statement)
    assert data["account_subject_code"] == "100203"
    assert data["account_subject_name"] == subject.name
    # 快照**不动**（它是「当年绑的是哪个账户」的记录），但显示时不用它。
    assert statement.account_subject_code == "100201"


def test_科目行不在了才回落快照里的码(uploaded):
    """科目行真的没了（老数据、或绕过了外键的手工操作），
    快照是「这份对账单当年绑的是哪个账户」的唯一线索，不能丢。"""
    from app.bankstatement.caliber import account_subject_code

    class _NoSubject:
        code = "不该被用到"

    class _Gone:
        account_subject_code = "100201"

    assert account_subject_code(_NoSubject(), _Gone()) == "不该被用到"
    assert account_subject_code(None, _Gone()) == "100201"


def test_非末级科目不能绑(db, book, admin_user, store):
    """「1002 银行存款」下面挂了几个明细科目，它自己不是末级。

    绑到它上面的话，凭证会被库层守卫拦下（分录只能挂末级科目）。

    **用的必须是银行账户**（2026-10-28 口径变了：这一条线只认 1002）——
    拿 1101 短期投资来测，会先撞上「不是银行账户」那道闸，压根走不到
    「末级科目」这一关，验的就不是想验的东西了。

    「非末级」这个状态得自己造：科目表补齐之后，1002 下面没有明细时它
    自己就是末级（真实用户则是挂了几个子账户）。
    """
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "1002"))
    assert parent is not None
    parent.is_leaf = False
    db.flush()
    db.add(Subject(
        book_id=book.id, year=book.start_year, code="100201", name="基本户",
        parent_id=parent.id, level=parent.level + 1, category="ASSET",
        direction="DEBIT", is_leaf=True, carry_forward=False, sort_no=1,
        active=True,
    ))
    db.commit()
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    with pytest.raises(ValueError) as excinfo:
        service.upload_statement(db, book.id, admin_user, "招商9月.xlsx", data, parent.id)
    assert "末级科目" in str(excinfo.value)


def test_不是银行账户的科目不能绑(db, book, admin_user, store):
    """1001 库存现金、1012 第三方支付备付金、1101 短期投资 —— 都不是银行账户。

    **网银导出的对账单对不上它们**（2026-10-28 用户定的口径：这一条线只认
    1002 银行存款）。前端的下拉也不给选，这里是**同一道闸** —— 两边各判
    一次的话，迟早出现「界面选不了、接口却收下」。
    """
    for code in ("1001", "1012", "1101"):
        subject = db.scalar(select(Subject).where(
            Subject.book_id == book.id, Subject.year == book.start_year,
            Subject.code == code))
        if subject is None:
            continue
        data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
        with pytest.raises(ValueError) as excinfo:
            service.upload_statement(
                db, book.id, admin_user, "招商9月.xlsx", data, subject.id)
        assert "不是银行账户" in str(excinfo.value), (
            f"{code} 应该被「不是银行账户」挡下，实际：{excinfo.value}")


def test_跨账套看不到别人的对账单(db, book, uploaded, admin_user):
    """另一个账套查这份对账单 —— 404，不是「查不到就当没有」。"""
    from app.models import Book, UserBook

    other = Book(company_name="别家", name="别家账套", start_year=2026,
                 start_month=1, subject_code_rule="4-2-2-2", status="ACTIVE")
    db.add(other)
    db.commit()
    db.refresh(other)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    with pytest.raises(ValueError):
        service._statement(db, other.id, uploaded["id"])
    assert service.list_statements(db, other.id) == []


# ----------------------------------------------------------------------
# 逐行操作
# ----------------------------------------------------------------------

def test_人工指定方向(uploaded, db, book):
    rows = service.list_rows(db, book.id, uploaded["id"])
    target = rows[0]
    result = service.set_row_direction(db, book.id, target["id"], DIRECTION_IN)
    assert result["direction"] == DIRECTION_IN
    assert "方向未知" not in " ".join(result["problems"])


def test_方向只能是收或付(uploaded, db, book):
    rows = service.list_rows(db, book.id, uploaded["id"])
    with pytest.raises(ValueError):
        service.set_row_direction(db, book.id, rows[0]["id"], "MAYBE")


def test_人工指定对方科目后状态变成MANUAL(uploaded, db, book, subjects):
    rows = service.list_rows(db, book.id, uploaded["id"])
    target = rows[0]
    result = service.update_row(db, book.id, target["id"], {
        "plan_subject_id": subjects["560202"].id})
    assert result["plan_status"] == PLAN_MANUAL
    assert plan_subject_ids(result) == [subjects["560202"].id]


def test_忽略一行(uploaded, db, book):
    rows = service.list_rows(db, book.id, uploaded["id"])
    result = service.update_row(db, book.id, rows[0]["id"], {"status": STATUS_IGNORED})
    assert result["status"] == STATUS_IGNORED
    assert result["voucher_state"] == "IGNORED"


# ----------------------------------------------------------------------
# 流水科目规则
# ----------------------------------------------------------------------

def test_规则命中则填对方科目_没命中标待确认(db, book, admin_user, cash, store, subjects):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                         data, cash.id)
    rows = service.list_rows(db, book.id, statement["id"])
    # 拿第一行的摘要配一条规则
    keyword = rows[0]["summary"][:4]
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": keyword,
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    rows = service.list_rows(db, book.id, statement["id"])
    hit = [r for r in rows if r["summary"].startswith(keyword)]
    assert hit, "找不到摘要以关键词开头的行"
    assert hit[0]["plan_status"] == PLAN_RULE
    assert plan_subject_ids(hit[0]) == [subjects["560202"].id]
    others = [r for r in rows if not r["summary"].startswith(keyword)]
    assert all(r["plan_status"] == PLAN_PENDING for r in others)


def test_同级规则取更长关键词(db, book, admin_user, cash, store, subjects):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                         data, cash.id)
    rows = service.list_rows(db, book.id, statement["id"])
    summary = rows[0]["summary"]
    if len(summary) < 6:
        pytest.skip("这份样例的摘要太短，凑不出长短两个关键词")
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": summary[:3],
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": summary[:6],
        "subject_id": subjects["560301"].id, "priority": 0, "active": True})
    rows = service.list_rows(db, book.id, statement["id"])
    target = [r for r in rows if r["summary"].startswith(summary[:6])]
    assert target and plan_subject_ids(target[0]) == [subjects["560301"].id], (
        "同优先级时该取更长的那个关键词")


def test_优先级大的赢(db, book, admin_user, cash, store, subjects):
    """两条规则都命中时，**优先级**说了算 —— 哪怕它的关键词更短。

    「同优先级取更长关键词」只是同优先级时的兜底；优先级是用户
    明确表达过的意图，它更大。
    """
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                         data, cash.id)
    rows = service.list_rows(db, book.id, statement["id"])
    summary = rows[0]["summary"]
    if len(summary) < 4:
        pytest.skip("这份样例的摘要太短，凑不出长短两个关键词")
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": summary[:4],
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": summary[:2],
        "subject_id": subjects["560301"].id, "priority": 10, "active": True})
    rows = service.list_rows(db, book.id, statement["id"])
    target = [r for r in rows if r["summary"].startswith(summary[:4])]
    assert target and plan_subject_ids(target[0]) == [subjects["560301"].id], (
        "优先级高的规则没赢")


def test_删规则之后重回待确认(db, book, admin_user, cash, store, subjects):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                         data, cash.id)
    rows = service.list_rows(db, book.id, statement["id"])
    keyword = rows[0]["summary"][:4]
    rule = service.create_rule(db, book.id, admin_user, {
        "match_field": "SUMMARY", "keyword": keyword,
        "subject_id": subjects["560202"].id, "priority": 0, "active": True})
    service.delete_rule(db, book.id, rule["id"])
    rows = service.list_rows(db, book.id, statement["id"])
    hit = [r for r in rows if r["summary"].startswith(keyword)]
    assert hit[0]["plan_status"] == PLAN_PENDING


# ----------------------------------------------------------------------
# 格式画像
# ----------------------------------------------------------------------

def test_记住格式之后同格式再传一次自动套用(db, book, admin_user, cash, store):
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    first = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                     data, cash.id)
    fingerprint = first["diagnosis"]["fingerprint"]
    # 手工指定一份（故意只给一半字段，模拟「自动没认出来」）
    service.save_profile(db, book.id, {
        "fingerprint": fingerprint,
        "column_mapping": {"DATE": 0, "TIME": 1, "IN": 2, "OUT": 3, "BALANCE": 4},
    })
    # 换个账户再传一次（守卫拦的是同账户同期间）
    other = add_sub_subject(db, book, "1002", "100203", "第三户", 3)
    second = service.upload_statement(db, book.id, admin_user, "招商9月-2.xlsx",
                                      data, other.id)
    assert second["diagnosis"]["column_mapping"]["DATE"] == 0
    assert second["row_count"] == first["row_count"]
    profiles = service.list_profiles(db, book.id)
    assert profiles and profiles[0]["hit_count"] >= 1


def test_画像按账套隔离(db, book, admin_user, cash, store):
    from app.models import Book, UserBook

    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    first = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                     data, cash.id)
    fingerprint = first["diagnosis"]["fingerprint"]
    service.save_profile(db, book.id, {"fingerprint": fingerprint,
                                        "column_mapping": {"DATE": 0}})
    other = Book(company_name="别家", name="别家账套", start_year=2026,
                 start_month=1, subject_code_rule="4-2-2-2", status="ACTIVE")
    db.add(other)
    db.commit()
    db.refresh(other)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()
    assert service.list_profiles(db, other.id) == []


# ----------------------------------------------------------------------
# 去重与制证
# ----------------------------------------------------------------------

def _prepare_row(db, book, statement, subjects, *, direction=DIRECTION_IN):
    """把一行弄成「可以制证」的状态：定方向 + 定对方科目。"""
    rows = service.list_rows(db, book.id, statement["id"])
    target = rows[0]
    service.set_row_direction(db, book.id, target["id"], direction)
    service.update_row(db, book.id, target["id"], {
        "plan_subject_id": subjects["560202"].id})
    return service.list_rows(db, book.id, statement["id"])[0]


def test_制证_一张流水一张凭证_借贷方向正确(db, book, admin_user, uploaded, subjects):
    row = _prepare_row(db, book, uploaded, subjects, direction=DIRECTION_IN)
    result = service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    assert result["created_count"] == 1, result
    voucher = db.get(Voucher, result["created"][0]["voucher_id"])
    assert voucher is not None
    entries = voucher.entries
    assert len(entries) == 2
    debit = next(e for e in entries if e.direction == "DEBIT")
    credit = next(e for e in entries if e.direction == "CREDIT")
    # **收钱：借 现金科目 / 贷 对方科目**
    assert debit.subject_id == uploaded["account_subject_id"]
    assert credit.subject_id == subjects["560202"].id
    assert debit.amount == credit.amount == Decimal(row["amount"])


def test_制证_支出方向反过来(db, book, admin_user, uploaded, subjects):
    row = _prepare_row(db, book, uploaded, subjects, direction=DIRECTION_OUT)
    service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    voucher = db.scalars(select(Voucher).order_by(Voucher.id.desc())).first()
    entries = voucher.entries
    debit = next(e for e in entries if e.direction == "DEBIT")
    credit = next(e for e in entries if e.direction == "CREDIT")
    # **付钱：借 对方科目 / 贷 现金科目**
    assert credit.subject_id == uploaded["account_subject_id"]
    assert debit.subject_id == subjects["560202"].id


def test_制证_source是IMPORT_备注点明来源(db, book, admin_user, uploaded, subjects):
    _prepare_row(db, book, uploaded, subjects)
    service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    voucher = db.scalars(select(Voucher).order_by(Voucher.id.desc())).first()
    assert voucher.source == "IMPORT"
    assert "流水对账" in voucher.remark
    assert voucher.attachments == 1, "对账单凭证要标「有一张附件」（那张对账单）"


def test_制证_凭证摘要用用途_没有用途才用摘要(db, book, admin_user, uploaded, subjects):
    """**用途优先，摘要兜底**（用户定的口径，2026-10-27）。

    对账单上「摘要」与「用途」装的不是一回事：**摘要是「怎么走的」，
    用途是「为什么」**。农商行那份就是「转入 / 办公室租金」——
    凭证摘要是给人看的，「办公室租金」比「转入」有用得多；而「转入」
    这种话在同一个月的几十张凭证里全都一样，翻凭证时认不出是哪笔。
    """
    rows = service.list_rows(db, book.id, uploaded["id"])
    first, second = rows[0], rows[1]
    # 第一行两列都有，第二行只有摘要
    for row, purpose in ((first, "办公室租金"), (second, "")):
        db_row = db.get(BankStatementRow, row["id"])
        db_row.summary = "转入"
        db_row.purpose = purpose
        db.flush()
        service.set_row_direction(db, book.id, row["id"], DIRECTION_IN)
        service.update_row(db, book.id, row["id"], {
            "plan_subject_id": subjects["560202"].id})

    service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    by_remark = {voucher.remark: voucher
                 for voucher in db.scalars(select(Voucher).order_by(Voucher.id)).all()}
    with_purpose = by_remark[f"流水对账 {uploaded['filename']}#{first['seq']}"]
    without_purpose = by_remark[f"流水对账 {uploaded['filename']}#{second['seq']}"]
    assert {entry.summary for entry in with_purpose.entries} == {"办公室租金"}, \
        [entry.summary for entry in with_purpose.entries]
    assert {entry.summary for entry in without_purpose.entries} == {"转入"}, \
        [entry.summary for entry in without_purpose.entries]


def test_制证_重复提交幂等(db, book, admin_user, uploaded, subjects):
    _prepare_row(db, book, uploaded, subjects)
    first = service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    second = service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    assert first["created_count"] == 1
    assert second["created_count"] == 0, "第二次又生成了一张凭证 —— 同一笔钱记了两次"


def test_制证_待确认的行不生成(db, book, admin_user, uploaded, subjects):
    """没定对方科目的行**不许**制证。

    它被跳过（而不是失败）—— 「待确认」是用户还没做完的事，
    不是错误。
    """
    rows = service.list_rows(db, book.id, uploaded["id"])
    service.set_row_direction(db, book.id, rows[0]["id"], DIRECTION_IN)
    plan = service.plan_vouchers(db, book.id, uploaded["id"])
    assert any(s["reason"] == "对方科目待确认" for s in plan["skipped"]), plan
    result = service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    assert result["created_count"] == 0


def test_制证_已入账的流水不再制证(db, book, admin_user, uploaded, subjects, make_voucher):
    """±7 天内、同一科目、同方向、同金额 → 判为已入账。"""
    row = _prepare_row(db, book, uploaded, subjects, direction=DIRECTION_IN)
    amount = Decimal(row["amount"])
    tx_date = row["tx_date"]
    make_voucher(tx_date,
                 dr=[(uploaded["account_subject_id"], amount, "已有凭证")],
                 cr=[(subjects["560301"].id, amount, "已有凭证")])
    plan = service.plan_vouchers(db, book.id, uploaded["id"])
    skipped = [s for s in plan["skipped"] if "已入账" in s["reason"]]
    assert skipped, f"没有把已入账的行标出来：{plan['skipped']}"
    result = service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    assert result["created_count"] == 0, "已入账的流水又生成了一张凭证"


def test_去重窗口外的同额凭证不算重复(db, book, admin_user, uploaded, subjects, make_voucher):
    """窗口是 ±7 天。超出窗口的同额凭证是**另一笔钱**，不能当成重复。

    **口径变了（2026-10-29）**：这条原来断言「超出窗口的同额凭证 → 这一行
    仍然是可以制证的候选」。现在超出的那一段被拆成两档：

    * **7~30 天** → **待复核**：金额对得上、日期对不上 —— 报出来让人判，
      但**不自动制证**（账上明明有那一笔，再制一张就是同一笔钱记两遍）；
    * **超过 30 天** → 就是普通的候选（真的是另一笔钱，不该打扰用户）。

    **这条用例原来的用意一个字没变**：窗口外的同额凭证**不许**被当成
    「已入账」。变的是「那它是什么」—— 从「静默地当候选」变成
    「报出来让人判」，而**保护其实更强了**（以前点一下「生成流水凭证」
    就会把同一笔钱记两遍，现在不会）。
    """
    from datetime import date, timedelta

    row = _prepare_row(db, book, uploaded, subjects, direction=DIRECTION_IN)
    amount = Decimal(row["amount"])
    tx_date = date.fromisoformat(row["tx_date"])
    far = tx_date + timedelta(days=10)
    make_voucher(far,
                 dr=[(uploaded["account_subject_id"], amount, "另一笔")],
                 cr=[(subjects["560301"].id, amount, "另一笔")])
    plan = service.plan_vouchers(db, book.id, uploaded["id"])
    assert not any("已入账" in s.get("reason", "") for s in plan["skipped"]), (
        "窗口外的同额凭证被误判成重复了 —— 那会让真正该记的第二笔永远进不了账")
    assert plan["review_count"] == 1, (
        "金额对得上、日期对不上 —— 应当报出来让人复核，而不是当成没这回事")
    # **待复核的行不在可制证清单里**：点一下「生成流水凭证」就会把
    # 同一笔钱记两遍。
    assert all(c["id"] != row["id"] for c in plan["candidates"]), plan["candidates"]


def test_差得太远的同额凭证_就是普通候选(db, book, admin_user, uploaded, subjects, make_voucher):
    """差 45 天 —— 真的是**另一笔钱**，不该报出来让人复核。"""
    from datetime import date, timedelta

    row = _prepare_row(db, book, uploaded, subjects, direction=DIRECTION_IN)
    amount = Decimal(row["amount"])
    tx_date = date.fromisoformat(row["tx_date"])
    far = tx_date + timedelta(days=45)
    make_voucher(far,
                 dr=[(uploaded["account_subject_id"], amount, "另一笔")],
                 cr=[(subjects["560301"].id, amount, "另一笔")])
    plan = service.plan_vouchers(db, book.id, uploaded["id"])
    assert plan["review_count"] == 0, plan
    assert plan["candidate_count"] >= 1, (
        "差 45 天的同额凭证被当成了同一笔 —— 那会让真正该记的第二笔永远进不了账")


def test_单行失败只回滚那一行(db, book, admin_user, uploaded, subjects, monkeypatch):
    """一行失败不许把整批都赔进去。"""
    rows = service.list_rows(db, book.id, uploaded["id"])
    # 两行都准备好
    for row in rows[:2]:
        service.set_row_direction(db, book.id, row["id"], DIRECTION_IN)
        service.update_row(db, book.id, row["id"], {
            "plan_subject_id": subjects["560202"].id})
    real = service.voucher_service.create_voucher
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("这一行故意失败")
        return real(*args, **kwargs)

    monkeypatch.setattr(service.voucher_service, "create_voucher", flaky)
    result = service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    assert result["created_count"] == 1, result
    assert len(result["failures"]) == 1
    assert "故意失败" in result["failures"][0]["reason"]


# ----------------------------------------------------------------------
# 期末余额对账
# ----------------------------------------------------------------------

def test_余额核对_差额为零(db, book, admin_user, uploaded, subjects, make_voucher):
    """对账单期末余额与账面一致时差额为 0。"""
    statement = db.get(BankStatement, uploaded["id"])
    statement.closing_balance = Decimal("1000.00")
    db.commit()
    rows = service.list_rows(db, book.id, uploaded["id"])
    make_voucher(rows[0]["tx_date"],
                 dr=[(statement.account_subject_id, Decimal("1000.00"), "期初")],
                 cr=[(subjects["560301"].id, Decimal("1000.00"), "期初")])
    result = service.reconcile_balance(db, book.id, uploaded["id"])
    assert result["available"] is True
    assert Decimal(result["difference"]) == Decimal("0")
    assert result["verdict"] == "MATCH"


def test_余额核对_差额被未入账流水解释(db, book, admin_user, uploaded, subjects, make_voucher):
    """账面比银行少一笔 —— 那笔就是还没入账的流水。

    差额的解释就是「本账户还没入账的流水净额」：银行那边已经变了、
    账上还没变的那部分。
    """
    statement = db.get(BankStatement, uploaded["id"])
    rows = service.list_rows(db, book.id, uploaded["id"])
    inflow = [r for r in rows if r["direction"] == "IN" and r["amount"]]
    if not inflow:
        pytest.skip("这份样例没有收入行")
    row = inflow[0]
    # 未入账流水的净额 = 收入 − 支出（与服务层的算法同口径）
    net = Decimal("0")
    for r in rows:
        if r["direction"] == "IN":
            net += Decimal(r["amount"])
        elif r["direction"] == "OUT":
            net -= Decimal(r["amount"])
    # 对账单期末余额比账面多 net —— 那多出来的正是还没入账的那些流水
    statement.closing_balance = net + Decimal("1000.00")
    db.commit()
    book_balance = Decimal("1000.00")
    make_voucher(row["tx_date"],
                 dr=[(statement.account_subject_id, book_balance, "调平")],
                 cr=[(subjects["560301"].id, book_balance, "调平")])
    result = service.reconcile_balance(db, book.id, uploaded["id"])
    assert result["available"] is True
    assert Decimal(result["difference"]) == net
    assert result["verdict"] == "EXPLAINED", result
    assert "未入账流水" in result["explanation"]


def test_余额核对_已提交发票覆盖的流水不算未入账(db, book, admin_user, uploaded,
                                             subjects, make_voucher):
    """发票已生成凭证后，流水上的发票认领不应继续制造未入账差额。"""
    statement = db.get(BankStatement, uploaded["id"])
    rows = service.list_rows(db, book.id, uploaded["id"])
    inflow = next(r for r in rows if r["direction"] == DIRECTION_IN and r["amount"])
    target = db.get(BankStatementRow, inflow["id"])
    for row in db.scalars(select(BankStatementRow).where(
            BankStatementRow.statement_id == statement.id,
            BankStatementRow.id != target.id)).all():
        row.status = STATUS_IGNORED

    statement.closing_balance = Decimal("1000.00")
    make_voucher(inflow["tx_date"],
                 dr=[(statement.account_subject_id, Decimal("1000.00"), "调平")],
                 cr=[(subjects["560301"].id, Decimal("1000.00"), "调平")])
    batch = InvoiceBatch(book_id=book.id, filename="已提交.pdf", file_path="",
                         status="COMMITTED", total=1, created_by="tester")
    db.add(batch)
    db.flush()
    record = InvoiceRecord(
        batch_id=batch.id, book_id=book.id, page_index=0,
        invoice_type="OUTPUT", status="COMMITTED", payload={})
    db.add(record)
    db.flush()
    target.matched_invoice_record_id = record.id
    db.commit()

    result = service.reconcile_balance(db, book.id, uploaded["id"])
    assert result["difference"] == "0.00"
    assert Decimal(result["unposted_net"]) == Decimal("0")
    assert result["unposted_count"] == 0
    assert result["verdict"] == "MATCH"


def test_余额核对_没有余额列时不报红(db, book, admin_user, cash, store):
    """对账单没给余额列 —— 这是**事实陈述**，不是错误。"""
    data = (FIXTURES / "pingan.xls").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "平安9月.xls",
                                         data, cash.id)
    result = service.reconcile_balance(db, book.id, statement["id"])
    if statement["closing_balance"]:
        assert result["available"] is True
    else:
        assert result["available"] is False
        assert "余额" in result["reason"]


# ----------------------------------------------------------------------
# 删除守卫
# ----------------------------------------------------------------------

def test_已生成凭证的对账单不许删(db, book, admin_user, uploaded, subjects):
    _prepare_row(db, book, uploaded, subjects)
    service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    with pytest.raises(ValueError) as excinfo:
        service.delete_statement(db, book.id, uploaded["id"])
    assert "凭证" in str(excinfo.value)


# ----------------------------------------------------------------------
# 重新解析
# ----------------------------------------------------------------------

def test_重新解析_不会被自己挡住(db, book, admin_user, cash, store):
    """重解析的那一份当然「已经有一份」—— 守卫必须把它自己排掉。

    不排掉的话，重新解析永远报「已经有一份对账单了」，
    而那条提示指向的还是它自己 —— 用户完全看不懂。
    """
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                         data, cash.id)
    again = service.reparse_statement(db, book.id, statement["id"])
    assert again["row_count"] == statement["row_count"]
    assert again["id"] == statement["id"]


def test_重新解析_按人工指定的列(db, book, admin_user, cash, store):
    """自动映射认不出来时，用户手工指定一次，这里照着重跑。"""
    data = (FIXTURES / "cmb_csv.xlsx").read_bytes()
    statement = service.upload_statement(db, book.id, admin_user, "招商9月.xlsx",
                                         data, cash.id)
    header = statement["diagnosis"]["header_cells"]
    mapping = {"DATE": header.index("交易日期"), "IN": header.index("收入"),
               "OUT": header.index("支出"), "BALANCE": header.index("余额")}
    again = service.reparse_statement(db, book.id, statement["id"],
                                      column_mapping=mapping)
    assert again["diagnosis"]["column_mapping"] == mapping
    assert again["row_count"] == statement["row_count"]


def test_重新解析_已生成凭证的行把关联对回去(db, book, admin_user, uploaded, subjects):
    """重建行会丢掉与凭证的关联 —— 所以那些行要**对回去**。

    对不回去就整次拒绝（不能硬挂：挂错了等于把甲流水的金额记到乙头上）。
    """
    _prepare_row(db, book, uploaded, subjects)
    service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    before = db.scalars(
        select(BankStatementRow).where(
            BankStatementRow.statement_id == uploaded["id"],
            BankStatementRow.matched_voucher_id.is_not(None))).all()
    assert before, "前置条件：有行已经生成过凭证"

    result = service.reparse_statement(db, book.id, uploaded["id"])
    after = db.scalars(
        select(BankStatementRow).where(
            BankStatementRow.statement_id == uploaded["id"],
            BankStatementRow.matched_voucher_id.is_not(None))).all()
    assert len(after) == len(before), "凭证的关联丢了 —— 那张凭证就成了「来路不明」"
    assert ({row.matched_voucher_id for row in after}
            == {row.matched_voucher_id for row in before})
    assert result["row_count"] == uploaded["row_count"]


def test_重新解析_对不回去就整次拒绝(db, book, admin_user, uploaded, subjects):
    """**宁可不做，也不能把凭证挂到别的行上。**

    那一行被人改过（金额与文件里对不上），于是按「日期 + 金额 + 方向」
    对不回去 —— 这时候要**整次拒绝**，并说清为什么。
    """
    _prepare_row(db, book, uploaded, subjects)
    service.commit_vouchers(db, book.id, uploaded["id"], admin_user)
    vouchered = db.scalars(
        select(BankStatementRow).where(
            BankStatementRow.statement_id == uploaded["id"],
            BankStatementRow.matched_voucher_id.is_not(None))).first()
    assert vouchered is not None
    vouchered.amount = Decimal("999999.99")      # 改坏它，让它对不回去
    db.flush()

    with pytest.raises(ValueError) as excinfo:
        service.reparse_statement(db, book.id, uploaded["id"])
    message = str(excinfo.value)
    assert "凭证" in message, message
    assert "对不回去" in message, message
    # 拒绝之后**什么都没变**：原来的关联还在
    still = db.scalars(
        select(BankStatementRow).where(
            BankStatementRow.statement_id == uploaded["id"],
            BankStatementRow.matched_voucher_id.is_not(None))).all()
    assert len(still) == 1


def test_行数超限报错(db, book, admin_user, cash, store):
    """超行数**报错**，不截断 —— 截断出来的对账单余额对不上，而用户看不出少了尾巴。"""
    lines = ["交易日期,交易时间,收入,支出,余额,交易类型,交易备注"]
    for day in range(1, 20100):
        lines.append(f"2026-09-01,10:00:00,1.00,0.0,1.00,行内转账,第{day}笔")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    with pytest.raises(ValueError) as excinfo:
        service.upload_statement(db, book.id, admin_user, "超大.csv", data, cash.id)
    assert "行" in str(excinfo.value)


# ----------------------------------------------------------------------
# 扫描件（没有文本层的 PDF）
# ----------------------------------------------------------------------

def _blank_pdf() -> bytes:
    """一份**没有文本层**的 PDF（只有一页空白）。"""
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


def test_扫描件PDF_没配OCR时明确报错且保留原件(db, book, admin_user, cash, store):
    """扫描件对账单没有文本层。没配 OCR 就**明确说清楚**，而不是「解析失败」。

    **原件必须留下**：用户至少能把它下载下来手工处理 ——
    「原件没了」是最没用的报错。
    """
    with pytest.raises(ValueError) as excinfo:
        service.upload_statement(db, book.id, admin_user, "扫描件.pdf",
                                 _blank_pdf(), cash.id)
    message = str(excinfo.value)
    assert "没有文本层" in message, message
    assert "OCR" in message, message
    assert "原件已保留" in message, message

    statements = service.list_statements(db, book.id)
    assert len(statements) == 1
    assert statements[0]["parse_status"] == "FAILED"
    assert "没有文本层" in statements[0]["parse_message"]
    # 原件确实在磁盘上
    from app.bankstatement import storage
    assert storage.exists(
        db.query(BankStatement).filter(BankStatement.id == statements[0]["id"]).one().file_path)


def test_文本流兜底层能切出表格(db, book, admin_user, cash, store):
    """第二层（纯文本流 + 锚点）至少要能切出表头。

    它比坐标那层弱，是兜底 —— 但「兜底完全不能用」等于没有兜底。
    """
    from app.bankstatement import pdf_table

    data = (FIXTURES / "cib_pdf.pdf").read_bytes()
    grid = pdf_table.extract_grid_from_text(data)
    assert grid is not None
    assert grid.rows, "纯文本流那一层切不出任何行"
    assert any(grid.rows[0]), grid.rows[0]


def test_有文本层的PDF_不走OCR(db, book, admin_user, cash, store):
    """有文本层却解析不出来是版面认错，OCR 同样认不对 —— 别白花一次钱。"""
    from app.bankstatement import pdf_table

    assert pdf_table.has_text_layer((FIXTURES / "cib_pdf.pdf").read_bytes())
    assert not pdf_table.has_text_layer(_blank_pdf())
