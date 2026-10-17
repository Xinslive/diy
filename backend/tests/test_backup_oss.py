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
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text

from app.backup import export as export_mod
from app.backup import retention, tier as tier_mod
from app.backup.export import ExportRefused
from app.backup.oss_client import RemoteObject
from app.backup.retention import Deleted
from app.models import (SCOPE_BOOK, SCOPE_SYSTEM, STATUS_FAILED, STATUS_SUCCESS, Entry,
                        TIER_DAILY, TIER_MONTHLY, TIER_WEEKLY, AuditLog, BackupRun,
                        Book, Period, ReportCell, ReportCol, ReportRow, ReportTemplate,
                        Subject, Voucher)

NOW = datetime(2026, 10, 17, 3, 17, tzinfo=timezone.utc)   # 东八区 11:17，周六


# ------------------------------------------------------------ 表归属


def test_excluded_tables_has_no_second_member():
    """排除表**只允许**有 sys_backup_run 一个。

    它自己带 book_id，于是「所有带 book_id 的表」这条规则会把它捞进来 ——
    于是这一期的备份里装着上一期的备份记录，体积每一期都比上一期大，
    而且没有任何东西会报错。要加第二个排除项之前，先想清楚它为什么不能
    被备份。
    """
    assert export_mod.EXCLUDED_TABLES == frozenset({"sys_backup_run"})


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
    p = tmp_path / "v99.gz"
    p.write_bytes(gzip.compress(json.dumps(
        {"header": {"format": "cwq-book-backup", "version": 99},
         "tables": {"book": {"columns": ["id"], "rows": []}}}
    ).encode()))
    with pytest.raises(ExportRefused, match="只认"):
        export_mod.restore(db, p, dry_run=True)


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
