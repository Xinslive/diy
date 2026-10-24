"""数据安全（OSS 备份）的测试。

这份测试盯的是三件事，按重要性排：

1. **备份里到底有什么** —— 特别是 ``report_row/col/cell`` 这三张**自己不带
   book_id** 的表。只按 book_id 筛就会漏掉它们，而漏掉的表现是备份照样成功、
   界面上照样显示行数，而用户设计的报表版式从来就不在任何一份备份里。
2. **分级判重** —— 每月一次、每周一次、每天一次，且**失败不算占位**。
3. **恢复真的能用** —— 真的导出、真的删库、真的灌回来、真的比对。

第 3 条是这份测试存在的主要理由。一个只测「导出成功」的备份测试，
证明的只是 gzip 写出来了 —— 那不是退路。
"""
from __future__ import annotations

import gzip
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text

from app.backup import export as export_mod
from app.backup import oss_client as oss_mod
from app.backup import retention, service, tier as tier_mod
from app.backup.export import ExportRefused
from app.backup.oss_client import RemoteObject
from app.backup.retention import Deleted
from app.models import (SCOPE_BOOK, SCOPE_SYSTEM, STATUS_FAILED, STATUS_SUCCESS, Entry,
                        TIER_DAILY, TIER_MONTHLY, TIER_WEEKLY, AuditLog, BackupRun,
                        Book, Period, ReportCell, ReportCol, ReportRow, ReportTemplate,
                        Subject, Voucher)

NOW = datetime(2026, 10, 17, 3, 17, tzinfo=timezone.utc)   # 东八区 11:17，周六


# ------------------------------------------------------------ 表归属


def test_excluded_tables_are_only_operational_state():
    """排除备份自引用和不可移植的插件运行状态；授权仍然进入 SYSTEM 备份。

    registry 的 digest、head、表元数据写入 v2 header 并与已安装代码核对，
    禁止把旧 worker lease、路径、未完成安装事务灌进另一台宿主。
    """
    assert export_mod.EXCLUDED_TABLES == frozenset({
        "sys_backup_run", "plugin_registry", "plugin_install_attempt",
        "plugin_worker_runtime", "plugin_cohort",
    })
    assert "plugin_user_access" not in export_mod.EXCLUDED_TABLES


def test_backup_run_never_appears_in_any_backup():
    names = {ts.table.name for ts in export_mod.tables_for(SCOPE_BOOK)}
    names |= {ts.table.name for ts in export_mod.tables_for(SCOPE_SYSTEM)}
    assert "sys_backup_run" not in names


def test_indirect_tables_are_included():
    """报表表体三张表自己没 book_id，但**必须**在账套备份里。

    它们只靠 template_id 指向 report_template。这条用例守着那个具体的坑：
    一旦有人把「沿外键递归」改回「只看 book_id 列」，这里立刻红。
    """
    book_tables = {ts.table.name: ts.kind for ts in export_mod.tables_for(SCOPE_BOOK)}
    for name in ("report_row", "report_col", "report_cell"):
        assert name in book_tables, f"{name} 不在账套备份里 —— 报表版式会丢"
        assert book_tables[name] == "indirect"


def test_every_table_is_classified():
    """每张表都要有归属，不许有「谁都没管」的表。

    一个没被分类的表意味着它的数据**不在任何一份备份里**，而且不会有任何报错。
    """
    scopes = export_mod.table_scopes()
    for t in export_mod.Base.metadata.sorted_tables:
        if t.name in export_mod.EXCLUDED_TABLES:
            continue
        assert t.name in scopes, f"表 {t.name} 没有归类"
        assert scopes[t.name].kind in ("book", "direct", "indirect", "global")


# ------------------------------------------------------------ 分级


def _record(db, scope, book_id, tier, status=STATUS_SUCCESS, when=NOW):
    db.add(BackupRun(book_id=book_id, scope=scope, book_label="x", tier=tier,
                     status=status, started_at=when, finished_at=when,
                     created_at=when, updated_at=when))
    db.commit()


def test_first_run_of_month_is_monthly(db, book):
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_MONTHLY


def test_tier_ladder_within_same_month(db, book):
    """同一个月里连跑三次：月度 → 周 → 天。这就是「每月一次、每周一次、每天一次」。"""
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_MONTHLY
    _record(db, SCOPE_BOOK, book.id, TIER_MONTHLY)
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_WEEKLY
    _record(db, SCOPE_BOOK, book.id, TIER_WEEKLY)
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_DAILY


def test_monthly_comes_again_next_month(db, book):
    _record(db, SCOPE_BOOK, book.id, TIER_MONTHLY, when=NOW)
    _record(db, SCOPE_BOOK, book.id, TIER_WEEKLY, when=NOW)
    _record(db, SCOPE_BOOK, book.id, TIER_DAILY, when=NOW)
    next_month = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, next_month) == TIER_MONTHLY


def test_weekly_comes_again_next_week(db, book):
    _record(db, SCOPE_BOOK, book.id, TIER_MONTHLY, when=NOW)
    _record(db, SCOPE_BOOK, book.id, TIER_WEEKLY, when=NOW)
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_DAILY
    next_week = NOW + timedelta(days=7)
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, next_week) == TIER_WEEKLY


def test_failed_run_does_not_consume_the_slot(db, book):
    """**失败不算占位** —— 否则月初一次上传超时，那个月就永远没有月度备份了。

    而界面上会是「一行成功记录都没有」，看着像配好了。
    """
    _record(db, SCOPE_BOOK, book.id, TIER_MONTHLY, status=STATUS_FAILED, when=NOW)
    assert tier_mod.classify(db, SCOPE_BOOK, book.id, NOW) == TIER_MONTHLY


def test_system_scope_uses_is_null_not_equals_null(db):
    """系统级备份的判重必须用 ``IS NULL``。

    ``== NULL`` 恒不成立，于是系统级备份每个周期都被判成「还没成功过」——
    月度档一天一份，永久保留，当天份数直线上涨。
    """
    assert tier_mod.succeeded_since(db, SCOPE_SYSTEM, None, TIER_MONTHLY,
                                    tier_mod.period_start(TIER_MONTHLY, tier_mod.local_now(NOW))) is False
    _record(db, SCOPE_SYSTEM, None, TIER_MONTHLY)
    assert tier_mod.succeeded_since(db, SCOPE_SYSTEM, None, TIER_MONTHLY,
                                    tier_mod.period_start(TIER_MONTHLY, tier_mod.local_now(NOW))) is True


def test_week_starts_on_monday():
    monday = datetime(2026, 10, 12, 9, 0)
    assert tier_mod.period_start(TIER_WEEKLY, monday) == datetime(2026, 10, 12)
    assert tier_mod.period_start(TIER_WEEKLY, monday + timedelta(days=6)) == datetime(2026, 10, 12)
    assert tier_mod.period_start(TIER_WEEKLY, monday + timedelta(days=7)) == datetime(2026, 10, 19)


def test_object_key_is_ascii_and_tier_is_first_dir():
    """对象键纯 ASCII，且 tier 是第一层目录（清理完全靠它）。"""
    key = tier_mod.object_key("acct-backup", TIER_MONTHLY, SCOPE_BOOK, 7,
                              tier_mod.local_now(NOW))
    assert key == "acct-backup/monthly/book-7/20261017-111700.json.gz"
    assert key.isascii()
    assert key.split("/")[1] == TIER_MONTHLY


# ------------------------------------------------------------ 保留策略


def _obj(key, age_days):
    return RemoteObject(key=key, size=1,
                        last_modified=datetime.now(timezone.utc) - timedelta(days=age_days))


def test_daily_keeps_30_days_weekly_keeps_a_year():
    now = datetime.now(timezone.utc)
    keep = {"daily": 30, "weekly": 365, "monthly": 0}
    daily = [_obj("p/daily/book-1/a.json.gz", 29), _obj("p/daily/book-1/b.json.gz", 31)]
    weekly = [_obj("p/weekly/book-1/a.json.gz", 364), _obj("p/weekly/book-1/b.json.gz", 366)]
    assert [k for k, _ in retention.plan_deletions(daily, "daily", keep["daily"], now)] == \
        ["p/daily/book-1/b.json.gz"]
    assert [k for k, _ in retention.plan_deletions(weekly, "weekly", keep["weekly"], now)] == \
        ["p/weekly/book-1/b.json.gz"]


def test_monthly_is_never_deleted():
    """**月度永久是结构性的。**

    即使 KEEP_DAYS 被改成 30、即使对象已经放了 50 年、即使 keep_days 传进来
    一个 30，plan_deletions 对 monthly 也必须返回空 —— 「永久」不该是一个
    「只要没人改那个数字就成立」的约定。
    """
    now = datetime.now(timezone.utc)
    ancient = [_obj("p/monthly/book-1/20200101-000000.json.gz", 365 * 50)]
    assert retention.plan_deletions(ancient, TIER_MONTHLY, 0, now) == []
    assert retention.plan_deletions(ancient, TIER_MONTHLY, 30, now) == []


def test_only_our_own_suffix_is_deleted():
    """清理只删 .json.gz。别的后缀的东西哪怕过期了也不碰。"""
    now = datetime.now(timezone.utc)
    objs = [_obj("p/daily/book-1/manual.sql", 999), _obj("p/daily/book-1/x.json.gz", 999)]
    assert [k for k, _ in retention.plan_deletions(objs, "daily", 30, now)] == \
        ["p/daily/book-1/x.json.gz"]


def test_missing_last_modified_is_kept():
    """OSS 没回 LastModified 时按「刚写下」处理，宁可多留。

    删掉一份备份的代价远大于多留一份，而这次清理明天还会再跑一次。
    """
    now = datetime.now(timezone.utc)
    objs = [RemoteObject(key="p/daily/book-1/a.json.gz", size=1, last_modified=None)]
    assert retention.plan_deletions(objs, "daily", 30, now) == []


class _FakeBucket:
    pass


def test_apply_retention_never_lists_monthly(monkeypatch, db):
    """月度前缀**连列都不列**。列了再「决定不删」是靠人自律；不列是结构保证。"""
    listed = []

    def fake_iter(bucket, prefix):
        listed.append(prefix)
        return iter([_obj(prefix + "x.json.gz", 100_000)])

    deleted = []
    monkeypatch.setattr(retention, "iter_objects", fake_iter)
    monkeypatch.setattr(retention, "delete_object",
                        lambda b, k: deleted.append(k))

    out = retention.apply_retention(
        _FakeBucket(), "acct-backup", {"daily": 30, "weekly": 365, "monthly": 0})

    assert listed == ["acct-backup/daily/", "acct-backup/weekly/"]
    assert "acct-backup/monthly/" not in listed
    assert len(deleted) == 2 and len(out) == 2


def test_dry_run_does_not_delete(monkeypatch):
    deleted = []
    monkeypatch.setattr(retention, "iter_objects",
                        lambda b, p: iter([_obj(p + "x.json.gz", 999)]))
    monkeypatch.setattr(retention, "delete_object", lambda b, k: deleted.append(k))
    out = retention.apply_retention(_FakeBucket(), "p", {"daily": 30, "weekly": 365, "monthly": 0},
                                    dry_run=True)
    assert deleted == []
    assert len(out) == 2


# ------------------------------------------------------------ 导出 / 恢复往返


def _add_report_template(db, book):
    tpl = ReportTemplate(book_id=book.id, code=f"t{uuid.uuid4().hex[:6]}",
                         name="自定义报表", type="CUSTOM")
    db.add(tpl)
    db.flush()
    db.add(ReportRow(template_id=tpl.id, row_no=1, label="资产总计", style={"bold": True}))
    db.add(ReportCol(template_id=tpl.id, col_no=1, label="期末余额"))
    db.add(ReportCell(template_id=tpl.id, row_no=1, col_no=1, formula="SUM(1,2)"))
    db.commit()
    return tpl


def test_book_export_contains_every_book_table(db, book, make_voucher, tmp_path):
    """账套级导出必须含**每**一张账套内的表，且行数与库里一致。"""
    make_voucher("2026-01-10", dr=[("1002", "1000.00", "存款")], cr=[("5001", "1000.00", "收入")])
    _add_report_template(db, book)
    db.commit()

    out = tmp_path / "b.json.gz"
    stats = export_mod.export_book(db, book, out)
    data = json.loads(gzip.decompress(out.read_bytes()))

    for ts in export_mod.tables_for(SCOPE_BOOK):
        assert ts.table.name in data["tables"], f"{ts.table.name} 不在导出里"
        assert stats.tables[ts.table.name] == \
            db.scalar(select(func.count()).select_from(ts.table)
                      .where(export_mod._predicate(ts, SCOPE_BOOK, book.id)))

    assert stats.tables["voucher"] == 1
    assert stats.tables["report_row"] == 1
    assert stats.tables["report_cell"] == 1
    assert data["header"]["book_id"] == book.id


def test_export_runs_on_a_repeatable_read_snapshot(db, book, tmp_path):
    """导出必须真的在 REPEATABLE READ 里跑 —— 断言的是**实际生效值**。

    这条不是为了「配了隔离级别」，而是因为**配错了不会响**。
    用 SQLAlchemy 的 ``execution_options(isolation_level=...)`` 去改一个已经
    INTRANS 的连接，psycopg 抛 ``can't change 'autocommit' now``，
    而 SQLAlchemy 只发一条 ``SAWarning`` 就把它忽略了 ——
    于是备份照常导出、照常压缩、照常算出 SHA256、照常上传，
    一切看起来都对，只是隔离级别还停在 READ COMMITTED。

    那意味着什么：凭证表读完再读科目表，中间别人录了一张凭证，
    出来的是一份**从未存在过**的状态（分录在、凭证不在）。
    它格式正确、能解开、能上传、校验和也对，唯独恢复不回来 ——
    而「校验和对得上」这件事还会让人更相信它。

    所以这里在导出期间抓一把实际值，而不是在代码里 grep 那个 SET。
    """
    from app.core.db import engine

    seen: list[str] = []

    def _after(conn, cursor, statement, parameters, context, executemany):
        # 只能在 SET **跑完之后**取值：begin 事件触发时事务刚开、还没发过
        # 一条语句，那时读出来永远是库的默认隔离级别，测了个寂寞。
        # 内层这条 SHOW 自己也会触发本函数，但它不含 "SET TRANSACTION"，
        # 所以不会递归。
        if "SET TRANSACTION ISOLATION" in statement.upper():
            seen.append(conn.execute(text("SHOW transaction_isolation")).scalar())

    event.listen(engine, "after_cursor_execute", _after)
    try:
        export_mod.export_book(db, book, tmp_path / "b.json.gz")
    finally:
        # 别把监听器留在 engine 上影响别的用例（engine 是模块级单例）。
        event.remove(engine, "after_cursor_execute", _after)

    assert seen, "没抓到任何事务 —— 导出没走 engine.connect()，隔离级别也就无从谈起"
    assert "repeatable read" in seen, (
        f"导出跑在 {[s for s in seen]} 上，不是 repeatable read。"
        "导出必须用独立连接 + SET TRANSACTION，不能复用调用方的 Session。"
    )


def test_export_is_compressed_and_checksum_matches_the_file(db, book, tmp_path):
    """产物是 gzip，且校验和对的是**压缩后**的字节 —— 那才是 OSS 上存的东西。"""
    out = tmp_path / "b.json.gz"
    stats = export_mod.export_book(db, book, out)
    assert out.read_bytes()[:2] == b"\x1f\x8b"          # gzip 魔数
    assert stats.checksum == __import__("hashlib").sha256(out.read_bytes()).hexdigest()
    assert stats.size_bytes == out.stat().st_size
    assert stats.size_bytes > 0


def test_export_is_deterministic(db, book, tmp_path):
    """同一份数据导两次，内容寻址的 SHA256 必须相同。

    gzip 头默认会写当前时间戳，那会让每次导出的文件都不同 —— 于是
    「校验和」没法用来判断两份备份内容是否一致，这个字段就白留了。
    """
    a, b_ = tmp_path / "a.gz", tmp_path / "b.gz"
    sa = export_mod.export_book(db, book, a, now=NOW)
    sb = export_mod.export_book(db, book, b_, now=NOW)
    assert sa.checksum == sb.checksum


def test_decimal_is_exported_as_text(db, book, make_voucher, tmp_path):
    """金额必须走文本。Numeric(20,2) 越过千万级时 float 已经不是那个数了。"""
    make_voucher("2026-01-11", dr=[("1002", "12345678.99", "款")],
                 cr=[("5001", "12345678.99", "收入")])
    db.commit()
    out = tmp_path / "b.json.gz"
    export_mod.export_book(db, book, out)
    data = json.loads(gzip.decompress(out.read_bytes()))
    amounts = [r["amount"] for r in data["tables"]["entry"]["rows"]]
    assert amounts == ["12345678.99", "12345678.99"]
    assert all(isinstance(a, str) for a in amounts)


def test_export_only_takes_this_books_rows(db, book, make_voucher, tmp_path):
    make_voucher("2026-01-12", dr=[("1002", "10", "a")], cr=[("5001", "10", "b")])
    db.commit()
    from app.accounting.periods import create_periods
    tag = uuid.uuid4().hex[:8]
    other = Book(company_name=f"别家公司{tag}", name=f"别家账套{tag}",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    p = db.scalars(select(Period).where(Period.book_id == other.id)).first()
    db.add(Voucher(book_id=other.id, period_id=p.id, voucher_no="记-001",
                   voucher_date=date(2026, 1, 12), status="UNPOSTED"))
    db.commit()

    out = tmp_path / "b.json.gz"
    export_mod.export_book(db, book, out)
    data = json.loads(gzip.decompress(out.read_bytes()))
    assert data["tables"]["book"]["rows"][0]["name"] == book.name
    assert all(r["book_id"] == book.id for r in data["tables"]["voucher"]["rows"])
    assert data["tables"]["voucher"]["rows"] != []


def test_round_trip_restores_everything(db, book, make_voucher, tmp_path):
    """**真的删库、真的灌回来、真的比对。**

    这是这份测试存在的主要理由：只测「导出成功」证明的只是 gzip 写出来了，
    那不是退路。往返一次之后，才知道归档里到底装了什么、灌回去会不会撞
    外键、序列有没有被推上去。
    """
    make_voucher("2026-02-10", dr=[("1002", "8888.88", "款")], cr=[("5001", "8888.88", "收入")])
    tpl = _add_report_template(db, book)
    book_id, tpl_code = book.id, tpl.code
    db.commit()

    # 导出前的基线
    before = {ts.table.name: db.scalar(
        select(func.count()).select_from(ts.table)
        .where(export_mod._predicate(ts, SCOPE_BOOK, book_id)))
        for ts in export_mod.tables_for(SCOPE_BOOK)}
    assert before["voucher"] == 1

    out = tmp_path / "b.json.gz"
    export_mod.export_book(db, book, out)

    # 真删，模拟灾难现场（空库）。CASCADE 会带走凭证、报表表体；
    # **audit_log 是 ON DELETE SET NULL，不会跟着走** —— 审计行是刻意比账套活得久的
    # （凭证号可回收，日志是事后唯一的追溯手段）。恢复现场把它们一并清掉，
    # 对应的正是「这台机器整个没了，重装之后从归档灌回来」。
    db.execute(text("DELETE FROM audit_log WHERE book_id = :i"), {"i": book_id})
    db.execute(text("DELETE FROM book WHERE id = :i"), {"i": book_id})
    db.commit()
    assert db.scalar(select(func.count()).select_from(Voucher)
                     .where(Voucher.book_id == book_id)) == 0

    counts = export_mod.restore(db, out)
    db.commit()

    after = {ts.table.name: db.scalar(
        select(func.count()).select_from(ts.table)
        .where(export_mod._predicate(ts, SCOPE_BOOK, book_id)))
        for ts in export_mod.tables_for(SCOPE_BOOK)}
    assert after == before
    assert counts["voucher"] == 1 and counts["report_cell"] == 1

    # 金额逐字节回来，不是「差一点点」。
    # 借贷合计是分录算出来的、voucher 上并没有这两列 —— 所以直接查分录。
    amounts = sorted(db.scalars(
        select(Entry.amount).join(Voucher, Voucher.id == Entry.voucher_id)
        .where(Voucher.book_id == book_id)).all())
    assert amounts == [Decimal("8888.88"), Decimal("8888.88")]
    assert db.scalar(select(func.count()).select_from(ReportTemplate)
                     .where(ReportTemplate.book_id == book_id)) == 1


def test_restore_resets_sequence(db, book, tmp_path):
    """恢复后序列要推上去，否则**第一张新凭证**就撞主键。

    一个当场看不见、要等到有人真的开始记账才炸的故障。
    """
    out = tmp_path / "b.json.gz"
    export_mod.export_book(db, book, out)
    book_id = book.id
    # 先删再灌：恢复是**灾难恢复**动作，不是覆盖写。对着已有数据的库灌会撞
    # 主键，而那正是我们要的行为 —— 绝不静默覆盖一份还在用的账。
    db.execute(text("DELETE FROM audit_log WHERE book_id = :i"), {"i": book_id})
    db.execute(text("DELETE FROM book WHERE id = :i"), {"i": book_id})
    db.commit()
    counts = export_mod.restore(db, out)
    db.commit()
    assert counts["book"] == 1

    nxt = db.scalar(text(
        "SELECT setval(pg_get_serial_sequence('book','id'), "
        "COALESCE((SELECT MAX(id) FROM book),0)+1, false)"))
    first_free = db.scalar(text("SELECT nextval('book_id_seq')"))
    assert first_free == int(nxt)


# ------------------------------------------------------------ 拒绝恢复


def test_restore_refuses_a_truncated_archive(db, tmp_path):
    p = tmp_path / "bad.gz"
    p.write_bytes(gzip.compress(b'{"format":"cwq-book-backup"')[:20])
    with pytest.raises(ExportRefused, match="解不开|拒绝恢复"):
        export_mod.restore(db, p, dry_run=True)


def test_restore_refuses_a_foreign_file(db, tmp_path):
    p = tmp_path / "other.gz"
    p.write_bytes(gzip.compress(json.dumps(
        {"header": {"format": "pg_dump", "version": 1}, "tables": {"book": {}}}).encode()))
    with pytest.raises(ExportRefused, match="不是本系统导出的备份"):
        export_mod.restore(db, p, dry_run=True)


def test_restore_refuses_unknown_version(db, tmp_path):
    """未知版本**仍然拒绝** —— 但拒绝的理由必须可操作。

    原来这条断言的是消息里有「只认」两个字（原文是「本程序只认 1」）。
    那句话恰恰是要被换掉的：它既没说这份归档是哪一版、也没说这台程序能读哪几版、
    更没说该怎么读到它。改成断言「说清了 99、说清了能读第 1 版、说了换哪台机器」。

    **拒绝本身没有放松**：硬灌一份读不懂的归档，比恢复失败更糟 ——
    最坏的结果不是恢复失败，而是恢复「成功」了。
    """
    p = tmp_path / "v99.gz"
    p.write_bytes(gzip.compress(json.dumps(
        {"header": {"format": "cwq-book-backup", "version": 99},
         "tables": {"book": {"columns": ["id"], "rows": []}}}
    ).encode()))
    with pytest.raises(ExportRefused) as ei:
        export_mod.restore(db, p, dry_run=True)
    msg = str(ei.value)
    assert "99" in msg and "第 1 版" in msg and "换一台" in msg


def test_restore_refuses_archive_with_no_tables(db, tmp_path):
    p = tmp_path / "empty.gz"
    p.write_bytes(gzip.compress(json.dumps(
        {"header": {"format": "cwq-book-backup", "version": 1}, "tables": {}}
    ).encode()))
    with pytest.raises(ExportRefused, match="一张表都没有"):
        export_mod.restore(db, p, dry_run=True)


def test_restore_dry_run_writes_nothing(db, book, tmp_path):
    out = tmp_path / "b.json.gz"
    export_mod.export_book(db, book, out)
    before = db.scalar(select(func.count()).select_from(Subject))
    counts = export_mod.restore(db, out, dry_run=True)
    assert counts["subject"] > 0
    assert db.scalar(select(func.count()).select_from(Subject)) == before


# ------------------------------------------------------------ 配置与 API


def test_backup_config_never_returns_secrets(db, admin_user, client, auth):
    r = client.put("/api/system/backup", json={
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "acct-backup-bucket",
        "access_key_id": "LTAI5tREALLYSECRET",
        "access_key_secret": "REALLYsecretvalue",
    }, headers=auth)
    assert r.status_code == 200, r.text
    blob = r.text
    assert "REALLYsecretvalue" not in blob
    assert "LTAI5tREALLYSECRET" not in blob
    assert r.json()["access_key_secret_set"] is True
    assert r.json()["access_key_secret_hint"].endswith("alue")


def test_backup_config_write_only_secrets_survive_a_normal_save(db, admin_user, client, auth):
    """空输入框 + 普通保存，**不能**把凭据抹掉。

    抹掉之后下一次定时备份会失败，而失败要等到第二天才被发现。
    """
    client.put("/api/system/backup", json={"access_key_id": "AK1", "access_key_secret": "SK1"}, headers=auth)
    r = client.put("/api/system/backup", json={"bucket": "another-bucket"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["access_key_secret_set"] is True
    assert r.json()["access_key_id_set"] is True


def test_clear_access_key_is_explicit(db, admin_user, client, auth):
    client.put("/api/system/backup", json={"access_key_id": "AK1", "access_key_secret": "SK1"}, headers=auth)
    r = client.put("/api/system/backup", json={"clear_access_key": True}, headers=auth)
    assert r.status_code == 200
    assert r.json()["access_key_secret_set"] is False


def test_backup_config_is_admin_only(db, client, plain_auth):
    h = dict(plain_auth)
    assert client.get("/api/system/backup", headers=h).status_code == 403
    assert client.put("/api/system/backup", json={"bucket": "x"}, headers=h).status_code == 403
    assert client.get("/api/system/backup/runs", headers=h).status_code == 403
    assert client.post("/api/system/backup/run", headers=h).status_code == 403


def test_backup_config_requires_login(db, client):
    assert client.get("/api/system/backup").status_code == 401


def test_run_without_config_is_refused_loudly(db, client, auth):
    """没配 OSS 就点备份：必须是 400 且说清缺什么，不能是 500 或静默成功。"""
    r = client.post("/api/system/backup/run", headers=auth)
    assert r.status_code == 400
    assert "未配置完整" in r.json()["detail"]


def test_retention_view_reports_monthly_as_forever(db, client, auth):
    client.put("/api/system/backup",
               json={"keep_daily_days": 30, "keep_weekly_days": 365}, headers=auth)
    body = client.get("/api/system/backup", headers=auth).json()
    assert body["keep_days"]["daily"] == 30
    assert body["keep_days"]["weekly"] == 365
    assert body["keep_days"]["monthly"] == 0
    assert body["monthly_forever"] is True


def test_keep_days_can_be_edited_but_monthly_cannot(db, client, auth):
    r = client.put("/api/system/backup", json={"keep_daily_days": 7, "keep_weekly_days": 90}, headers=auth)
    assert r.status_code == 200
    assert r.json()["keep_days"] == {"daily": 7, "weekly": 90, "monthly": 0}


def test_prefix_slashes_are_normalized(db, client, auth):
    r = client.put("/api/system/backup", json={"prefix": "/acct-backup/"}, headers=auth)
    assert r.json()["prefix"] == "acct-backup"


def test_backup_runs_endpoint_lists_history(db, book, admin_user, client, auth):
    _record(db, SCOPE_BOOK, book.id, TIER_DAILY, STATUS_SUCCESS, when=NOW)
    _record(db, SCOPE_BOOK, book.id, TIER_WEEKLY, STATUS_FAILED, when=NOW)
    body = client.get("/api/system/backup/runs", headers=auth).json()
    mine = [r for r in body if r["book_id"] == book.id]
    assert len(mine) == 2
    assert {r["status"] for r in mine} == {STATUS_SUCCESS, STATUS_FAILED}
    assert all(r["started_at"] for r in mine)


def test_backup_config_change_is_audited(db, book, admin_user, client, auth):
    client.put("/api/system/backup", json={"bucket": "audited-bucket", "access_key_secret": "S"}, headers=auth)
    rows = db.scalars(select(AuditLog)
                      .where(AuditLog.action == "SYSTEM_BACKUP_CONFIG_UPDATE")).all()
    assert rows, "改备份配置没有留痕"
    assert "S" not in rows[-1].after


def test_restore_refuses_to_overwrite_an_existing_book(db, book, tmp_path):
    """恢复是**灾难恢复**，不是覆盖写。

    库里已经有一套数据时，恢复撞主键并整体回滚 —— 而不是「有冲突就跳过」或
    「有冲突就覆盖」。理由很直接：一份归档可能来自几天前，把它盖在今天的账上，
    等于用旧数据抹掉新数据，而且没有任何东西会提示这件事发生过。
    """
    out = tmp_path / "b.json.gz"
    export_mod.export_book(db, book, out)
    with pytest.raises(Exception) as exc:
        export_mod.restore(db, out)
    assert "duplicate key" in str(exc.value).lower() or "违反" in str(exc.value)
    db.rollback()
    assert db.get(Book, book.id) is not None      # 原数据还在


# ------------------------------------------------------------ 编排（OSS 打桩）


class _FakeOss:
    """把 oss2 换成内存字典。**全程不碰网络。**"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_on: str | None = None

    def make_bucket(self, cfg):
        return self

    def put_bytes(self, bucket, key, data):
        if self.fail_on and self.fail_on in key:
            from app.backup.oss_client import OssError
            raise OssError(f"模拟失败：{key}")
        self.objects[key] = data
        return "etag"

    def put_file(self, bucket, key, path):
        """备份走的是**按文件**上传（不把整份 gzip 读进内存）。"""
        if self.fail_on and self.fail_on in key:
            from app.backup.oss_client import OssError
            raise OssError(f"模拟失败：{key}")
        self.objects[key] = Path(path).read_bytes()
        return "etag"

    def iter_objects(self, prefix):
        for k in sorted(self.objects):
            if k.startswith(prefix):
                yield RemoteObject(key=k, size=len(self.objects[k]),
                                   last_modified=datetime.now(timezone.utc))

    def delete_object(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)


@pytest.fixture
def oss(monkeypatch, db):
    """把 OSS 参数写进设置、把客户端换成假实现，返回那个假实现。"""
    from app.backup import oss_client as oss_mod
    from app.backup import retention as ret
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-hangzhou.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct-backup"), ("oss_prefix", "acct-backup"),
                       ("backup_include_system", "1"),
                       ("backup_keep_daily_days", "30"),
                       ("backup_keep_weekly_days", "365")):
        system_settings.set_value(db, key, value)
    db.commit()

    fake = _FakeOss()
    monkeypatch.setattr(oss_mod, "make_bucket", fake.make_bucket)
    monkeypatch.setattr(oss_mod, "put_bytes", fake.put_bytes)
    monkeypatch.setattr(oss_mod, "put_file", fake.put_file)
    monkeypatch.setattr(ret, "iter_objects", lambda b, p: fake.iter_objects(p))
    monkeypatch.setattr(ret, "delete_object", lambda b, k: fake.delete_object(k))
    return fake


def test_run_backup_uploads_one_object_per_book(db, book, admin_user, oss):
    """每个账套一个对象，外加一份系统数据；键的第一层目录是档位。"""
    from app.backup import service as service_mod

    result = service_mod.run_backup(db, actor=admin_user, now=NOW)

    assert result.ok
    assert not result.failed
    # 库是 session 级共用的（别的用例也建过账套），所以按账套/作用域筛，
    # 不对**总条数**断言 —— 那测的是别的用例留下了几本书。
    mine = [r for r in result.runs if r.scope == SCOPE_BOOK and r.book_id == book.id]
    assert len(mine) == 1
    assert mine[0].tier == TIER_MONTHLY
    assert mine[0].size_bytes > 0 and mine[0].checksum and mine[0].row_count > 0
    assert [r for r in result.runs if r.scope == SCOPE_SYSTEM], [(r.scope, r.tier, r.status, r.message[:200]) for r in result.runs]

    assert any(f"/monthly/book-{book.id}/" in k for k in oss.objects), sorted(oss.objects)
    # 系统级那份不挑档位：前面的用例已经给 SCOPE_SYSTEM 占过本月/本周的名额，
    # 这一轮它落到哪一档，取决于那些记录 —— 那是分级在正常工作，不是这里要测的东西。
    assert any("/system/" in k for k in oss.objects), sorted(oss.objects)
    # 真的传上去了，而且字节就是压缩后的那份
    key = next(k for k in oss.objects if f"book-{book.id}" in k)
    assert oss.objects[key][:2] == b"\x1f\x8b"


def test_second_run_same_day_drops_to_daily(db, book, admin_user, oss):
    """同一个月的第二次跑：月度已占位，这次记周/天 —— 每天仍然有产物。"""
    from app.backup import service as service_mod

    service_mod.run_backup(db, actor=admin_user, now=NOW, book_ids=[book.id])
    result = service_mod.run_backup(db, actor=admin_user, now=NOW, book_ids=[book.id])
    assert [r.tier for r in result.runs] == [TIER_WEEKLY]   # 月度已占位，本周还没
    assert sorted(k.split("/")[1] for k in oss.objects) == ["monthly", "weekly"]


def test_run_backup_prunes_expired_daily_but_never_monthly(db, book, admin_user, oss,
                                                          monkeypatch):
    """过期的天备份被清掉，同龄的月度备份原封不动。"""
    from app.backup import service as service_mod
    from app.backup import retention as ret

    old_daily = "acct-backup/daily/book-1/20200101-000000.json.gz"
    old_monthly = "acct-backup/monthly/book-1/20200101-000000.json.gz"
    oss.objects[old_daily] = b"x"
    oss.objects[old_monthly] = b"x"

    # 让**所有**对象看起来都放了 400 天（超过天备份的 30 天，也超过周的 365 天）
    old = datetime.now(timezone.utc) - timedelta(days=400)
    base = oss.iter_objects
    monkeypatch.setattr(ret, "iter_objects",
                        lambda b, p: (RemoteObject(o.key, o.size, old) for o in base(p)))

    service_mod.run_backup(db, actor=admin_user, now=NOW)

    assert old_daily not in oss.objects
    assert old_monthly in oss.objects, "月度备份被删了 —— 那正是唯一那份永久档"


def test_one_book_failing_does_not_stop_the_others(db, book, admin_user, oss):
    """一本传失败，其余照传；两条记录都留下，原因写全文。"""
    from app.backup import service as service_mod

    # 造第二本账，好让「一本挂掉不影响另一本」这件事真的有个「另一本」
    from app.accounting.periods import create_periods
    tag = uuid.uuid4().hex[:8]
    other = Book(company_name=f"别家公司{tag}", name=f"别家账套{tag}",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    db.commit()

    oss.fail_on = f"book-{book.id}"
    result = service_mod.run_backup(db, actor=admin_user, now=NOW,
                                    book_ids=[book.id, other.id])

    assert result.ok, "另一本传成功了，这一轮就该算成功"
    assert len(result.runs) == 2
    assert len(result.failed) == 1
    assert result.failed[0].book_id == book.id
    failed = result.failed[0]
    assert "模拟失败" in failed.message
    assert failed.oss_key == ""                     # 失败不该留下指向对象的键
    assert any(r.status == STATUS_SUCCESS for r in result.runs)


def test_all_failing_reports_not_ok(db, book, admin_user, oss):
    """全灭必须 ok=False —— 脚本据此非零退出，界面也不会显示成功。"""
    from app.backup import service as service_mod

    oss.fail_on = "/"
    result = service_mod.run_backup(db, actor=admin_user, now=NOW, book_ids=[book.id])

    assert not result.ok
    assert result.succeeded == []
    assert len(result.failed) == 1


def test_no_targets_is_refused_loudly(db, admin_user, oss):
    """指定了一个不存在的账套：必须拒绝，不能「成功跑了 0 个」。"""
    from app.backup import service as service_mod

    with pytest.raises(export_mod.ExportRefused, match="没有可备份的对象"):
        service_mod.run_backup(db, actor=admin_user, book_ids=[999999])


def test_temp_file_is_removed_after_upload(db, book, admin_user, oss, tmp_path, monkeypatch):
    """上传完就删本地副本：服务器上不留全量账务数据的明文。"""
    from app.backup import service as service_mod

    monkeypatch.setenv("CWQ_BACKUP_WORK_DIR", str(tmp_path))
    service_mod.run_backup(db, actor=admin_user, now=NOW, book_ids=[book.id])

    assert list(tmp_path.iterdir()) == []


def test_backup_run_is_audited(db, book, admin_user, oss):
    from app.backup import service as service_mod

    service_mod.run_backup(db, actor=admin_user, now=NOW)
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "BACKUP_RUN")).all()
    assert rows, "跑备份没有留痕"


# ==================================================================
# 超时与连接池（2026-10-17 那次故障里看到的 CLOSE_WAIT 引出来的）
# ==================================================================

class _FakeSession:
    """记自己有没有被关掉的 requests.Session 替身。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _BucketWithSession:
    def __init__(self):
        self.session = _FakeSession()


def test_oss2_的签名第二个参数确实是文件名():
    """钉住 **SDK 的契约本身**：``put_object_from_file(key, filename)``。

    这条得对着**真 SDK** 看。替身照着实现写的时候，实现和替身会一起错
    （真犯过一次：2026-10-23 加 ``put_file`` 时传了文件句柄，替身也就跟着
    收句柄，于是每天都在失败的离站备份在测试里一路绿灯）。只有对着 SDK 看，
    才能发现两边一起错。
    """
    import inspect

    import oss2

    params = list(inspect.signature(oss2.Bucket.put_object_from_file).parameters)
    assert params[:3] == ["self", "key", "filename"], params


def test_上传传的是路径_不是文件句柄(tmp_path):
    """``put_file`` 传给 oss2 的必须是**本地文件路径**。

    传句柄进去，oss2 拿它当路径用，抛的是
    ``TypeError: expected str, bytes or os.PathLike object, not BufferedReader``
    —— 每一轮离站备份都失败，而且日志里看不出是自己这边的参数问题。

    这里用一个**照 SDK 契约写**的替身：只收路径，给句柄就当场抛。
    """
    seen = {}

    class ContractBucket:
        def put_object_from_file(self, key, filename):
            # 照 oss2 的契约：第二个参数是本地文件路径，oss2 自己会 open 它。
            if not isinstance(filename, (str, os.PathLike)):
                raise TypeError(
                    "expected str, bytes or os.PathLike object, not "
                    f"{type(filename).__name__}")
            with open(filename, "rb") as handle:
                seen["content"] = handle.read()
            seen["filename"] = filename
            return SimpleNamespace(etag="etag")

    target = tmp_path / "backup.json.gz"
    target.write_bytes(b"hello")

    oss_mod.put_file(ContractBucket(), "backup/daily/book-1/x.json.gz", str(target))

    assert isinstance(seen["filename"], str), "传过去的必须还是路径"
    assert seen["content"] == b"hello"


def test_本地文件不存在时报的是人话():
    """文件不在了要说清楚是哪个文件，别让 oss2 的异常糊过去。"""
    import pytest as _pytest

    class Bucket:
        def put_object_from_file(self, key, filename):
            raise FileNotFoundError(filename)

    with _pytest.raises(oss_mod.OssError) as excinfo:
        oss_mod.put_file(Bucket(), "backup/daily/book-1/x.json.gz", "/nope/x.json.gz")
    assert "本地文件不存在" in str(excinfo.value)


def test_make_bucket显式传超时_不用SDK默认的60秒(monkeypatch):
    """不传 ``connect_timeout`` 时 oss2 用的是 ``defaults.connect_timeout = 60``。

    那个 60 与「这一次调用要做的事」毫无关系：列一页对象和传一个几百 MB 的
    账套快照拿到的是同一个数。而 oss2 2.19.1 **没有任何重试** —— 所以这个值
    是「多久承认失败」的唯一闸门，它不是保护，是上限。
    """
    import oss2

    seen = {}

    class FakeBucket:
        def __init__(self, auth, endpoint, bucket_name, connect_timeout=None):
            seen["timeout"] = connect_timeout

    monkeypatch.setattr(oss2, "Auth", lambda a, b: object())
    monkeypatch.setattr(oss2, "Bucket", FakeBucket)
    cfg = {"endpoint": "oss-cn-heyuan.aliyuncs.com", "bucket": "b",
           "access_key_id": "k", "access_key_secret": "s"}

    oss_mod.make_bucket(cfg)

    assert seen["timeout"] == oss_mod.OSS_TIMEOUT, \
        "没传显式超时 —— 于是默默用了 SDK 的 60 秒"
    # (建连, 读写)：握手和传大对象差一个数量级，不该共用一个数
    connect, io = seen["timeout"]
    assert connect < io, "建连超时不该大于读写超时"
    assert connect <= 15, "建连超时给到几十秒，等于每次网络不通都要干等"


def test_超时是二元组能一路传到requests():
    """oss2 把 connect_timeout **原样**当 requests 的 timeout= 用。

    钉住这一点是因为它是整个方案的前提：如果哪天 oss2 改成只接受标量
    （或者自己做 int 转换），二元组会在 SDK 里炸掉，而炸点在那行
    ``defaults.get(...)`` 之后 —— 离这里很远。
    """
    import oss2

    b = oss2.Bucket(oss2.Auth("a", "b"), "oss-cn-heyuan.aliyuncs.com",
                    "a-valid-bucket-name", connect_timeout=(10, 60))
    assert b.timeout == (10, 60)


def test_close_bucket关掉连接池():
    """不关连接池，CLOSE_WAIT 的连接就一直占着 fd。

    实测在 worker 上看到过一条长期挂着 CLOSE_WAIT 的连接，目的地正是
    ``oss-cn-heyuan.aliyuncs.com``（解析到 47.113.155.25）。CLOSE_WAIT 是
    「对端已发 FIN、本地还没 close」—— 本地这一侧还占着一个 fd，而且是
    **不报错**地占着，最后以 ``Too many open files`` 在不相干的地方炸掉。
    """
    b = _BucketWithSession()
    oss_mod.close_bucket(b)
    assert b.session.closed


def test_close_bucket对没有session的替身是空操作():
    """测试替身没有 ``.session``；这时必须安静地什么都不做。

    抛异常的话，所有用替身跑备份的用例都会变成「因为清理失败而失败」，
    于是真正要测的东西被一个与它无关的失败盖住。
    """
    oss_mod.close_bucket(object())
    oss_mod.close_bucket(None)


def test_close_bucket自己抛异常时不能连带失败():
    """关不掉连接池**不是**备份失败的原因。

    一次已经成功的备份因为收尾出错而被报成失败，会让运维去查一个根本没坏的
    地方 —— 那正是「看起来在工作但其实没生效」的变体。
    """
    class Bad:
        class session:
            @staticmethod
            def close():
                raise RuntimeError("boom")

    oss_mod.close_bucket(Bad())          # 不抛就算过


def test_oss_bucket用完一定关(monkeypatch):
    """``with`` 形态：正常退出和抛异常都要关。"""
    b = _BucketWithSession()
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: b)

    with oss_mod.oss_bucket({"endpoint": "e", "bucket": "b",
                             "access_key_id": "k", "access_key_secret": "s"}):
        pass
    assert b.session.closed

    b2 = _BucketWithSession()
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: b2)
    with pytest.raises(ValueError):
        with oss_mod.oss_bucket({}):
            raise ValueError("备份中途失败")
    assert b2.session.closed, "异常路径上没关 —— 而那正是最常见的结局"


def test_测试连接用更短的超时(monkeypatch):
    """"测试连接"是**给人点的按钮**，等一分钟才报错是不能接受的。"""
    seen = {}
    monkeypatch.setattr(oss_mod, "make_bucket",
                        lambda cfg, **kw: seen.setdefault("timeout", kw.get("timeout"))
                        or _BucketWithSession())
    monkeypatch.setattr(oss_mod, "put_bytes", lambda b, k, d: "etag")
    monkeypatch.setattr(oss_mod, "delete_object", lambda b, k: None)
    oss_mod.check_connection({"endpoint": "oss-cn-heyuan.aliyuncs.com",
                              "bucket": "b", "access_key_id": "k",
                              "access_key_secret": "s"})
    assert seen["timeout"] == oss_mod.OSS_PROBE_TIMEOUT
    assert oss_mod.OSS_PROBE_TIMEOUT[1] < oss_mod.OSS_TIMEOUT[1]


def test_run_backup结束时关掉连接池(db, book, admin_user, oss, monkeypatch):
    """整轮备份跑完，连接池必须被关。

    旧版没有这一步：Bucket 句柄随函数返回被回收，连接池交给 GC 与
    urllib3 的 keep-alive 超时 —— 两者都不是「这一轮结束时」。
    """
    holder = _BucketWithSession()
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: holder)
    service.run_backup(db, actor=admin_user, book_ids=[book.id])
    assert holder.session.closed


def test_run_backup抛异常时也关连接池(db, monkeypatch, oss):
    """``finally`` 而不是「成功路径上关」。

    备份最常见的结局就是抛异常（某一本网络失败、配置被改掉）。只在成功时
    关，等于异常路径永远漏 —— 而异常恰恰是更常见的那条路。
    """
    holder = _BucketWithSession()
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: holder)
    # 配置不全会在 require_ready 阶段抛，那时**还没建桶**；这里要验的正是
    # 「桶已经建好、然后才炸」的那条路径 —— 那种如果不关，就是漏的。
    with pytest.raises(ExportRefused):
        service.run_backup(db, book_ids=[999999])
    assert holder.session.closed, "异常路径上连接池没关"


# ==================================================================
# 格式版本注册表 —— 用户的原话：「我可不想看到未来某天需要恢复数据的时候
# 提示不兼容之类的」。
#
# 旧实现是 ``if header["version"] != VERSION: raise``。它在 VERSION 还是 1、
# 一份归档都还没产生的时候看着无害，致命之处在于**月度归档永久保留**：
# 等 VERSION 升到 2，今天存的每一份月度归档会同时失去可恢复性，
# 而那恰恰是出事那天唯一能救回来的东西。
# ==================================================================


@pytest.fixture
def become_future(monkeypatch):
    """把程序**模拟**成下一个格式版本（不覆盖已有 reader）。

    **为什么是「一个可调用的开关」而不是一个直接改好的夹具。** 第一版这么写的
    时候，夹具在用例体之前就把 VERSION 改成了 2 —— 于是用例里的
    ``export_book`` 用 v2 **写出**归档，然后拿它去验「v1 还能不能读」。
    归档里写的本来就是 2，所以那条用例在旧代码下**也是绿的**。

    这就是最该防的那种空洞：测试看着在验证兼容性，实际验证的是「新程序读新
    归档」—— 而那件事从来不会坏。真正会坏的是「新程序读旧归档」。
    所以切换必须发生在**写完之后**，并且要断言归档里真的写着 version 1。
    """
    before_readers = dict(export_mod.FORMAT_READERS)
    before_version = export_mod.VERSION

    def go():
        @export_mod.register_reader(before_version + 1, "测试用的下一个格式版本")
        def _fake_v2(data, path):                   # pragma: no cover - 只为占位
            return data
        monkeypatch.setattr(export_mod, "VERSION", before_version + 1)
        return before_version + 1

    yield go
    # 注册表是模块级全局，漏还原会让后面所有用例都以为自己活在一个能读两版的
    # 程序里 —— 那样真正的拒绝路径就再也测不到了。
    export_mod.FORMAT_READERS.clear()
    export_mod.FORMAT_READERS.update(before_readers)
    export_mod.VERSION = before_version


def _assert_archive_is_v1(path):
    """确认这份归档**真的是 v1**。

    没有这一步，上面那个「先升版本再导出」的坑会原样复现：断言全绿，
    而验证的根本不是历史归档。
    """
    with gzip.open(path, "rb") as fh:
        head = json.loads(fh.readline().decode("utf-8").rstrip(",")[:-1]
                          + "}")["header"]
    assert head["version"] == 1, f"归档写的是 v{head['version']}，不是 v1 —— 用例验的不是历史归档"


def test_v1的reader必须永久存在():
    """**最要紧的一条守卫。**

    v1 是所有过去与将来月度归档的唯一入口。删掉它（哪怕是「清理死代码」时顺手
    删掉）就等于让全部历史归档在出事那天集体失效 —— 而这个删除动作在测试里
    **不会让任何一条用例变红**，因为那时所有测试也都在用新版本导出。
    """
    assert 1 in export_mod.FORMAT_READERS, \
        "v1 reader 不在了 —— 已归档的月度备份会集体变成不可恢复"
    assert export_mod.readable_versions() == sorted(export_mod.FORMAT_READERS)


def test_升到下一个版本之后v1归档仍然可以恢复(db, book, make_voucher, tmp_path,
                                       become_future):
    """核心断言：程序往前走一步，历史归档必须原地不受影响。

    这条在旧实现下**必然是红的** —— ``!= VERSION`` 会直接拒绝 v1。
    """
    make_voucher("2026-02-10", dr=[("1002", "8888.88", "款")],
                 cr=[("5001", "8888.88", "收入")])
    db.commit()
    out = tmp_path / "v1.json.gz"
    export_mod.export_book(db, book, out)          # 先用 v1 写出来
    _assert_archive_is_v1(out)
    future_version = become_future()               # 写完历史格式后再模拟升级
    assert export_mod.VERSION == future_version

    counts = export_mod.restore(db, out, dry_run=True)
    assert counts, "v1 归档在 VERSION=2 的程序下被拒了 —— 旧归档全部失效"
    assert counts["voucher"] == 1


def test_升版本后v1归档能真正灌回数据库(db, book, make_voucher, tmp_path,
                                         become_future):
    """不只是 dry_run 通过，是真的灌回去。

    dry_run 只走到 :func:`read_snapshot` 之后的列校验；真正灌回还要经过
    :func:`_decode`、类型 coercion 与外键顺序。版本注册表如果只在读的那一步
    生效、后面漏了，dry_run 会绿而这里会红。
    """
    make_voucher("2026-02-10", dr=[("1002", "8888.88", "款")],
                 cr=[("5001", "8888.88", "收入")])
    db.commit()
    out = tmp_path / "v1.json.gz"
    export_mod.export_book(db, book, out)
    _assert_archive_is_v1(out)
    become_future()                                # VERSION 从此刻起是 2
    book_id = book.id
    db.execute(text("DELETE FROM audit_log WHERE book_id = :i"), {"i": book_id})
    db.execute(text("DELETE FROM book WHERE id = :i"), {"i": book_id})
    db.commit()

    export_mod.restore(db, out)                     # VERSION 此刻是 2
    db.commit()
    assert db.scalar(select(func.count()).select_from(Voucher)
                     .where(Voucher.book_id == book_id)) == 1


def test_重复登记同一版本会被拒绝():
    """「改掉旧 reader」是最容易犯、后果最重的改动，所以在这里直接拦住。

    真实的坏路径长这样：某一版出了 bug，于是有人去改那个 reader 的实现 ——
    而那份格式的归档**已经躺在 OSS 上永久保存了**。改 reader 不会让它们报错，
    只会在恢复时静默地读出错的内容 —— 那比拒绝恶劣得多。
    """
    with pytest.raises(ValueError, match="已经有 reader"):
        @export_mod.register_reader(1, "偷偷替换 v1")
        def _bad(data, path):                       # pragma: no cover
            return data


def test_认不出来的版本报错必须可操作(tmp_path):
    """「不兼容」这三个字本身是有害的。

    它既没说这份归档是哪一版、也没说这台程序能读哪几版、更没说该怎么读到它。
    收到这句话的人只剩两个选择：干等着，或者凭感觉改归档里的 version 数字 ——
    后者会把归档灌坏。
    """
    p = tmp_path / "future.json.gz"
    p.write_bytes(_archive_bytes(version=99))
    with pytest.raises(ExportRefused) as ei:
        export_mod.read_snapshot(p)
    msg = str(ei.value)
    assert "99" in msg, "没说这份归档是哪一版"
    assert "第 1 版" in msg, "没说本程序能读哪几版"
    assert "换一台" in msg, "没说该怎么读到它 —— 这是最要紧的一半"
    assert "永久保留" in msg, "没说旧归档不会因为升版本而失效"


def test_版本号不是整数时报错也可操作(tmp_path):
    p = tmp_path / "weird.json.gz"
    p.write_bytes(_archive_bytes(version="一"))
    with pytest.raises(ExportRefused) as ei:
        export_mod.read_snapshot(p)
    assert "不是本系统导出的备份" in str(ei.value)


def test_不要回到版本必须相等的写法():
    """静态守卫：``read_snapshot`` 里不许再出现 ``!= VERSION``。

    旧实现就是这一句。它在 VERSION 还是 1 时**完全看不出问题** —— 而这正是
    它能在没有任何一份归档存在的时候活下来的原因。等到有归档时再改，已经晚了。
    """
    import ast
    import inspect
    # 用 AST 而不是字符串查找：注释里**必须**能引用旧写法来说明为什么不能那么写
    # （``# 版本查表，**不是** ``!= VERSION````），按文本查会把注释一起判成违规。
    tree = ast.parse(inspect.getsource(export_mod.read_snapshot).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.NotEq, ast.Eq)) for op in node.ops):
            names = {getattr(n, "id", getattr(n, "attr", "")) for n in
                     (node.left, *node.comparators)}
            assert "VERSION" not in names, \
                "read_snapshot 又变回「版本必须恰好等于 VERSION」了 —— 升版本当天所有历史归档失效"
    src = inspect.getsource(export_mod.read_snapshot)
    assert "FORMAT_READERS.get" in src, "应当查版本注册表"


def _archive_bytes(*, version, tables=None, fmt=export_mod.FORMAT):
    """造一份最小可读归档的字节（给版本相关的用例用）。"""
    body = {
        "header": {"format": fmt, "version": version, "scope": "book",
                   "book_id": 1, "book_label": "x",
                   "exported_at": "2026-10-17T00:00:00"},
        "tables": tables if tables is not None else {
            "sys_subject": {"columns": ["id"], "rows": [[1]]}},
    }
    if isinstance(version, int) and version >= 2:
        body["header"]["plugins"] = []
    return gzip.compress(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def test_归档里有当前库里没有的列时报错要可操作(db, tmp_path):
    """列缺失与「表整张不认识」是同一个病因：程序比库新。

    处置动作是唯一的 —— 先跑迁移 —— 而旧文案只说了「有哪些列没有」，
    于是最可能的反应是「归档坏了，重传一份」；而重传的归档有完全相同的问题，
    于是又重传一次。**报错文案不给出动作，就会把人引向错误的循环。**
    """
    p = tmp_path / "newer.json.gz"
    p.write_bytes(_archive_bytes(version=export_mod.VERSION, tables={
        "book": {"columns": ["id", "name", "不存在的列"], "rows": []},
    }))
    with pytest.raises(ExportRefused) as ei:
        export_mod.restore(db, p, dry_run=True)
    msg = str(ei.value)
    assert "不存在的列" in msg, "没说缺的是哪一列"
    assert "alembic upgrade head" in msg, "没说该执行哪条命令"
    assert "不要" in msg and "删" in msg, \
        "没拦住「把归档里的列删掉」这个会丢账的做法"


# ============================================================ 清理不能吞掉备份的成功
#
# 下面这一整组照抄的是一次真实事故：一轮 24 个目标全部上传成功，界面上却显示
# 「整轮失败」，而真实原因是一个 ``AttributeError`` —— 它在清理阶段抛出，
# 冒到最外层之后被兜底成「后台任务异常，见服务端日志」九个字。
#
# 两个缺陷叠在一起才形成这个形态，所以用例也按这两个方向写：
#   1. 清理本身崩了（类型契约不成立 —— oss2 给的是 int 纪元秒）；
#   2. 清理崩了之后，清理的失败吞掉了备份的成功。


class _FakeOss2:
    """内存版 oss2，**连字段类型都照抄真的**。

    ``oss2.xml_utils.parse_list_objects_v2`` 是这么写的::

        SimplifiedObjectInfo(..., iso8601_to_unixtime(_find_tag(contents_node, 'LastModified')), ...)

    而 :func:`oss2.utils.iso8601_to_unixtime` 返回 ``calendar.timegm`` 的结果 ——
    **int 纪元秒**。同一个字段：``head_object`` 那条路给 datetime，
    列表这条路给 int。

    之前所有测试替身都只递 datetime，于是这个分叉一次也没被踩到 ——
    **替身越顺手，边界越没人测。**
    """

    class exceptions:                       # noqa: N801 - 照抄 SDK 的命名
        OssError = type("OssError", (Exception,), {})
        NoSuchKey = type("NoSuchKey", (Exception,), {})

    def __init__(self, now=NOW):
        # 与 run_backup(now=NOW) 共用时钟；不能让日期流逝改变对象年龄。
        self.now = now
        self.objects: dict[str, bytes] = {}
        self.mtimes: dict[str, int] = {}          # 每个对象各自的「写下的时刻」
        self.deleted: list[str] = []

    def seed(self, key: str, data: bytes, age_days: float) -> None:
        """先摆一个 age_days 天前就在 Bucket 里的对象。"""
        self.objects[key] = data
        self.mtimes[key] = int(
            (self.now - timedelta(days=age_days)).timestamp())

    def Auth(self, ak, sk):                 # noqa: N802 - 照抄 SDK 的命名
        return object()

    def Bucket(self, auth, endpoint, bucket, connect_timeout=None):    # noqa: N802
        return self

    def put_object(self, key, data):
        self.objects[key] = data
        # 刚写下的就是「现在」。少了这一步，这个替身会把本轮刚上传的对象也
        # 标成陈旧品，于是它们立刻被自己删掉 —— 一个只让测试通过、自己跑不通
        # 的替身，比没有替身更糟。
        self.mtimes[key] = int(self.now.timestamp())
        return SimpleNamespace(etag="etag")

    def put_object_from_file(self, key, filename):
        """备份走的是**按文件**上传（不把整份 gzip 读进内存）。

        **这里必须照 SDK 的契约写，不能照实现写。** oss2 的签名是
        ``put_object_from_file(key, filename)``，第二个参数是**本地文件路径**。
        当年实现里传的是文件句柄，这个替身也就跟着收句柄 —— 两边一起错，
        于是「每天都在失败的离站备份」在测试里一路绿灯。

        替身照契约写（并且只收路径），实现传错参数时就会当场炸出来。
        """
        with open(filename, "rb") as handle:
            self.objects[key] = handle.read()
        self.mtimes[key] = int(self.now.timestamp())
        return SimpleNamespace(etag="etag")

    def delete_object(self, key):
        self.objects.pop(key, None)
        self.mtimes.pop(key, None)
        self.deleted.append(key)

    def ObjectIteratorV2(self, bucket, prefix):    # noqa: N802
        return [SimpleNamespace(key=k, size=len(v), last_modified=bucket.mtimes[k])
                for k, v in sorted(bucket.objects.items()) if k.startswith(prefix)]


@pytest.fixture
def oss_sdk(monkeypatch, db):
    """把**整个 oss2** 换成内存实现，让 run_backup 走真代码路径。

    与上面那个 :func:`oss` fixture 的区别是刻意的：``oss`` 把
    ``retention.iter_objects`` 整个替换掉了，于是恰好绕开出问题的那一层；
    这里只换 SDK，``oss_client.iter_objects`` → ``retention`` 这条真实链路
    会被完整跑到 —— 事故就发生在这一段上。
    """
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-heyuan.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct963"), ("oss_prefix", "acct-backup"),
                       ("backup_include_system", "1"),
                       ("backup_keep_daily_days", "30"),
                       ("backup_keep_weekly_days", "365")):
        system_settings.set_value(db, key, value)
    db.commit()

    fake = _FakeOss2()
    monkeypatch.setattr(oss_mod, "_import_oss2", lambda: fake)
    monkeypatch.setattr(oss_mod, "make_bucket", lambda cfg, **kw: fake)
    return fake


def test_整轮不该因为清理崩掉而报失败(db, book, admin_user, oss_sdk):
    """一次真实事故的形状：上传全成功，却因为清理抛异常被记成「整轮失败」。

    修法不能是「把清理包成什么都不做」—— 下一条用例盯着清理真的发生。
    """
    result = service.run_backup(db, actor=admin_user, now=NOW)

    assert result.ok, [(r.scope, r.status, r.message[:200]) for r in result.runs]
    assert not result.failed, [r.message[:200] for r in result.failed]
    assert result.prune_error is None, result.prune_error


def test_清理真的发生了_没有退化成静默空操作(db, book, admin_user, oss_sdk):
    """过期 40 天的日档必须真的被删掉。

    上一条不能靠「清理改成 no-op」通过 —— 那样只是把「误报失败」换成了
    「清理从此不工作」，而后者更安静：退路越攒越多，没人知道。
    """
    stale = "acct-backup/daily/book-999/20200101-000000.json.gz"
    oss_sdk.seed(stale, b"old daily", age_days=40)

    result = service.run_backup(db, actor=admin_user, now=NOW)

    assert [d.key for d in result.deleted] == [stale]
    assert oss_sdk.deleted == [stale]
    assert [d.age_days for d in result.deleted][0] == pytest.approx(40, abs=1)


def test_清理失败是另一件事_不该把整轮判成失败(db, book, admin_user, oss, monkeypatch):
    """清理失败是**另一件事**，它不能吞掉已经传上去的数据。

    而且它必须有地方可查 —— 否则就是「静默地不清理」：退路窗口越缩越短，
    而界面上永远一片绿。
    """
    def boom(bucket, prefix, keep_days, now=None, dry_run=False):
        raise oss_mod.OssError("列举 acct-backup/daily/ 失败：AccessDenied")

    monkeypatch.setattr(retention, "apply_retention", boom)
    result = service.run_backup(db, actor=admin_user, now=NOW)

    assert result.ok, [(r.status, r.message[:160]) for r in result.runs]
    assert result.prune_error and "AccessDenied" in result.prune_error
    assert result.deleted == []


def test_清理失败要留审计痕迹(db, book, admin_user, oss, monkeypatch):
    """清理失败要能在审计日志里查到 —— 那是运维唯一会主动翻的地方。"""
    def boom(bucket, prefix, keep_days, now=None, dry_run=False):
        raise oss_mod.OssError("列举 acct-backup/daily/ 失败：AccessDenied")

    monkeypatch.setattr(retention, "apply_retention", boom)
    service.run_backup(db, actor=admin_user, now=NOW)

    row = db.scalars(select(AuditLog)
                     .where(AuditLog.action == "BACKUP_RUN")
                     .order_by(AuditLog.id.desc())).first()
    assert row is not None
    assert "AccessDenied" in row.detail, row.detail


def test_判龄看到不像时间的东西时宁可多留():
    """类型不对时按「刚写下」处理，而不是让判龄本身抛异常。

    判龄崩掉的后果是**整轮备份被判失败** —— 一份备份的年龄算不出来，
    和这份备份传没传上去本来毫无关系。
    """
    now = datetime.now(timezone.utc)
    objs = [RemoteObject(key="p/daily/book-1/a.json.gz", size=1,
                         last_modified=1792215388)]
    assert retention.plan_deletions(objs, "daily", 30, now) == []


@pytest.mark.parametrize("raw, want_epoch", [
    (1792215388, 1792215388),                                  # oss2 列表接口：int
    (datetime(2026, 10, 17, 5, 36, 28), 1792215388 - 0),        # naive：按 UTC 解
])
def test_iter_objects_把_sdk_给的各种时间形状归一成_datetime(monkeypatch, raw, want_epoch):
    """``RemoteObject.last_modified`` 声明的是 datetime，就得永远是 datetime。

    类型契约写在 dataclass 上不等于它成立 —— 值是 SDK 递进来的，而 SDK 在
    列表和 HEAD 两条路上给的就是两种类型。把归一放在**边界**这一处，
    下游（判龄）才不用为每一种 SDK 版本的分叉各写一遍。
    """
    fake = SimpleNamespace(
        ObjectIteratorV2=lambda bucket, prefix: iter([
            SimpleNamespace(key="p/daily/book-1/a.json.gz", size=7, last_modified=raw)]),
        exceptions=SimpleNamespace(OssError=type("OssError", (Exception,), {})),
    )
    monkeypatch.setattr(oss_mod, "_import_oss2", lambda: fake)

    (obj,) = list(oss_mod.iter_objects(object(), "p/daily/"))

    assert isinstance(obj.last_modified, datetime), \
        f"last_modified 还是 {type(obj.last_modified).__name__}，判龄会崩"
    assert obj.last_modified.tzinfo is not None, "naive 时间会被当成东八区"
    assert abs(obj.last_modified.timestamp() - want_epoch) < 1


# ------------------------------------------------------------ 内网 Endpoint


class _RefusingBucket(_FakeOss2):
    """建连就失败，且抛的是真 SDK 那个异常类。"""

    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint

    def Bucket(self, auth, endpoint, bucket, connect_timeout=None):    # noqa: N802
        raise self.exceptions.OssError(
            f"RequestError: connect timeout=60 to {endpoint} refused")


def _endpoint_note_for(monkeypatch, endpoint):
    fake = _RefusingBucket(endpoint)
    monkeypatch.setattr(oss_mod, "_import_oss2", lambda: fake)
    cfg = {"endpoint": endpoint, "access_key_id": "AK",
           "access_key_secret": "SK", "bucket": "acct963"}
    with pytest.raises(oss_mod.OssError) as ei:
        oss_mod.make_bucket(cfg)
    return str(ei.value)


def test_内网端点的失败要说明只能在阿里云内网用(monkeypatch):
    """``-internal`` 那个坑非常常见：控制台上它就贴在公网 Endpoint 旁边。

    配错了的后果不是「连不上」这么轻描淡写：每个对象先干等一个连接超时，
    一轮几十本就是几十分钟；而报错里只有一个没人认识的主机名。
    """
    msg = _endpoint_note_for(monkeypatch, "acct963.oss-cn-heyuan-internal.aliyuncs.com")
    assert "VPC" in msg, msg


def test_公网端点不该被安上内网提示(monkeypatch):
    """提示只在真的用了内网端点时出现，否则就是噪音，久了没人看。"""
    msg = _endpoint_note_for(monkeypatch, "acct963.oss-cn-heyuan.aliyuncs.com")
    assert "VPC" not in msg, msg


# ------------------------------------------------------------ 整轮失败记录


def test_整轮失败记录要带上真实原因(db, client, auth, monkeypatch):
    """「后台任务异常，见服务端日志」在排障时等于没写。

    这次事故里它让人以为「备份失败了」，而真实原因只在服务端日志里躺着 ——
    记录本身一个字都不肯说，于是排障从「读记录」变成了「考古日志」。
    """
    from app.api import system as system_api
    from app.core import system_settings

    for key, value in (("oss_endpoint", "oss-cn-heyuan.aliyuncs.com"),
                       ("oss_access_key_id", "AK"), ("oss_access_key_secret", "SK"),
                       ("oss_bucket", "acct963")):
        system_settings.set_value(db, key, value)
    db.commit()

    def boom(session, actor=None, **kw):
        raise RuntimeError("apply_retention: 'int' object has no attribute 'tzinfo'")

    monkeypatch.setattr(system_api.backup_service, "run_backup", boom)
    client.post("/api/system/backup/run", headers=auth)

    db.expire_all()
    row = db.scalars(select(BackupRun)
                     .where(BackupRun.book_label == "（整轮）")
                     .order_by(BackupRun.id.desc())).first()
    assert row is not None, "整轮失败没有落记录"
    assert "tzinfo" in row.message, row.message
    assert "见服务端日志" not in row.message, row.message


# ------------------------------------------------------- 传输必须是 https（P1-1）
#
# **这里曾经把整份备份放在明文上。** 原来的归一化把 ``https://`` 与
# ``http://`` 一起剥掉，交给 oss2 的是裸 host；而 oss2 看到没有 scheme
# 会**自己补 ``http://``**（``oss2/api.py::_normalize_endpoint``）。
# 于是配置里明明写着 ``https://``，实际发出去的是明文 ``http://``：
# 备份正文（全账套凭证、金额、用户口令散列）明文过网，``Authorization``
# 头里的 AccessKey ID 与请求签名也明文过网 —— 而那个签名是针对该请求
# 算的，捕获之后可以**重放同一个 PUT**，覆盖 OSS 上的备份对象。
#
# 所以这里的断言对象是**实际请求 URL 的 scheme**，不是配置里的字符串：
# 配置字符串对了不代表发出去的对。


def test_归一化保留scheme并默认https():
    """缺省按 https 处理；**只去协议、不去区域**。"""
    assert oss_mod.normalize_endpoint("oss-cn-hangzhou.aliyuncs.com") == \
        "https://oss-cn-hangzhou.aliyuncs.com"
    assert oss_mod.normalize_endpoint("https://oss-cn-hangzhou.aliyuncs.com") == \
        "https://oss-cn-hangzhou.aliyuncs.com"
    # 区域是寻址的一部分：去掉 ``cn-hangzhou`` 会连到别的地址上去。
    assert "cn-hangzhou" in oss_mod.normalize_endpoint("https://oss-cn-hangzhou.aliyuncs.com")
    assert oss_mod.normalize_endpoint("https://oss-cn-hangzhou.aliyuncs.com/") == \
        "https://oss-cn-hangzhou.aliyuncs.com"
    # 显式写了 http:// 的**原样保留**（由 ensure_secure_endpoint 去拒）
    assert oss_mod.normalize_endpoint("http://oss-cn-hangzhou.aliyuncs.com") == \
        "http://oss-cn-hangzhou.aliyuncs.com"


def test_实际请求走https():
    """**用假 session 拦截请求**，断言实际 URL 的 scheme 是 https。

    断言的对象是**真正会发出去的那个请求**，不是配置 —— 配置对了、发出去
    的不对，是这类缺陷最常见的形态（原来的 bug 正是这样：配置写着 https，
    代码把它降级成了 http）。
    """
    import oss2

    seen: dict = {}

    class _FakeSession:
        def do_request(self, req, timeout=None, **kw):
            seen["url"] = req.url
            seen["method"] = req.method
            raise RuntimeError("测试到此为止：不发真的请求")

    bucket = oss2.Bucket(
        oss2.Auth("ak", "sk"),
        oss_mod.normalize_endpoint("oss-cn-hangzhou.aliyuncs.com"),
        "example-bucket",
        session=_FakeSession(),
    )
    with pytest.raises(Exception):
        bucket.put_object("acct-backup/daily/x.json.gz", b"payload")

    assert seen, "请求根本没发出去 —— 这个用例没有测到任何东西"
    assert seen["url"].startswith("https://"), (
        f"实际请求走的是明文：{seen['url']} —— "
        "备份正文与 Authorization 头都会明文过网")


def test_显式http的endpoint被拒绝():
    """**明确拒绝**明文 endpoint，并给出「改成什么」的迁移提示。"""
    with pytest.raises(oss_mod.OssError) as ei:
        oss_mod.ensure_secure_endpoint("http://oss-cn-hangzhou.aliyuncs.com")
    msg = str(ei.value)
    assert "https" in msg, msg


def test_make_bucket拒绝明文endpoint():
    """出网路径的**唯一收口** —— 建句柄时就拒，不会等到数据已经传出去。"""
    cfg = {"endpoint": "http://oss-cn-hangzhou.aliyuncs.com", "bucket": "b",
           "access_key_id": "k", "access_key_secret": "s"}
    with pytest.raises(oss_mod.OssError) as ei:
        oss_mod.make_bucket(cfg)
    assert "https" in str(ei.value)


def test_内网端点提示在带scheme之后仍然有效():
    """``-internal`` 的判断要看**主机名**，加了 scheme 之后不能失效。"""
    assert oss_mod.is_internal_endpoint("oss-cn-heyuan-internal.aliyuncs.com")
    assert oss_mod.is_internal_endpoint("https://oss-cn-heyuan-internal.aliyuncs.com")
    assert not oss_mod.is_internal_endpoint("oss-cn-heyuan.aliyuncs.com")
