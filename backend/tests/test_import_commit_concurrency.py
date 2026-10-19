"""同一个导入批次并发提交，只能导入一次（审计 P1-03）。

``POST /import/{id}/commit`` 原来是裸的 ``db.get(ImportBatch, batch_id)``：
无锁。读出来的 ``status`` 是**那一刻**的快照，而 ``importer.commit`` 里那句
``if batch.status != "PENDING"`` 判的就是这份快照。两个并发的提交（用户手快
按了两次确认；或者一个人提交、另一个人同时点了确认）会双双读到 PENDING、双双
通过检查。

后果分两种，差别很大：

* 工作簿**带凭证号**时，``uq_voucher_period_no`` 把第二边顶回去 —— 响亮地失败，
  至少不脏。这是运气好，不是设计。
* 工作簿**不带凭证号**时（导入器里是 ``row_no or None``，很常见），两边各自
  ``next_voucher_no`` 取「本期最大号 + 1」。READ COMMITTED 下第二个事务看得见
  第一个刚提交的凭证，于是拿到**另一个**号 —— 约束不触发，两批凭证安静地重复
  入库，``committed_count`` 还各自报「成功导入 N 张」。用户点一次确认，账上多出
  一份，而界面上没有任何异常。

所以这里钉的是**后果**：并发提交之后，账上的凭证数必须恰好等于成功那一次导入
的张数，不许是它的两倍。

用两条用例覆盖两个层次：

* ``test_提交期间必须持有批次行的排他锁`` —— 直接验「第二个提交者拿不到那把锁」。
  做法是让第一个请求卡在写入阶段（此时它持有路由取的那把行锁），再用第二个
  连接带 ``lock_timeout`` 去取同一把锁。**先证明两个会话真的是并发的**（不带锁的
  普通读立刻成功），再证明带锁的读被挡住 —— 不是「断言某段 SQL 被执行过」。
* ``test_两个并发提交只会导入一次`` —— 完整的竞态：第二个请求先读完批次就停下，
  等第一个请求整个跑完再继续。这正是缺陷描述里的时序。没有锁时第二个拿着过期的
  ``PENDING`` 快照继续写，于是拿到不同的凭证号、安静地重复入库；加了锁之后它
  只能等第一个提交完，锁释放时重新读到 COMMITTED，于是拿到一个 4xx。
"""
from __future__ import annotations

import io
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.accounting import importer as svc_import
from app.api.vouchers import commit_import
from app.core.db import SessionLocal
from app.models import Book, ImportBatch, SysUser, Voucher

#: 工作簿刻意**不带凭证号**：带号时唯一约束会响亮地挡住第二边，
#: 缺陷描述的「静默重复」要靠不带号才复现得出来。
_ROWS = [
    ["2026-01-05", "", "收租金", "1002", 0, 1000],
    ["", "", "收租金", "5001", 1000, 0],
    ["2026-01-06", "", "付水电", "560206", 500, 0],
    ["", "", "付水电", "1002", 0, 500],
]
_EXPECTED = 2          # 两张凭证
_HEADER = ["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"]

#: 「慢」的那个线程等对手多久。必须足够长，让对手在**没有锁**的旧实现下
#: 真的能跑完整个提交（那才是缺陷复现的时序）；而在修好的实现下，对手被行锁
#: 挡住、这个等待必然走满 —— 代价就是这条用例慢几秒，换来的是确定性。
_WAIT_SECONDS = 3.0


def _xlsx_no_voucher_no() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(_HEADER)
    for row in _ROWS:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def pending_batch(db, book, admin_user):
    """一个已暂存待确认的导入批次。"""
    batch = svc_import.stage(
        db, book.id, admin_user, "并发导入.xlsx", _xlsx_no_voucher_no())
    assert batch.status == "PENDING"
    assert batch.ok_count == _EXPECTED, \
        f"前置：这份工作簿应当解析出 {_EXPECTED} 张可用凭证，实际 {batch.ok_count}"
    db.expire_all()
    return batch


def _voucher_count(db, book_id: int) -> int:
    return int(db.scalar(select(func.count()).select_from(Voucher)
                         .where(Voucher.book_id == book_id)) or 0)


def _run_commit(batch_id: int, book_id: int, user_id: int, name: str,
                out: dict) -> None:
    """在**自己的**连接上跑一次真实的提交（直接调路由函数，不绕 HTTP）。

    直接调而不是 ``TestClient``：要的是两个真正并发的数据库事务，
    而 TestClient 的请求生命周期由框架掌控，插不进「第二个请求先读完批次、
    再等第一个提交完」这种编排。
    """
    s = SessionLocal()
    try:
        book = s.get(Book, book_id)
        user = s.get(SysUser, user_id)
        try:
            out[name] = ("ok", commit_import(
                batch_id, book=book, db=s, _link=None, user=user))
        except HTTPException as exc:
            out[name] = ("http", exc.status_code, exc.detail)
        except Exception as exc:                      # noqa: BLE001
            out[name] = ("exc", type(exc).__name__, str(exc))
    finally:
        s.close()


# --------------------------------------------------------------------- 契约


def test_顺序提交两次第二次被拒且不重复入库(client, auth, db, book, pending_batch):
    """最朴素的那一半：同一批次不能提交两次。

    这条在没有修复的旧实现上也通过（``importer.commit`` 的状态检查会挡），
    它的作用是把「4xx + 一张不多」这个对外契约写死，好让下面那条竞态用例
    只需要关心「并发」带来的差别。
    """
    url = f"/api/books/{book.id}/vouchers/import/{pending_batch.id}/commit"

    first = client.post(url, headers=auth)
    assert first.status_code == 200, first.text[:300]
    assert first.json()["created"] == _EXPECTED, first.text[:300]
    assert _voucher_count(db, book.id) == _EXPECTED

    second = client.post(url, headers=auth)
    assert 400 <= second.status_code < 500, \
        f"重复提交应当回 4xx，实际 {second.status_code}：{second.text[:300]}"
    db.expire_all()
    assert _voucher_count(db, book.id) == _EXPECTED, "被拒的提交又写了一遍"


# --------------------------------------------------------------------- 行锁


def test_提交期间必须持有批次行的排他锁(db, book, admin_user, pending_batch,
                                        monkeypatch):
    """第二个提交者必须**拿不到**那把锁 —— 这是并发提交唯一的闸门。

    验法分两步，缺一不可：

    1. 先证明两个连接真的是并发的（另一个会话的普通读**立刻**成功）——
       否则「被挡住」可能只是因为连接池把两个请求排成了串行。
    2. 再让第一个请求卡在写入阶段（此时它持有路由取的那把行锁），用第二个
       连接带 ``lock_timeout`` 去取同一把锁：必须超时。
       没有 ``FOR UPDATE`` 时这一步会立刻成功 —— 那就是缺陷本身。
    """
    entered = threading.Event()
    release = threading.Event()
    out: dict = {}
    real_commit = svc_import.commit

    def hold(db_, batch, user):
        # 已经走到写入阶段 = 路由里的批次行锁已经在本事务手上了
        entered.set()
        assert release.wait(timeout=30), "测试自己在等自己放开锁"
        return real_commit(db_, batch, user)

    monkeypatch.setattr(svc_import, "commit", hold)

    holder = threading.Thread(
        target=_run_commit,
        args=(pending_batch.id, book.id, admin_user.id, "holder", out),
        name="holder", daemon=True)
    holder.start()
    try:
        assert entered.wait(timeout=30), "第一个提交没有进入写入阶段"

        # ① 两个连接确实并发：普通读不会被对方的锁挡住
        probe = SessionLocal()
        try:
            plain = probe.get(ImportBatch, pending_batch.id)
            assert plain is not None, "前置：第二个连接应当能自由读到这行"
        finally:
            probe.close()

        # ② 但带锁的读拿不到 —— 说明第一个请求真的握着排他锁
        contender = SessionLocal()
        try:
            contender.execute(text("SET LOCAL lock_timeout = '750ms'"))
            with pytest.raises(OperationalError) as exc:
                contender.scalar(
                    select(ImportBatch).where(
                        ImportBatch.id == pending_batch.id).with_for_update())
            assert "lock" in str(exc.value).lower(), \
                f"应当是拿不到行锁，实际报的是：{exc.value}"
        finally:
            contender.rollback()
            contender.close()
    finally:
        release.set()
        holder.join(timeout=60)

    assert not holder.is_alive(), "持锁的线程没有退出，测试会挂住"
    assert out.get("holder", ("", ""))[0] == "ok", f"持锁那次提交应当成功：{out}"
    db.expire_all()
    assert _voucher_count(db, book.id) == _EXPECTED, "持锁那次提交导入的张数不对"


# --------------------------------------------------------------------- 真正的竞态


def test_两个并发提交只会导入一次(db, book, admin_user, pending_batch, monkeypatch):
    """缺陷本身的复现：第二个请求先读完批次，然后第一个请求整个跑完。

    这个时序就是「静默重复」的成因：第二个请求手里那份 ``status`` 是读批次
    那一刻的 PENDING，它不会重新读库；等它继续往下走到取号时，第一个请求已经
    提交了 —— READ COMMITTED 看得见那些新凭证，于是拿到**另一个**凭证号，
    唯一约束不触发，两批安静地入库。

    修好之后第二个请求在读完批次那一刻就握着行锁，第一个请求只能等它；
    反过来，它自己走到写入时会被第一个挡住并重新读到 COMMITTED，落在一个 4xx 上。
    """
    slow_at_write = threading.Event()
    fast_done = threading.Event()
    out: dict = {}
    real_commit = svc_import.commit

    def gated(db_, batch, user):
        if threading.current_thread().name == "slow":
            # 我已读完批次（无锁时就是一次裸 db.get），现在让对手先整个跑完
            slow_at_write.set()
            fast_done.wait(timeout=_WAIT_SECONDS)
        return real_commit(db_, batch, user)

    monkeypatch.setattr(svc_import, "commit", gated)

    slow = threading.Thread(
        target=_run_commit, name="slow", daemon=True,
        args=(pending_batch.id, book.id, admin_user.id, "slow", out))
    slow.start()
    assert slow_at_write.wait(timeout=30), "慢的那个提交没有进入写入阶段"

    fast = threading.Thread(
        target=_run_commit, name="fast", daemon=True,
        args=(pending_batch.id, book.id, admin_user.id, "fast", out))
    fast.start()
    fast.join(timeout=60)
    slow.join(timeout=60)

    assert not slow.is_alive() and not fast.is_alive(), f"有线程没退出：{out}"
    assert set(out) == {"slow", "fast"}, f"两个提交都要有结果：{out}"

    ok = [k for k, v in out.items() if v[0] == "ok"]
    rejected = [(k, v) for k, v in out.items() if v[0] == "http"]
    assert len(ok) == 1, f"两个并发提交应当只有一个成功，实际：{out}"
    assert len(rejected) == 1, f"落败的那个应当拿到一个 4xx，而不是成功或异常：{out}"
    status, detail = rejected[0][1][1], rejected[0][1][2]
    assert 400 <= status < 500, f"落败的那个应当是 4xx，实际 {status}：{detail}"
    assert "COMMITTED" in str(detail), \
        f"报错要说清是「这批已经提交过了」，而不是一句泛泛的失败：{detail}"

    created = out[ok[0]][1]["created"]
    assert created == _EXPECTED, f"成功那一次应当导入 {_EXPECTED} 张，实际 {created}"

    db.expire_all()
    total = _voucher_count(db, book.id)
    assert total == created, (
        f"并发提交把同一批凭证导了两遍：账上 {total} 张，成功那一次只说导入 "
        f"{created} 张。旧实现会在这里拿到 {created * 2} —— 凭证号不同、"
        "唯一约束不触发，用户点一次确认账上多出一份。")
