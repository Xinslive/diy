"""审计修复 §3.15 的第 2 步：迁移的**前置检查**要在有重复行号时大声失败。

**为什么要有这道检查。**

审计报告明确指出：**活库里可能已经有重复行** —— 那正是这个缺陷的症状
（并发改同一张凭证留下两组重复的行号，同一笔钱在账上记了两遍，
而借贷仍然平，所有自检都查不出来）。

而 ``ALTER TABLE ... ADD CONSTRAINT`` 撞上重复行时，PostgreSQL 报的是一句
英文的 ``could not create unique index ... duplicate key value``：里面只有
约束名和重复的那几个值 —— 运维看到它不知道该去清理**哪几张凭证**，
更不知道「重复行」意味着账上那笔钱被记了两遍。

所以迁移 0038 **先自己查一遍**，把话说清楚再拒绝：
列出每张出问题的凭证（voucher_id）与重复的行号，并说清这些行是怎么来的、
该怎么处理。**清理数据是加约束的前置条件**（审计报告 §7 第 1 步第 11 项），
不是「迁移顺手替人清理」—— 哪一条重复该留、哪一条该改金额，
只有懂业务的人能决定。

**这些用例里 DDL 都不提交。** PostgreSQL 的 DDL 是事务性的，
最后那次 ``rollback()`` 会把「摘掉唯一约束」和「插进去的重复行」
一起还原 —— 所以这几个用例不会给后面的用例留一个**没有唯一约束**的库。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

BACKEND = Path(__file__).resolve().parents[1]
MIGRATION = BACKEND / "migrations" / "versions" / "0038_entry_voucher_line_unique.py"


def _load_migration():
    """按路径加载那条迁移（文件名以数字开头，import 语句写不出来）。"""
    spec = importlib.util.spec_from_file_location(
        "0038_entry_voucher_line_unique", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drop_unique_constraint(db):
    """摘掉刚加的唯一约束（**不提交** —— 回滚时一起还原）。

    顺序要紧：``ALTER TABLE`` 不接受有挂起的延迟触发器事件的表，
    所以必须先改约束、后插行。
    """
    db.execute(text("ALTER TABLE entry DROP CONSTRAINT uq_entry_voucher_line_no"))


def _clone_entry(db, entry_id: int, id_offset: int) -> None:
    """照抄一行分录（**同样的行号**）—— 这就是并发改凭证留下的那种脏数据。"""
    db.execute(text("""
        INSERT INTO entry (id, voucher_id, book_id, period_ord, line_no,
                           subject_id, direction, amount, summary,
                           created_at, updated_at)
        SELECT id + :off, voucher_id, book_id, period_ord, line_no,
               subject_id, direction, amount, summary, now(), now()
          FROM entry WHERE id = :eid
    """), {"eid": entry_id, "off": id_offset})


def test_迁移带前置检查(db):
    """迁移文件里必须有那道检查，而且它要**认得出**重复的行号。"""
    mod = _load_migration()
    assert hasattr(mod, "_preflight"), "迁移里必须有前置检查"
    # 干净的库 → 检查通过，什么都不说
    mod._preflight(bind=db.connection())


def test_有重复行号时前置检查大声失败(db, book, make_voucher):
    """重复行号存在 → 报错，且**点名是哪几张凭证**、哪几个行号。"""
    mod = _load_migration()
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    row = db.execute(
        text("SELECT id, line_no FROM entry WHERE voucher_id = :v ORDER BY line_no LIMIT 1"),
        {"v": v.id}).one()
    eid, line_no = row

    _drop_unique_constraint(db)
    _clone_entry(db, eid, 100000)

    with pytest.raises(RuntimeError) as ei:
        mod._preflight(bind=db.connection())
    msg = str(ei.value)
    # 报错必须**点名是哪几张凭证**、哪几个行号，还要说清怎么处理
    assert f"voucher_id={v.id}" in msg, msg
    assert f"行号 [{line_no}]" in msg, msg
    assert "清理" in msg, msg

    db.rollback()


def test_重复行号检查认得出全部重复(db, book, make_voucher):
    """两张凭证各有重复 → 两张都要点名（不能只报第一张）。"""
    mod = _load_migration()
    ids = []
    entry_ids = []
    for day in ("2026-01-05", "2026-01-06"):
        v = make_voucher(day, dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
        eid = db.execute(
            text("SELECT id FROM entry WHERE voucher_id = :v ORDER BY line_no LIMIT 1"),
            {"v": v.id}).scalar()
        ids.append(v.id)
        entry_ids.append(eid)

    # 约束在两张凭证都建好之后再摘 —— ``make_voucher`` 自己会提交，
    # 放在它前面的话这次 DDL 会被一起提交掉（回滚就还原不回来了）。
    _drop_unique_constraint(db)
    for eid in entry_ids:
        _clone_entry(db, eid, 200000)

    with pytest.raises(RuntimeError) as ei:
        mod._preflight(bind=db.connection())
    msg = str(ei.value)
    for vid in ids:
        assert f"voucher_id={vid}" in msg, msg
    db.rollback()


def test_库里本来就没有重复行号(db, book, make_voucher):
    """对照组：正常的库（并发没发生过）→ 检查安静通过。"""
    mod = _load_migration()
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    assert mod._duplicate_line_no(bind=db.connection()) == []
    mod._preflight(bind=db.connection())
    # 唯一约束还在（上面那两条用例的 DDL 已随回滚还原）
    names = db.execute(text("""
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'entry'::regclass AND conname = 'uq_entry_voucher_line_no'
    """)).all()
    assert names, "唯一约束不见了"
    assert db.scalar(
        text("SELECT count(*) FROM entry WHERE voucher_id = :v"), {"v": v.id}) == 2
