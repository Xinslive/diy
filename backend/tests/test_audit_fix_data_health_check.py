"""审计修复 §7 第 1 步第 11 项：**存量数据体检**要有一条可执行的命令。

**为什么要有这个文件。**

审计报告路线图第 1 步第 11 项写着：

    11. 先做一次存量数据体检：查 entry 是否已存在同一 voucher_id 下 line_no
        重复的行（§3.15 场景 B 已经能落库）。有重复则先人工清理 ——
        这一步是第 12 项的前置。

这一步**本身**是在真实库上跑的一次性人工动作（代码替不了「该留哪一条」那个
决定）。但「查」这一步替得了 —— 而且**必须**替：验收记录里如果只写
「上线前先在真实库跑一次查询」而不给出那条查询，运维就得临场自己写 SQL，
写出来的那条 SQL 与迁移前置检查用的那条**很可能不是同一口径**，
于是出现最坏的一种情况：体检说干净，迁移撞上重复行炸了。

所以这里把「怎么做」落成了一条命令，并把三件事钉死：

  1. 体检脚本**查得出**重复行号，并**点名**是哪张凭证、哪个行号；
  2. 体检脚本与迁移 0038 的前置检查**共用同一份判据**（§4.5：同一个量
     只允许一个权威实现 —— 体检说干净、迁移说有问题，比不说还糟）；
  3. 体检脚本**只读**：跑完之后库里的一切逐字节不变。

**这些用例里 DDL 都不提交。** PostgreSQL 的 DDL 是事务性的，
最后那次 ``rollback()`` 会把「摘掉唯一约束」和「插进去的重复行」
一起还原 —— 不会给后面的用例留一个**没有唯一约束**的库。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "data_health_check.py"
MIGRATION = BACKEND / "migrations" / "versions" / "0038_entry_voucher_line_unique.py"


def _load(path: Path, name: str):
    """按路径加载模块（文件名不是合法标识符，import 语句写不出来）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_script():
    return _load(SCRIPT, "data_health_check")


def _load_migration():
    return _load(MIGRATION, "0038_entry_voucher_line_unique")


def _drop_unique_constraint(db):
    """摘掉唯一约束（**不提交** —— 回滚时一起还原）。

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


def _make_duplicate(db, make_voucher, offset: int = 100000):
    """造一组重复行号，返回 (voucher_id, line_no)。

    **先建凭证再摘约束**：``make_voucher`` 自己会提交，放在它前面的话
    这次 DDL 会被一起提交掉（回滚就还原不回来了）。
    """
    v = make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    eid, line_no = db.execute(
        text("SELECT id, line_no FROM entry WHERE voucher_id = :v ORDER BY line_no LIMIT 1"),
        {"v": v.id}).one()
    _drop_unique_constraint(db)
    _clone_entry(db, eid, offset)
    return v.id, int(line_no)


# --------------------------------------------------------------------- 判据本身


def test_干净库里没有重复行号(db, book, make_voucher):
    """对照组：正常的库 → 判据安静返回空。"""
    from app.accounting.data_health import duplicate_line_no

    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])
    assert duplicate_line_no(db.connection()) == []


def test_有重复行号时判据查得出来并点名(db, book, make_voucher):
    """有重复 → 判据要报出**是哪张凭证、哪个行号、几份**。"""
    from app.accounting.data_health import describe_duplicate, duplicate_line_no

    voucher_id, line_no = _make_duplicate(db, make_voucher)
    dups = duplicate_line_no(db.connection())
    assert len(dups) == 1, dups
    vid, lines, n = dups[0]
    assert vid == voucher_id
    assert lines == [line_no]
    assert n == 2
    # 点名的文案要能直接照着去查那张凭证
    text_out = describe_duplicate(dups[0])
    assert f"voucher_id={voucher_id}" in text_out
    assert f"行号 [{line_no}]" in text_out
    db.rollback()


def test_按账套过滤只看得见那本账(db, book, make_voucher):
    """``book_id`` 给了就只看那本账 —— 体检范围要能被圈定。"""
    from app.accounting.data_health import duplicate_line_no

    voucher_id, _line_no = _make_duplicate(db, make_voucher)
    assert duplicate_line_no(db.connection(), book_id=book.id)
    assert duplicate_line_no(db.connection(), book_id=book.id + 9999) == []
    assert voucher_id
    db.rollback()


# --------------------------------------------------------------------- 三处同一份


def test_迁移前置检查与体检脚本共用同一份判据(db, monkeypatch):
    """**同一个量只允许一个权威实现**（审计报告 §4.5）。

    这条钉的是「迁移的前置检查真的去调了 app 侧那份判据」，而不是
    「迁移里恰好也有一份差不多的查询」。

    验证方式：把 app 侧的判据换成一个假的，看迁移的前置检查会不会跟着变。
    换成假的返回空 → 前置检查安静通过；换成假的返回有重复 → 前置检查炸。
    两次都成立，就说明迁移**没有自己那份查询**。
    """
    from app.accounting import data_health

    mod = _load_migration()

    seen = []

    def fake(bind, **kw):
        seen.append(bind)
        return []

    monkeypatch.setattr(data_health, "duplicate_line_no", fake)
    mod._preflight(bind=db.connection())
    assert seen, "迁移的前置检查没有走 app.accounting.data_health.duplicate_line_no"

    monkeypatch.setattr(
        data_health, "duplicate_line_no", lambda bind, **kw: [(7, [1], 2)])
    with pytest.raises(RuntimeError):
        mod._preflight(bind=db.connection())


def test_迁移报错与体检脚本点名的是同一件事(db, book, make_voucher):
    """两边的文案必须指向同一张凭证、同一个行号。

    这两个入口会在**不同的时候**被人看到：体检是上线前看的，迁移报错是
    上线时炸出来的。如果它们点名的口径不一样，运维会怀疑「到底是哪一个
    说错了」—— 而两个其实都没错，只是各说各的。
    """
    from app.accounting.data_health import describe_duplicate, duplicate_line_no

    mod = _load_migration()
    voucher_id, line_no = _make_duplicate(db, make_voucher)

    dups = duplicate_line_no(db.connection())
    assert dups, "造出来的重复行没查出来"

    with pytest.raises(RuntimeError) as ei:
        mod._preflight(bind=db.connection())
    msg = str(ei.value)
    assert describe_duplicate(dups[0]) in msg, msg
    assert f"voucher_id={voucher_id}" in msg
    assert f"行号 [{line_no}]" in msg
    db.rollback()


# --------------------------------------------------------------------- 脚本


def test_体检脚本在干净库上通过(db, book, make_voucher, capsys):
    """干净的库 → 退出码 0，输出说「通过」。"""
    mod = _load_script()
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    rc = mod.main(["--book-id", str(book.id)], session_factory=lambda: db)
    out = capsys.readouterr().out
    assert rc == mod.EXIT_OK, out
    assert "没有重复行号" in out, out
    assert "体检通过" in out, out


def test_体检脚本在有重复行号时阻断上线(db, book, make_voucher, capsys):
    """有重复 → 退出码是**阻断**，输出点名那张凭证。"""
    mod = _load_script()
    voucher_id, line_no = _make_duplicate(db, make_voucher)

    rc = mod.main(["--book-id", str(book.id)], session_factory=lambda: db)
    out = capsys.readouterr().out
    assert rc == mod.EXIT_BLOCKING, out
    assert f"voucher_id={voucher_id}" in out, out
    assert f"行号 [{line_no}]" in out, out
    assert "阻断上线" in out, out
    db.rollback()


def test_体检脚本只读_跑完库里什么都没变(db, book, make_voucher):
    """体检**一条写操作都没有** —— 跑完之后库里逐字节不变。

    这个性质有用：它证明体检本身没有污染被体检的对象。
    一个会顺手改点东西的体检脚本，用在生产库上就是另一回事了。
    """
    mod = _load_script()
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    def snapshot():
        return (
            db.execute(text("SELECT count(*) FROM entry")).scalar(),
            db.execute(text("SELECT coalesce(sum(amount), 0) FROM entry")).scalar(),
            db.execute(text("SELECT count(*) FROM voucher")).scalar(),
            db.execute(text("SELECT count(*) FROM period")).scalar(),
        )

    before = snapshot()
    rc = mod.main(["--book-id", str(book.id)], session_factory=lambda: db)
    after = snapshot()
    assert rc == mod.EXIT_OK
    assert before == after, f"体检改动了库：{before} → {after}"


def test_体检脚本不碰约束_跑完唯一约束还在原样(db, book, make_voucher):
    """体检**不许**顺手把唯一约束加上/摘掉。

    理由：体检是**上线前**跑的，而唯一约束是**迁移**该干的活。
    体检擅自把约束加上，迁移再跑一遍就会撞 DuplicateTable；
    体检擅自把约束摘掉，生产库就失去了那道护栏。
    """
    mod = _load_script()
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    def constraint_names():
        return {r[0] for r in db.execute(text("""
            SELECT conname FROM pg_constraint WHERE conrelid = 'entry'::regclass
        """)).all()}

    before = constraint_names()
    mod.main(["--book-id", str(book.id)], session_factory=lambda: db)
    assert constraint_names() == before


def test_整理就绪不干净的期间会被点名(db, book, make_voucher, capsys):
    """断号的期间 → 被点名，且**不**被当成阻断项。

    为什么这是「提示」而不是「阻断」：断号、乱序这些问题
    **迁移不会因此失败**，而「整理凭证」页面上就能修（重排一次）。
    真正卡住上线的是重复行号（迁移 0038 会直接拒绝加约束）。

    顺带验的是：体检用的**就是**「整理凭证」页面那个判据
    （``app.accounting.organize.check_period``），不是另写一份。
    """
    mod = _load_script()
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 voucher_no="记0001")
    make_voucher("2026-01-06", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 voucher_no="记0003")

    rc = mod.main(["--book-id", str(book.id)], session_factory=lambda: db)
    out = capsys.readouterr().out
    assert "记0002" in out, out
    assert rc == mod.EXIT_WARNING, out
    assert "需人工确认" in out, out


def test_strict档把提示项也当阻断(db, book, make_voucher):
    """``--strict`` → 提示项也返回阻断码。"""
    mod = _load_script()
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 voucher_no="记0001")
    make_voucher("2026-01-06", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 voucher_no="记0003")

    rc = mod.main(["--book-id", str(book.id), "--strict"], session_factory=lambda: db)
    assert rc == mod.EXIT_BLOCKING


def test_体检脚本跑不动时报的是_1_不是_2(db, book, monkeypatch):
    """体检自己坏了 → 退出码 1（**不是** 2）。

    这两个码必须分开：「库里有问题」（2）和「体检没跑成」（1）的动作
    完全不同 —— 前者要清理数据，后者要修脚本/修连接串。
    混成一个的后果是发布流程把「体检脚本自己坏了」当成「库有问题」，
    然后去**清理生产数据**。
    """
    mod = _load_script()

    def boom(db, book_id):
        raise RuntimeError("模拟：体检自己跑不动")

    monkeypatch.setattr(mod, "_check_duplicates", boom)
    rc = mod.main(["--book-id", str(book.id)], session_factory=lambda: db)
    assert rc == mod.EXIT_BROKEN


def test_体检脚本能输出机器可读的结果(db, book, make_voucher, capsys):
    """``--json`` → 结构化输出，给发布流程用。"""
    import json

    mod = _load_script()
    make_voucher("2026-01-05", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")])

    rc = mod.main(["--book-id", str(book.id), "--json"], session_factory=lambda: db)
    payload = json.loads(capsys.readouterr().out)
    assert rc == mod.EXIT_OK
    assert payload["duplicate_line_no"] == []
    assert payload["unclean_periods"] == []
    assert payload["blocking"] is False
    # 连接串必须脱敏 —— 这个输出会进日志
    assert "cwq123" not in json.dumps(payload, ensure_ascii=False)
