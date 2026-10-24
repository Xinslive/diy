"""2026-10-24 性能审计的钉子：账簿取数路径的**内存**不再靠「小心」来保证。

这份文件钉的是 2026-10-24 那轮审计落地的三件事：

1. **明细账取数不再物化 ORM 实体，也不再 join 一张用不上的表。**
   以前是 ``select(Entry, Voucher, Subject)`` —— 一行三个 ORM 对象，
   而第三张 ``Subject`` **从头到尾没被用过**。实测每行约 5.6 KB，
   6 万分录的账本 = **336 MB 的瞬时内存**；改成列元组后约 0.8 KB
   （**-85%**），实测峰值 98.8 MB → 23.6 MB。
   这里把「不 join Subject」「不物化实体」两条都钉住。

2. **moves 的跨请求缓存有自己的上界。**
   它的一条就是**整本账**的发生额聚合（压测规模 ≈ 2.7 MB/条），
   与另外三种缓存（几十 KB/条）差了两个数量级，共用一个上界就是
   「一个缓存吃掉一台 1.6 GB 机器的 5%」。

3. **凭证打印页有行数闸。**
   同族的三个打印范围（科目余额表 / 总账 / 明细账）都有，唯独凭证没有 ——
   而它恰恰是内存最凶的一个（整份 HTML 文档一次性在内存里拼出来，
   而且同时存在三份拷贝）。

**为什么要有这些钉子。** 这三条都不是「写错了会报错」的改动 ——
它们全是**性能**改动，而性能改动的默认命运是：半年后有人为了「顺手」
把 select 改回实体、把上界去掉，**没有任何测试会红**，而机器在一个月后
悄悄开始 swap。所以这里钉的是**形状**，不是某次测出来的具体数字。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.ledger import queries
from app.ledger.cache import BoundedTTLCache, cache_for, clear_all
from app.models import Entry, Subject, Voucher


# --------------------------------------------------------------------- 1. 取数形状


def test_取分录的语句不join用不上的subject表():
    """``_entry_stmt`` 不得 join ``Subject``。

    以前是 ``select(Entry, Voucher, Subject)``，两个调用点都只绑定
    ``_subj`` 且**从不使用**它。代价是每行多一个 ORM 对象
    （6 万行 = 几十 MB）**加**一次完全用不上的 join。

    这条断言看的是 **SQL 文本**：join 掉了就是 join 掉了，
    不依赖「用不用得上」这种运行时判断。
    """
    stmt = queries._entry_stmt(book_id=1, include_unposted=False)
    sql = " ".join(str(stmt).split()).lower()
    assert " join subject" not in sql, (
        "取分录的语句又 join 了 subject —— 那张表从来没有被用到过：\n" + sql)
    # 但 join voucher 是要的：状态过滤与凭证号/日期都在那张表上。
    assert " join voucher " in sql, sql


def test_取分录不物化ORM实体而是取列元组():
    """``_entry_stmt`` 的输出必须是**列**，不是实体。

    物化成实体的代价是每行两个带 identity map 的 ORM 对象。
    取成列元组之后每行只留真正用到的那些值。

    断言 ``selected_columns`` 里**没有实体**：SQLAlchemy 对「实体」与
    「列」是分开记的（``_select_iterables``），实体会出现在那个列表里。
    """
    stmt = queries._entry_stmt(book_id=1, include_unposted=False)
    from sqlalchemy.orm import Mapper
    entities = [c for c in stmt.selected_columns
                if isinstance(getattr(c, "entity", None), Mapper)]
    assert not entities, (
        "取分录又在物化 ORM 实体了 —— 每行两个对象的代价是列元组的六倍："
        f"{[str(e) for e in entities]}")


def test_明细账的取数列与用到的字段一一对应():
    """选出来的列必须**够用**，且**不多余**。

    够用：少一列，``_detail_account_rows`` 就排不出那一行。
    不多余：多选一列，6 万行就是 6 万个没人读的对象。

    这里钉的是「集合相等」而不是「包含」—— 多选一列同样是回归，
    而「包含」这种断言看不出来。
    """
    stmt = queries._entry_stmt(book_id=1, include_unposted=False)
    got = {c.name for c in stmt.selected_columns}
    expected = {
        "subject_id", "period_ord", "amount", "direction", "summary",
        "id", "voucher_no", "voucher_date", "status",
    }
    assert got == expected, (
        "取分录的列集合变了。"
        f"\n  现在：{sorted(got)}\n  应该是：{sorted(expected)}")


def test_明细账行里的字段一个都不少(db, book, periods, subjects, make_voucher):
    """列元组化之后，**出行内容必须与之前逐字一致**。

    这是本文件最重要的一条：前面三条钉的是「形状」，
    这条钉的是「**结果没变**」。少一个字段、金额口径变了、
    余额方向反了 —— 都要在这里红。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "现金收入")],
                 cr=[("5001", 100, "收入")], posted=True)
    period = periods[1]
    rows = queries.detail_ledger(
        db, book.id, subjects["1001"].id, period, period, include_unposted=False)
    entries = [r for r in rows["rows"] if r["kind"] == "ENTRY"]
    assert entries, "这一张凭证应该有逐笔行"
    row = entries[0]
    assert row["voucher_no"], "凭证号没了"
    assert row["voucher_date"], "凭证日期没了"
    assert row["summary"] == "现金收入", row
    assert row["debit"] == Decimal("100"), row
    assert row["credit"] == Decimal("0"), row
    assert row["status"] == "POSTED", row
    assert row["voucher_id"], "凭证 id 没了"


# --------------------------------------------------------------------- 2. 缓存上界


def test_moves缓存有自己的上界():
    """moves 一条 = 整本账的发生额聚合，必须**单独**限容。

    另外三种缓存（periods / ancestors / openings）一条是几十 KB，
    共用一个上界就等于让 moves 按那个上界常驻 —— 16 × 2.7 MB = 43 MB/worker。
    """
    assert settings.ledger_cache_moves_maxsize < settings.ledger_cache_maxsize, (
        "moves 缓存必须比其它缓存的上界更小 —— 它的一条就是整本账的聚合"
        f"（moves={settings.ledger_cache_moves_maxsize}，"
        f"其它={settings.ledger_cache_maxsize}）")
    assert settings.ledger_cache_moves_maxsize >= 1, "上界不能是 0"


def test_各类缓存按自己的上界取():
    """``shared()`` 必须按种类选上界，而不是一律用同一个数。"""
    from app.ledger import engine as eng_mod
    clear_all()
    moves = eng_mod.shared("moves", ("k",))
    periods = eng_mod.shared("periods", ("k",))
    assert moves is not periods, "不同种类的缓存必须是两个对象"
    assert moves._maxsize == settings.ledger_cache_moves_maxsize, moves._maxsize
    assert periods._maxsize == settings.ledger_cache_maxsize, periods._maxsize
    clear_all()


def test_缓存上界超了就挤掉最久没用的():
    """上界是真的在生效，不是只写在配置里。

    ``BoundedTTLCache`` 的上界是**硬约束**（见 app/ledger/cache.py
    「边界：必须有上界」那一节）—— 没有上界的进程内缓存在长驻进程里
    会一路涨，而 worker 不重启、账却越记越多。
    """
    c = BoundedTTLCache(maxsize=2, ttl_seconds=60)
    c.get_or_set("a", lambda: 1)
    c.get_or_set("b", lambda: 2)
    c.get_or_set("c", lambda: 3)
    assert len(c) == 2, f"缓存没有按上界挤掉旧条目：{len(c)}"
    assert c.get_or_set("a", lambda: 99) == 99, "a 已经被挤掉了"


# --------------------------------------------------------------------- 3. 打印行数闸


def test_凭证打印页有行数闸(db, book, periods, subjects, make_voucher, monkeypatch):
    """凭证打印必须有行数上限，与另外三个打印范围同一把尺。

    没有闸的时候：满月的账 5000 张凭证 × 8 行分录 = 4 万行，
    而打印是**整份 HTML 文档一次性在内存里拼出来**的，
    而且同时存在三份拷贝（pieces / docs / wrap_pages）。
    """
    from app.printing import excel as xl

    from app.api import printing as pr

    calls = []

    def fake_check(n: int, kind: str) -> None:
        # 记下这一关卡测到的行数，然后**一律拦下来** ——
        # 本用例要验的是「这一关**存在**且**在序列化之前**」，不是闸门的阈值。
        calls.append((n, kind))
        raise xl.ExportTooLarge(f"{kind}共 {n:,} 行，超过上限")

    monkeypatch.setattr(pr, "_check_print_line_count", fake_check)
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    with pytest.raises(xl.ExportTooLarge):
        pr._voucher_html(db, book, periods[1], {"voucher": {}}, status=None)
    assert calls, "凭证打印压根没有走行数闸"
    assert calls[0][1] == "记账凭证", calls


def test_行数闸与Excel导出用同一个上限():
    """两处面对的是同一份账、同一批行，上限必须是同一个数。

    写成两个常量迟早会被调成不一致 —— 而不一致意味着「Excel 拦住了、
    打印没拦住」，正是这次要修的那个洞。
    """
    from app.printing import excel as xl

    from app.api import printing as pr

    # 闸门之内放行，出了闸门抛同一个异常 —— 两者用的是同一个上限。
    pr._check_print_line_count(xl.MAX_EXPORT_ROWS, "记账凭证")
    with pytest.raises(xl.ExportTooLarge):
        pr._check_print_line_count(xl.MAX_EXPORT_ROWS + 1, "记账凭证")


def test_打印与Excel数行数的口径一致():
    """凭证打印数的是**分录条数**，不是凭证张数。

    一张凭证可以有 2 行也可以有 200 行。按张数算闸门等于没算 ——
    200 张 200 行的凭证是 4 万行，按张数只有 200。

    Excel 导出那边（``xl.vouchers_excel``）数的是
    ``sum(len(v.get("entries") or []) for v in rows)`` ——
    打印这边数的是 ``sum(len(v.entries) for v in shown)``，口径相同。
    """
    import inspect

    from app.printing import excel as xl

    src = inspect.getsource(xl.vouchers_excel)
    assert 'len(v.get("entries") or [])' in src, (
        "Excel 侧数行数的口径变了 —— 打印侧要跟着一起改")
    # 打印侧：分录条数之和。
    from app.api import printing as pr
    pr_src = inspect.getsource(pr._voucher_html)
    assert "len(v.entries)" in pr_src, (
        "打印侧数行数的口径变了 —— 两处必须是同一把尺")


def test_超出上限抛的是可翻译成413的那个异常():
    """异常类型必须与 Excel 通道一致 —— 由同一个 handler 翻译成 413。

    明确失败（413 + 「请缩小范围」）比静默截断出一份缺行的账好得多：
    缺行的账交给审计方是错的。
    """
    from app.printing import excel as xl

    from app.api import printing as pr

    with pytest.raises(xl.ExportTooLarge):
        pr._check_print_line_count(xl.MAX_EXPORT_ROWS + 1, "记账凭证")
    # 闸门之内必须放行。
    pr._check_print_line_count(xl.MAX_EXPORT_ROWS, "记账凭证")


# --------------------------------------------------------------------- 4. 发票上传


def test_发票上传不再把全部字节攒在内存里():
    """上传接口不得再攒出一份「所有文件的 bytes」。

    上传的字节流**本来就已经在磁盘上了** —— FastAPI 的 ``UploadFile.file``
    是 ``SpooledTemporaryFile``，超过 1 MB 自动溢写。把它 ``read()`` 成
    ``bytes`` 等于把已经在磁盘上的东西**又抄一份进内存**，
    而一批的合计上限是 **50 MB**。

    改成边读边写之后，峰值内存 = 一个 64 KB 的块，与上传大小无关。
    实测（``scripts/measure_invoice_upload_memory.py``）：20 MB 的一批
    **20.0 MB → 0.1 MB 峰值**。

    这条断言看的是**源码形状**：接口里不得出现「攒 payload 的列表」。
    """
    import ast
    import inspect

    from app.api import invoices

    src = inspect.getsource(invoices.upload_invoice_batch)
    tree = ast.parse(inspect.cleandoc(src))
    # 「攒起来」的形状：一个 AnnAssign / Assign 的目标是「list[...]」，
    # 而且它的名字里带 payload / files / contents 这类「装内容」的词。
    collecting = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if "payload" in target.id or target.id in ("contents", "blobs"):
                collecting.append(target.id)
    assert not collecting, (
        f"上传接口又在攒 payload 列表了：{collecting} —— "
        "那是把已经在磁盘上的字节又抄一份进内存（合计上限 50 MB）")


def test_发票上传走的是共享的容量闸():
    """上传入口必须调用 ``app.core.uploads`` 里的闸。

    闸得住住的是「上传体积」这一条纪律；闸住在别处等于把纪律拆成两处。
    ``save_upload_to_temp`` 是同一道「按字节数设上限」的闸，只是边读边写。
    """
    import ast
    import inspect

    from app.api import invoices

    src = inspect.getsource(invoices.upload_invoice_batch)
    calls = {
        node.func.id for node in ast.walk(ast.parse(inspect.cleandoc(src)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "save_upload_to_temp" in calls, (
        "上传接口没有调用共享的容量闸："
        f"{sorted(calls)}")


def test_边读边写的那道闸真的挡住超限(tmp_path):
    """闸必须真的生效 —— 而且是在**读的过程中**判的。

    ``file.size`` 是客户端声明的，不可信；所以上限要靠「读一块判一块」来兜。
    """
    import io

    from app.core.uploads import UploadTooLarge, save_upload_to_temp

    with pytest.raises(UploadTooLarge):
        save_upload_to_temp(io.BytesIO(b"x" * 200_000), tmp_path,
                            limit=100_000, label="发票")
    # 没超限就放行。
    name = save_upload_to_temp(io.BytesIO(b"x" * 1000), tmp_path,
                                limit=100_000, label="发票")
    assert (tmp_path / name).exists()


def test_单份上传落盘的就是用户传上来的原文件():
    """单文件上传时落盘的字节**一个都不改**。

    改名（promote）而不是重写 —— 这是既有行为（「原文件原样落盘」），
    改实现时最容易悄悄改掉的就是这一条。
    """
    from app.invoice import storage

    original = b"%PDF-1.4 original bytes \x00\x01"
    temp = storage.stage_temp_bytes(original)
    try:
        final = storage.promote(temp, "batch_unit_test.pdf")
        assert (storage.root() / final).read_bytes() == original
    finally:
        storage.delete(temp)
        storage.delete("batch_unit_test.pdf")
