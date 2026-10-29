"""宿主新增的「对账单」能力：**口径只有一份**的机器证明。

这组用例盯的是共享卡对账插件依赖的那几条宿主能力：

  · ``ctx.statement.parse`` —— 用宿主解析器把文件解析成结构化流水行，
    **不写宿主任何业务表**；
  · ``ctx.statement.read`` —— 读该账套在宿主已绑的对账单**清单**（不含行内容）；
  · ``ctx.period_list`` —— 账套的会计期间清单；
  · ``ctx.ledger_read(include_unposted=True)`` —— 与宿主对账口径一致；
  · ``ctx.data.insert_many`` —— 整批同一事务。

**为什么「口径只有一份」要靠机器证明**：共享卡的对账单**不属于任何账套**，
宿主 ``bank_statement`` 表里没有它的行，能复用的只有**解析器**。插件要是
自己再写一份解析器，同一份文件就会有两个答案 —— 而这两个答案不会同时错，
它们会**各自对上一半**（列映射认得不一样、方向判得不一样），用户看到的是
「有时候对有时候不对」。所以这里用 14 份真实银行样例逐行对比：宿主自己的
解析结果与插件面拿到的结果，必须**完全一致**。
"""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.bankstatement import service
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import BankStatement, BankStatementRow, Subject
from app.models.book import Period
from app.plugins.manifest import table_prefix
from tests.plugin_fixtures import plugin_env
from tests.plugin_helpers import package

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "statements"
OCR_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "ocr"
GOLDENS = json.loads((FIXTURES / "goldens.json").read_text(encoding="utf-8"))

#: 全部真实样例。PDF 那几份在 Linux 上要靠 OCR 兜底（见
#: ``tests/test_bank_statement_parser.py`` 的说明），所以对照时把 OCR 夹具
#: 接上 —— **测的始终是生产那条路**。
SAMPLES = sorted(GOLDENS.keys())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "statement_storage_dir", str(tmp_path / "statements"))
    return tmp_path / "statements"


@pytest.fixture
def cash(db, book):
    """一个 1002 银行存款的末级科目（宿主流水对账的绑定对象）。"""
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


def _data(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _shape(parsed) -> dict:
    """解析结果 → 可比较的形状（金额统一成字符串，避免 Decimal/float 混比）。"""
    return {
        "container": parsed.container,
        "fingerprint": parsed.fingerprint,
        "column_mapping": dict(parsed.column_mapping),
        "diagnosis": list(parsed.diagnosis),
        "meta": {
            "account_no": parsed.meta.account_no,
            "bank_name": parsed.meta.bank_name,
            "closing_balance": str(parsed.meta.closing_balance)
            if parsed.meta.closing_balance is not None else None,
            "declared_income_count": parsed.meta.declared_income_count,
            "declared_total_count": parsed.meta.declared_total_count,
        },
        "rows": [{
            "seq": row.seq,
            "tx_date": row.tx_date.isoformat() if row.tx_date else "",
            "tx_time": row.tx_time,
            "direction": row.direction,
            "amount": str(row.amount) if row.amount is not None else None,
            "balance": str(row.balance) if row.balance is not None else None,
            "summary": row.summary,
            "purpose": row.purpose,
            "counterparty_name": row.counterparty_name,
            "counterparty_account": row.counterparty_account,
        } for row in parsed.rows],
    }


# ---------------------------------------------------------------------------
# 1. 口径只有一份：插件面与宿主面逐行一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SAMPLES)
def test_parse_only与宿主自己的解析逐行一致(db, name):
    """插件面拿到的解析结果，必须与宿主自己那条路**完全一样**。

    对照的不是「另一份实现」，而是宿主**生产用的那一条**
    （``_parse_with_profile``）—— 插件面只是把同一个结果递出去。
    """
    data = _data(name)
    with pytest.MonkeyPatch.context() as patcher:
        fixture = OCR_FIXTURES / f"{pathlib.Path(name).stem}_statement_ocr_markdown.md"
        if fixture.exists():
            grid = service._grid_from_ocr_markdown(
                [fixture.read_text(encoding="utf-8")])
            patcher.setattr(service, "_ocr_grid", lambda db, data: grid)
        host_parsed, _profile = service._parse_with_profile(db, 1, data)
        plugin_parsed = service.parse_only(db, data)
    assert _shape(plugin_parsed) == _shape(host_parsed)


@pytest.mark.parametrize("name", [n for n in SAMPLES if not n.endswith(".pdf")])
def test_parse_only的结果对得上金标准(db, name):
    """对着金标准核：行数、收支合计、期末余额、首末行。

    这条不依赖宿主那条路 —— 它核的是**解析本身**。两份断言一起看才够：
    「与宿主一致」防分叉，「与金标准一致」防两边一起错。
    """
    golden = GOLDENS[name]
    parsed = service.parse_only(db, _data(name))
    assert len(parsed.rows) == golden["row_count"]
    assert str(parsed.income_total) == golden["income_total"]
    assert str(parsed.expense_total) == golden["expense_total"]
    assert parsed.container == golden["container"]
    assert parsed.header_row == golden["header_row"]
    assert dict(parsed.column_mapping) == golden["column_mapping"]
    first = parsed.rows[0]
    assert {"tx_date": first.tx_date.isoformat(), "direction": first.direction,
            "amount": str(first.amount), "balance": str(first.balance)} == golden["first"]
    last = parsed.rows[-1]
    assert {"tx_date": last.tx_date.isoformat(), "direction": last.direction,
            "amount": str(last.amount), "balance": str(last.balance)} == golden["last"]


def test_parse_only不写任何宿主业务表(db, book, cash):
    """解析是**只读**的：宿主的对账单台账与流水行一行都不能多。

    这条是「共享卡是插件实体、宿主只贡献解析器」的地基 —— 宿主那几张表
    一旦被写进去，用户就会在宿主流水对账页面上看到这份对账单，而它其实
    属于一张跨账套共用的卡（那会让余额对账报红、其余账套满屏假存疑凭证）。
    """
    before = _statement_counts(db)
    service.parse_only(db, _data("ccb.xls"))
    assert _statement_counts(db) == before


def _statement_counts(db) -> dict:
    return {
        "statement": db.scalar(select(func.count()).select_from(BankStatement)) or 0,
        "row": db.scalar(select(func.count()).select_from(BankStatementRow)) or 0,
    }


def test_parse_only带列映射时整体替换自动映射(db):
    """人工指定的映射要**真的生效**，且是整体替换（不是逐字段合并）。

    判据：把 DATE 指到摘要那一列，日期就应当全认不出来 —— 若结果与自动映射
    一模一样，说明映射被当成了摆设。
    """
    data = _data("ccb.xls")
    golden = GOLDENS["ccb.xls"]["column_mapping"]
    summary_at = golden["SUMMARY"]
    swapped = dict(golden)
    swapped["DATE"] = summary_at
    parsed = service.parse_only(db, data, column_mapping=swapped)
    assert dict(parsed.column_mapping) == swapped
    assert all(row.tx_date is None for row in parsed.rows), "映射没生效"


def test_parse_only用金标准的映射时结果与自动映射一致(db):
    """给了正确映射 → 结果必须与自动映射**一样**（映射是替换，不是叠加）。"""
    data = _data("ccb.xls")
    golden = GOLDENS["ccb.xls"]["column_mapping"]
    auto = service.parse_only(db, data)
    mapped = service.parse_only(db, data, column_mapping=golden)
    assert _shape(mapped) == _shape(auto)


def test_parse_only的行数超上限被拒(db, monkeypatch):
    """超上限要报错，不是截断 —— 截断出来的对账单是「少了一半的钱」。"""
    monkeypatch.setattr(service, "MAX_STATEMENT_ROWS", 1)
    with pytest.raises(ValueError, match="超过上限"):
        service.parse_only(db, _data("ccb.xls"))


def test_parse_only不查也不写宿主的格式画像(db, book):
    """共享卡跨账套，宿主那份画像按账套隔离，用不上 —— 映射由插件自己存。

    这里断言的是「插件面解析**不会**在宿主留下画像行」。
    """
    from app.models.bankstatement import BankStatementProfile

    before = db.scalar(select(func.count()).select_from(BankStatementProfile)) or 0
    service.parse_only(db, _data("ccb.xls"))
    after = db.scalar(select(func.count()).select_from(BankStatementProfile)) or 0
    assert after == before


# ---------------------------------------------------------------------------
# 2. ctx 能力面（走插件的真实路径：安装 → HTTP → 宿主）
# ---------------------------------------------------------------------------

def _install_statement_plugin(plugin_env, client, auth, admin_user):
    """装一个把新能力暴露成路由的测试插件。"""
    build, restart = plugin_env
    pid = "com.test.stmtcap"
    table = table_prefix(pid) + "items"
    code = f'''from fastapi import File

def register(registrar, ctx):
    def parse(book_id: int, file=File(...)):
        payload = ctx.upload_read(file)
        out = ctx.statement.parse(payload["content"], book_id=book_id)
        return {{"rows": len(out["rows"]), "container": out["container"],
                "fingerprint": out["fingerprint"], "diagnosis": out["diagnosis"],
                "via_ocr": out["via_ocr"], "period_start": out["period_start"],
                "period_end": out["period_end"], "file": out["file"],
                "income_total": str(out["income_total"]),
                "expense_total": str(out["expense_total"]),
                "closing_balance": str(out["meta"]["closing_balance"]),
                "first": out["rows"][0] if out["rows"] else None}}

    def parse_no_book(file=File(...)):
        payload = ctx.upload_read(file)
        out = ctx.statement.parse(payload["content"])
        return {{"rows": len(out["rows"]), "file": out["file"]}}

    def read(book_id: int):
        return ctx.statement.read(book_id)

    def periods(book_id: int):
        return ctx.period_list(book_id)

    def ledger(book_id: int, period_id: int):
        return ctx.ledger_read(book_id, period_id, include_unposted=True)

    def insert_many(book_id: int, payload: dict):
        return ctx.data.insert_many("{table}", payload["rows"], book_id=book_id)

    def rows(book_id: int):
        return ctx.data.read("{table}", book_id=book_id)

    registrar.post("/parse", parse)
    registrar.post("/parse-no-book", parse_no_book)
    registrar.get("/read", read)
    registrar.get("/periods", periods)
    registrar.get("/ledger", ledger)
    registrar.post("/insert-many", insert_many)
    registrar.get("/rows", rows)
'''.encode()
    raw, manifest, _keys, _private = build(
        plugin_id=pid,
        tables=True,
        files={"backend/main.py": code,
               "frontend/index.js": b'export default host => ({render: () => host.h("div")});'},
        edit=lambda m: m.update(permissions=["book:read", "ledger:read", "upload:read",
                                             "statement:parse", "statement:read",
                                             "plugin_data:rw"]))
    assert client.post("/api/system/plugins/install",
                       files={"file": ("stmtcap.cwp", raw)}, headers=auth).status_code == 200
    # 插件授权是**显式**的（manifest 里的 assignment 只是给管理页的默认值），
    # 不授权就一律回「插件不存在或不可用」。
    assert client.put(f"/api/system/plugins/{pid}/users", headers=auth,
                      json={"user_ids": [admin_user.id]}).status_code == 200
    restart()
    return pid, table


def test_插件面解析对账单并留存原件(plugin_env, client, auth, admin_user, book, store):
    """插件面解析要能拿到行、期间、合计，且**原件落盘**。

    原件必须留：解析器会改、映射会更新，早期的解析结果和后来的不一样；
    没有原件，用户抱怨「这笔钱认错了」时只能让他重新去网银导一遍。
    """
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    data = _data("ccb.xls")
    r = client.post(path + "/parse", params={"book_id": book.id}, headers=auth,
                    files={"file": ("ccb.xls", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    body = r.json()
    golden = GOLDENS["ccb.xls"]
    assert body["rows"] == golden["row_count"]
    assert body["income_total"] == golden["income_total"]
    assert body["expense_total"] == golden["expense_total"]
    assert body["period_start"] == golden["period_start"]
    assert body["period_end"] == golden["period_end"]
    assert body["container"] == golden["container"]
    assert body["diagnosis"] == golden["diagnosis"]
    # 原件落在 shared/ —— 它不属于任何账套。
    assert body["file"]["relative_path"].startswith("shared/")
    assert (store / body["file"]["relative_path"]).exists()


def test_不给book_id时只解析不落原件(plugin_env, client, auth, admin_user, book):
    """``book_id`` 缺省 = 只想看看解析结果，不留原件。"""
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    data = _data("ccb.xls")
    r = client.post(path + "/parse-no-book", headers=auth,
                    files={"file": ("ccb.xls", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["file"]["relative_path"] == ""


def test_statement_read只给清单不给行内容(plugin_env, client, auth, admin_user, db, book, cash, store):
    """清单要能看出「这张卡是不是已经在这个账套绑过」—— 那会导致双重记账。

    但**不能给行内容**：行的归属各算各的，混在一起说不清哪一行归谁。
    """
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    assert client.get(path + "/read", params={"book_id": book.id},
                      headers=auth).json() == []
    service.upload_statement(db, book.id, None, "ccb.xls", _data("ccb.xls"), cash.id)
    db.commit()
    listed = client.get(path + "/read", params={"book_id": book.id}, headers=auth).json()
    assert len(listed) == 1
    item = listed[0]
    assert item["account_subject_code"] == "100201"
    assert item["row_count"] == GOLDENS["ccb.xls"]["row_count"]
    assert item["period_start"] == GOLDENS["ccb.xls"]["period_start"]
    # 清单里**没有**行数据。
    assert "rows" not in item and "extra" not in item


def test_statement_read不越权(plugin_env, client, auth, admin_user, plain_auth, plain_user, db, book):
    """别人的账套读不到 —— 与 ``book:read`` 同一道闸。"""
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    client.put(f"/api/system/plugins/{pid}/users", headers=auth,
               json={"user_ids": [plain_user.id]}).raise_for_status()
    path = f"/api/plugins/{pid}"
    r = client.get(path + "/read", params={"book_id": book.id}, headers=plain_auth)
    assert r.status_code == 403


def test_period_list与宿主期间表一致(plugin_env, client, auth, admin_user, db, book):
    """插件要能拿到各账套的期间，才能算「取哪一期的账面余额」。

    跨账套的账期**可以不一样**（有的账套用 4-4-5 财年），所以不能拿宿主
    那份对账单的期间号直接套到别的账套上。
    """
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    listed = client.get(path + "/periods", params={"book_id": book.id}, headers=auth).json()
    rows = db.scalars(select(Period).where(
        Period.book_id == book.id).order_by(Period.ord)).all()
    assert len(listed) == len(rows)
    assert [item["id"] for item in listed] == [row.id for row in rows]
    assert [item["month"] for item in listed] == [row.month for row in rows]
    assert all(item["ord"] == row.ord for item, row in zip(listed, rows))


def test_ledger_read的include_unposted与账簿引擎一致(plugin_env, client, auth, admin_user, db, book):
    """插件余额核对要与宿主对账口径**同一个**。

    宿主 ``reconcile_balance`` 用的是 ``LedgerEngine.for_ledger(...,
    include_unposted=True)``；插件若用 ``include_unposted=False``，差额里
    会混着「已制证未过账」的噪声，结论直接偏掉。

    **余额表的「方向」字段是中文**：``借`` / ``贷`` / ``平``（宿主
    ``ledger.engine.display_side`` 的口径）。插件跨账套相加时必须按它还原
    **符号** —— 落在贷方的那张账套若以正数加进去，合计凭空多出一截。

    **两个方向都要覆盖**：只测借方的话，「按方向还原符号」那一行永远走不到，
    判据看着是绿的，实则什么都没验过（这条断言踩过一次：账上没凭证、余额 0、
    方向「平」，符号还原的分支根本没执行，测试照样通过）。
    """
    from app.ledger.engine import LedgerEngine
    from tests.conftest import build_voucher_factory

    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    period = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.month == 9))

    def _leaf(parent_code: str, code: str, name: str) -> Subject:
        parent = db.scalar(select(Subject).where(
            Subject.book_id == book.id, Subject.year == book.start_year,
            Subject.code == parent_code))
        parent.is_leaf = False
        db.flush()
        sub = Subject(book_id=book.id, year=book.start_year, code=code, name=name,
                      parent_id=parent.id, level=parent.level + 1, category=parent.category,
                      direction=parent.direction, is_leaf=True, carry_forward=False,
                      sort_no=1, active=True)
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub

    # 一个落在**借方**的科目（银行存款）+ 一个落在**贷方**的科目（其他应付款）。
    debit_side = _leaf("1002", "100201", "基本户")
    credit_side = _leaf("2202", "224101", "其他应付款-往来款")
    maker = build_voucher_factory(db, book, admin_user)
    maker("2026-09-15",
          dr=[(debit_side.code, "8888.00", "存一笔")],
          cr=[(credit_side.code, "8888.00", "存一笔")])

    body = client.get(path + "/ledger", params={"book_id": book.id, "period_id": period.id},
                      headers=auth).json()
    assert body["include_unposted"] is True
    engine = LedgerEngine.for_ledger(db, book.id, include_unposted=True)
    rows = {item["subject_id"]: item for item in body["rows"]}

    # 借方余额：符号为正。
    row = rows[debit_side.id]
    assert row["direction"] == "借", row
    assert Decimal(str(row["closing"])) == Decimal("8888.00"), row
    assert Decimal(str(engine.closing(debit_side.id, period.ord))) == Decimal("8888.00")

    # 贷方余额：表里给的是**绝对额**，符号在方向里 —— 还原后必须是负数。
    row = rows[credit_side.id]
    assert row["direction"] == "贷", row
    assert Decimal(str(row["closing"])) == Decimal("8888.00"), row
    signed = Decimal(str(row["closing"]))
    if row["direction"] == "贷":
        signed = -signed
    assert signed == Decimal(str(engine.closing(credit_side.id, period.ord)))
    assert signed == Decimal("-8888.00")


def test_insert_many整批同一事务(plugin_env, client, auth, admin_user, book):
    """第 N 行非法 → **整批回滚**，一行都不留。

    逐行插入的老路会留下「前 N−1 行已经落库」的半批数据：用户看到的是
    「插了一半」，而他以为这是一次操作。
    """
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    r = client.post(path + "/insert-many", params={"book_id": book.id}, headers=auth,
                    json={"rows": [{"label": "一"}, {"label": "二"}]})
    assert r.status_code == 200, r.text
    inserted = r.json()
    assert len(inserted) == 2
    assert {item["label"] for item in inserted} == {"一", "二"}
    assert all(item["book_id"] == book.id for item in inserted)
    assert all(item["id"] for item in inserted)
    rows = client.get(path + "/rows", params={"book_id": book.id}, headers=auth).json()
    assert len(rows) == 2

    bad = client.post(path + "/insert-many", params={"book_id": book.id}, headers=auth,
                      json={"rows": [{"label": "三"}, {"nope": 1}]})
    assert bad.status_code == 400
    assert "第 2 行" in bad.json()["detail"]
    rows = client.get(path + "/rows", params={"book_id": book.id}, headers=auth).json()
    assert len(rows) == 2, "非法行让整批回滚失效了"


def test_insert_many空批被拒(plugin_env, client, auth, admin_user, book):
    """空批不是「成功插了 0 行」，而是参数不合法。"""
    pid, _table = _install_statement_plugin(plugin_env, client, auth, admin_user)
    path = f"/api/plugins/{pid}"
    r = client.post(path + "/insert-many", params={"book_id": book.id}, headers=auth,
                    json={"rows": []})
    assert r.status_code == 400
