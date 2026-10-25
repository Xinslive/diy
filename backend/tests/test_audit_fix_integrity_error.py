"""审计修复 §3.20 与 §3.14/§3.22：完整性违例要**认约束**，模型要**同名同义**。

**§3.20 —— 异常分类。**

``api/vouchers.py`` 的更新接口原来是一个**裸** ``except IntegrityError``：
**所有**完整性违例一律被翻成「凭证号 X 刚被另一张凭证占用，请再保存一次」。

而库层护栏 ``fn_entry_subject_year_guard`` 抛的是 **23514 CheckViolation**，
说的是完全另一回事（「分录的科目属于 2026 年度，不能记在 2027 年度的凭证上」）。
那句话**本来就该给用户看**，却被换成了一句让人去重试一个
**永远不可能成功**的操作 —— 真正的病因一次也没出现在界面上。

外键违例、非空违例同理：把它们说成「凭证号被占用」是**假话**，
用户会照着去改一个根本没问题的字段。

修法：按**约束名**区分（``util.constraint_name``，与 ``accounting/vouchers.py``、
``accounting/organize.py`` 同一份口径）—— 只有**唯一约束**冲突才翻成
「凭证号被占用」；其余的原样抛回全局处理器（``app/main.py`` 的
``dbapi_error_handler``）：我们自己写的中文护栏原文照实透出，
PG 自带的英文原文换成脱敏文案 + 请求号。

**§3.14/§3.22 —— 模型侧补齐库级 CHECK 约束。**

``app/models/accounting.py:96-99`` 自己立了规矩：「库级 CHECK 约束，
模型侧必须同名同义（审计 P3-2）」。而 Alembic 1.20 的 ``compare_metadata``
**根本不比较 CHECK 约束** —— 缺了不会报、补了也不会报，两条路都看不见。
所以这条规矩只能靠人工守着。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.api import vouchers as api_vouchers
from app.models import Entry, Voucher


def _payload(subjects, dr, cr, when="2026-01-05"):
    entries = [{"subject_id": subjects[c].id, "direction": "DEBIT",
                "amount": str(a), "summary": s} for c, a, s in dr]
    entries += [{"subject_id": subjects[c].id, "direction": "CREDIT",
                 "amount": str(a), "summary": s} for c, a, s in cr]
    return {"voucher_date": when, "entries": entries, "remark": "测试"}


def _db_constraints(db, table: str) -> dict[str, str]:
    rows = db.execute(text("""
        SELECT conname, pg_get_constraintdef(oid)
          FROM pg_constraint
         WHERE conrelid = to_regclass(:t) AND contype IN ('c', 'u', 'f')
    """), {"t": table}).all()
    return {r[0]: r[1] for r in rows}


def _orm_constraints(table) -> dict[str, str]:
    """模型侧声明的、**带名字**的约束 → 它的定义文本。

    ``CheckConstraint`` 有 ``sqltext``，``UniqueConstraint`` 没有
    （它就是一组列名）—— 两种都要能取到文本。
    """
    out: dict[str, str] = {}
    for c in table.__table__.constraints:
        if not c.name:
            continue
        text_ = getattr(c, "sqltext", None)
        if text_ is None:
            text_ = ", ".join(col.name for col in c.columns)
        out[c.name] = str(text_)
    return out


# ----------------------------------------------------------------- §3.20 异常分类


def test_库层护栏的违例不会被说成凭证号被占用(
        client, auth, book, subjects, make_voucher, monkeypatch):
    """23514 CheckViolation 不许被翻成「凭证号被占用」。

    这里用**真的**库层违例（把一条分录的金额改成 0，撞
    ``ck_entry_amount_nonzero``），而不是伪造一个异常 ——
    伪造的异常测不到「异常是怎么分类的」这件事。
    """
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    def boom(db, voucher, user, payload, **kw):
        db.execute(text("UPDATE entry SET amount = 0 WHERE voucher_id = :v"),
                   {"v": voucher.id})
        db.flush()

    monkeypatch.setattr(api_vouchers.svc, "update_voucher", boom)
    r = client.put(f"/api/books/{book.id}/vouchers/{v.id}", headers=auth,
                   json=_payload(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    assert r.status_code == 400, f"HTTP {r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "刚被另一张凭证占用" not in detail, (
        f"库层护栏的违例被说成了「凭证号被占用」：{detail}")
    assert "凭证号" not in detail, detail


def test_凭证号撞车仍然说凭证号被占用(
        client, auth, book, subjects, make_voucher, monkeypatch):
    """**唯一约束**冲突才翻成「凭证号被占用」——这条口径不许被改掉。"""
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    def boom(db, voucher, user, payload, **kw):
        # 同一个 (period_id, voucher_no) 再插一张 → 撞 uq_voucher_period_no
        db.execute(text("""
            INSERT INTO voucher (book_id, period_id, voucher_no, voucher_date,
                                 status, source, attachments, remark,
                                 created_at, updated_at)
            VALUES (:b, :p, :no, DATE '2026-01-05', 'UNPOSTED', 'MANUAL', 0, '',
                    now(), now())
        """), {"b": voucher.book_id, "p": voucher.period_id, "no": voucher.voucher_no})
        db.flush()

    monkeypatch.setattr(api_vouchers.svc, "update_voucher", boom)
    r = client.put(f"/api/books/{book.id}/vouchers/{v.id}", headers=auth,
                   json=_payload(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    assert r.status_code == 422, f"HTTP {r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "刚被另一张凭证占用" in detail, detail
    assert "请再保存一次" in detail, detail


def test_自己写的中文护栏原文会透给用户(client, auth, book, subjects, make_voucher,
                                    monkeypatch):
    """23514 且文案是**我们自己写的中文**时 → 原文透出（这是全局处理器的口径）。

    这里造的是「已结账期间禁写」那一条（``fn_entry_lock_guard``）：
    先把本期标成已结账，再改分录 → 「会计期间已结账，禁止修改凭证」。
    """
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    def boom(db, voucher, user, payload, **kw):
        db.execute(text("UPDATE period SET locked = true WHERE id = :p"),
                   {"p": voucher.period_id})
        db.execute(text("UPDATE entry SET amount = 1 WHERE voucher_id = :v"),
                   {"v": voucher.id})
        db.flush()

    monkeypatch.setattr(api_vouchers.svc, "update_voucher", boom)
    r = client.put(f"/api/books/{book.id}/vouchers/{v.id}", headers=auth,
                   json=_payload(subjects, [("1001", 100, "x")], [("5001", 100, "x")]))
    assert r.status_code == 400, f"HTTP {r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "刚被另一张凭证占用" not in detail, detail
    # 我们自己写的中文护栏原文要**照实透出**（那正是给用户看的提示）
    assert "已结账" in detail, detail


# ------------------------------------------------- §3.14/§3.22 模型 == 库（同名同义）


def test_模型侧声明了ck_voucher_source(db):
    """``ck_voucher_source`` 库里是活的，模型侧也要**同名同义**地声明。"""
    db_defs = _db_constraints(db, "voucher")
    assert "ck_voucher_source" in db_defs, db_defs
    orm_defs = _orm_constraints(Voucher)
    assert "ck_voucher_source" in orm_defs, orm_defs
    # 语义一致：四个取值一个不多一个不少
    for value in ("MANUAL", "IMPORT", "INVOICE", "CARRY_FORWARD"):
        assert value in db_defs["ck_voucher_source"], db_defs["ck_voucher_source"]
        assert value in orm_defs["ck_voucher_source"], orm_defs["ck_voucher_source"]


def test_模型侧声明了entry的行号唯一约束(db):
    """``uq_entry_voucher_line_no``（迁移 0038）：模型与库同名。"""
    db_defs = _db_constraints(db, "entry")
    assert "uq_entry_voucher_line_no" in db_defs, db_defs
    assert "voucher_id" in db_defs["uq_entry_voucher_line_no"]
    assert "line_no" in db_defs["uq_entry_voucher_line_no"]
    orm_defs = _orm_constraints(Entry)
    assert "uq_entry_voucher_line_no" in orm_defs, orm_defs


def test_模型侧的每一条命名约束库里都有(db):
    """模型声明的、**带名字**的约束，库里必须同名存在（不许有「只有模型有」的）。

    这条判据对 CHECK 约束**结构性失效**（Alembic 不比较它们），
    所以只能靠这种「两边对一遍名字」的方式守着。
    """
    for table, name in ((Voucher, "voucher"), (Entry, "entry")):
        db_defs = _db_constraints(db, name)
        for cname in _orm_constraints(table):
            assert cname in db_defs, (
                f"{name}.{cname} 模型侧有、库里没有 —— "
                "换用会比较约束的工具时它会被当成「库里没有」而删掉")
