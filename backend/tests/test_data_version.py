"""跨请求聚合缓存的正确性（2026-10 性能收尾 P0）。

这组用例盯的是**一件**事：加了缓存之后，用户看到的数字必须与不加缓存时
**逐分逐厘一样**。

它存在的理由是这个功能最典型的失败形态：缓存写错了不会报错，
只会让某些余额在某些时刻对不上 —— 而那正是 README 明确承诺「不会发生」的事：

    凭证和分录是唯一事实来源，余额、账簿、报表、打印全部由同一套聚合引擎
    派生。因此不存在「某个报表和总账对不上」这种事。

所以这里不用「跑通了」当验收标准，而是逐条钉住**作废机制**的每个侧面：

1. 版本号会被每一种写顶起来（漏一种 = 那条写之后余额不刷新）；
2. 写完立刻重查必须看到新数（read-your-writes，**核心断言**）；
3. 两个口径（账簿/报表）不能互相串；
4. 两个账套不能互相串；
5. 两种科目范围不能互相串；
6. 缓存有界（worker 不重启，账会越记越多）；
7. 版本号是**每条语句** +1 而不是每行 +1（锁竞争的防线）。

第 7 条单独说一句：它防的不是正确性而是**性能**。行级触发器同样正确，
但一次 5000 行导入会让 ``book`` 行（整个账套共用的那一行）排队 5000 次。
所以「有人把它改成行级」必须让测试变红。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.ledger import cache as cache_mod
from app.ledger.engine import LedgerEngine
from app.models import Book, Entry, Period, Subject, Voucher


@pytest.fixture(autouse=True)
def _clean_shared_cache():
    """每个用例前后清空进程内缓存。

    不清的话，上一个用例的聚合结果可能被下一个用例命中 ——
    键里虽然有 book_id 与 data_version，但测试库会复用 book 行，
    版本号未必变化，那就串了。
    """
    cache_mod.clear_all()
    yield
    cache_mod.clear_all()


def _version(db, book_id: int) -> int:
    db.expire_all()
    return db.scalar(select(Book.data_version).where(Book.id == book_id)) or 0


def _subjects(db, book_id: int) -> dict:
    return {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book_id)).all()}


def _seed(db, user, book_id: int, date_str: str, amount: str, summary: str = "x"):
    """造一张最普通的凭证：银行存款 ↔ 应付账款。"""
    from datetime import date as _date

    from app.accounting.vouchers import create_voucher, post_vouchers

    subs = _subjects(db, book_id)
    v = create_voucher(
        db, book_id=book_id, user=user, voucher_date=_date.fromisoformat(date_str),
        entries=[
            {"subject_id": subs["1001"].id, "amount": amount,
             "summary": summary, "direction": "DEBIT"},
            {"subject_id": subs["2001"].id, "amount": amount,
             "summary": summary, "direction": "CREDIT"},
        ], commit=False)
    db.commit()
    # 账簿口径默认**不含**未记账凭证（include_unposted=False），
    # 不记账的话下面所有断言看到的都是 0，而用例照样「通过」。
    post_vouchers(db, book_id, [v.id], user)


def _movements_total(db, book_id: int, subject_ids=None) -> Decimal:
    e = LedgerEngine.for_ledger(db, book_id)
    e.compute()
    return sum((dr for (_sid, _o), (dr, _cr) in e._cum_movements(99, subject_ids).items()),
               Decimal("0"))


# ====================================================================== 1


def test_版本号会被每一种写顶起来(db, book, admin_user, make_voucher):
    """**漏掉任何一种写，那条写之后余额就不刷新。**

    触发器覆盖 voucher / entry / subject / period_opening / period 五张表。
    这里逐张表走一遍真实写路径。
    """
    bid = book.id

    # voucher + entry：建一张凭证
    before = _version(db, bid)
    make_voucher("2026-01-15", dr=[("1001", 100, "买货")], cr=[("2001", 100, "付钱")])
    assert _version(db, bid) > before, "建凭证没有顶起版本号"

    # entry：改一张已有凭证的分录
    v = db.scalar(select(Voucher).where(Voucher.book_id == bid).order_by(Voucher.id))
    e = db.scalar(select(Entry).where(Entry.voucher_id == v.id))
    before = _version(db, bid)
    e.summary = "改了摘要"
    db.commit()
    assert _version(db, bid) > before, "改分录没有顶起版本号"

    # subject：新增一个科目
    before = _version(db, bid)
    # 年度必填：科目表每个年度各有一份（迁移 0032）
    s = Subject(book_id=bid, year=db.scalar(
        select(Book.start_year).where(Book.id == bid)),
        code="9999", name="测试科目", level=1,
        category="ASSET", direction="DEBIT", is_leaf=True, sort_no=9999)
    db.add(s)
    db.commit()
    assert _version(db, bid) > before, "新增科目没有顶起版本号"

    # period_opening：录一次期初
    first_period = db.scalar(
        select(Period).where(Period.book_id == bid).order_by(Period.ord))
    before = _version(db, bid)
    db.execute(
        text("INSERT INTO period_opening (book_id, period_id, subject_id, amount) "
             "VALUES (:b, :p, :s, 10.00)"),
        {"b": bid, "p": first_period.id, "s": s.id})
    db.commit()
    assert _version(db, bid) > before, "录期初没有顶起版本号"

    # period：改一次期间（locked 是期间上真实存在的列）
    before = _version(db, bid)
    db.execute(text("UPDATE period SET locked = NOT locked WHERE book_id=:b AND ord=1"),
               {"b": bid})
    db.commit()
    assert _version(db, bid) > before, "改期间没有顶起版本号"


def test_删凭证也顶起版本号(db, book, make_voucher, admin_user):
    """删也要算「数据变了」。

    漏掉 DELETE 的话，删完凭证余额还留在账上 —— 而账面比实际多一笔钱，
    是比「少算」更难被发现的方向。
    """
    from app.accounting.vouchers import delete_voucher

    bid = book.id
    v = make_voucher("2026-01-15", dr=[("1001", 100, "买货")], cr=[("2001", 100, "付钱")])
    before = _version(db, bid)
    delete_voucher(db, v, admin_user)
    db.commit()
    assert _version(db, bid) > before, "删凭证没有顶起版本号 —— 余额会一直留着"


# ====================================================================== 2


def test_写完立刻重查必须看到新数(db, book, make_voucher):
    """**核心断言。** 整组用例里只有这一条能直接抓住「缓存写错了」。

    顺序刻意这样：先查一次（把聚合结果**装进缓存**），再写入，再查一次。
    如果第二次没查数据库就会拿到第一次的旧结果 —— 也就是「用户刚记完账，
    刷新总账看到的还是记之前的数」。
    """
    bid = book.id
    eng1 = LedgerEngine.for_ledger(db, bid)
    eng1.compute()
    eng1._cum_movements(99)          # 预热：让缓存里有东西

    make_voucher("2026-02-10", dr=[("1001", 500, "买货")], cr=[("2001", 500, "付钱")],
                 posted=True)

    # **换一个引擎实例** —— 这才是真实场景（每个请求都是新引擎）。
    # 不换实例的话测的是实例内缓存，跟跨请求缓存没关系。
    eng2 = LedgerEngine.for_ledger(db, bid)
    eng2.compute()
    total = sum((dr for (_sid, _o), (dr, _cr) in eng2._cum_movements(99).items()),
                Decimal("0"))
    assert total == Decimal("500"), (
        f"写入后立刻重查没看到新凭证的 500 元（拿到 {total}）—— "
        "这就是「刚保存的东西没生效」")


def test_跨请求缓存确实命中了(db, book, make_voucher):
    """反过来验：缓存**得真的被用上**，否则上面那条可能只是因为根本没缓存。

    不靠「查询次数」这种间接指标，直接看缓存自己的命中计数 ——
    计数不对的话，用例照样绿，而功能其实没生效。
    """
    bid = book.id
    make_voucher("2026-01-15", dr=[("1001", 100, "买货")], cr=[("2001", 100, "付钱")],
                 posted=True)

    for _ in range(3):
        _movements_total(db, bid)

    assert cache_mod.cache_for("moves").stats()["hits"] > 0, (
        "三次同样的查询一次都没命中缓存 —— 跨请求缓存没有真正生效")


# ====================================================================== 3


def test_两个口径不能互相串(db, book, make_voucher):
    """账簿口径含结转凭证，报表口径排除结转凭证，两者差额恰好是结转金额。

    串了的后果：月度结转之后，利润表会显示「本期净额恒为 0」
    （因为含结转就把损益轧平了），而总账显示的却是真实发生额。
    两边对不上，且**看起来都像对的**。

    刻意不绕道「真的执行一次结转」—— 那样用例还依赖结转的前置条件与副作用，
    挂了分不清是缓存串了还是结转没发生。这里直接造一张
    ``source=CARRY_FORWARD`` 的凭证：口径差异的唯一来源就是那个字段。
    """
    bid = book.id
    make_voucher("2026-01-10", dr=[("1001", 300, "收款")], cr=[("5001", 300, "收入")],
                 posted=True, source="MANUAL")
    # 同科目、同金额，只是 source 不同 —— 于是只有口径能把它们分开
    make_voucher("2026-01-31", dr=[("1001", 300, "结转收款")], cr=[("5001", 300, "结转收入")],
                 posted=True, source="CARRY_FORWARD")
    db.expire_all()

    # 前提自检：确实存在一张结转凭证，否则下面的断言没有意义
    cf = db.scalar(select(Voucher).where(
        Voucher.book_id == bid, Voucher.source == "CARRY_FORWARD"))
    assert cf is not None, "测试数据没造出结转凭证，本条用例失去意义"

    # 先让两个口径各跑一次，缓存里两个口径都会有条目
    for factory in (LedgerEngine.for_ledger, LedgerEngine.for_report):
        e = factory(db, bid)
        e.compute()
        e._cum_movements(99)

    ledger_moves = LedgerEngine.for_ledger(db, bid)._cum_movements(99)
    report_moves = LedgerEngine.for_report(db, bid)._cum_movements(99)

    assert ledger_moves != report_moves, (
        "账簿口径与报表口径返回了完全相同的结果 —— 缓存键没带口径。"
        "实际后果：利润表把损益轧成 0，而总账显示真实发生额")


# ====================================================================== 4


def test_两个账套不能互相串(db, book, admin_user):
    """A 账套的余额不能出现在 B 账套的查询里。

    键里必须带 book_id。这条看着显然，但它串起来的后果是**跨公司的数字** ——
    账套之间是有权限边界的（sys_user_book）。
    """
    import uuid as _uuid

    from app.accounting.periods import create_periods
    from app.models import UserBook
    from app.seed.chart_of_accounts import install_chart

    b2 = Book(company_name=f"另一家{_uuid.uuid4().hex[:6]}",
              name=f"账套{_uuid.uuid4().hex[:6]}", start_year=2026, start_month=1)
    db.add(b2)
    db.flush()
    create_periods(db, b2)
    install_chart(db, b2.id)
    db.add(UserBook(user_id=admin_user.id, book_id=b2.id, role="OWNER"))
    db.commit()
    db.refresh(b2)

    _seed(db, admin_user, book.id, "2026-01-15", "100", "账套A")
    _seed(db, admin_user, b2.id, "2026-01-20", "777", "账套B")
    db.expire_all()

    # 各查两次：第二次吃缓存。串了的话第二次就会读到对方的数。
    a_first, a_second = _movements_total(db, book.id), _movements_total(db, book.id)
    b_first, b_second = _movements_total(db, b2.id), _movements_total(db, b2.id)

    assert a_first == a_second == Decimal("100"), f"账套 A 应为 100，实际 {a_first}/{a_second}"
    assert b_first == b_second == Decimal("777"), f"账套 B 应为 777，实际 {b_first}/{b_second}"


# ====================================================================== 5


def test_两种科目范围不能互相串(db, book, make_voucher):
    """先查全账套、再查单科目，两者各自都要对。

    这条对应 engine.py 既有注释里那句话：「范围必须进缓存键，否则
    『查得全的』和『只查了一个科目的』会互相串」。
    """
    bid = book.id
    make_voucher("2026-01-15", dr=[("1001", 100, "买货")], cr=[("2001", 100, "付钱")],
                 posted=True)
    db.expire_all()

    e1 = LedgerEngine.for_ledger(db, bid)
    e1.compute()
    full, full_again = e1._cum_movements(99), e1._cum_movements(99)

    target = _subjects(db, bid)["1001"].id
    e2 = LedgerEngine.for_ledger(db, bid)
    e2.compute()
    one, one_again = e2._cum_movements(99, [target]), e2._cum_movements(99, [target])

    assert full == full_again
    assert one == one_again
    assert full != one, "收窄范围与全账套返回了相同结果 —— 范围没进缓存键"
    assert target in {sid for sid, _o in one}, "单科目查询里没有那个科目"


# ====================================================================== 6


def test_缓存有界():
    """**必须有上界。**

    压测记录了峰值内存 O(科目数 × 期间数)：317 末级 × 24 期间 = 7,608 组。
    worker 不重启，而账会越记越多 —— 无界的进程内缓存会一路涨，
    最后表现为「跑了三个月之后内存莫名高了一截」，且极难定位。
    """
    c = cache_mod.BoundedTTLCache(maxsize=4, ttl_seconds=60)
    for i in range(50):
        c.get_or_set(("k", i), lambda i=i: i)

    assert len(c) <= 4, f"缓存涨到了 {len(c)} 条，上界失效"


def test_挤掉的是最久没用的那个():
    c = cache_mod.BoundedTTLCache(maxsize=4, ttl_seconds=60)
    for i in range(4):
        c.get_or_set(("k", i), lambda i=i: i)
    c.get_or_set(("k", 0), lambda: 0)      # 0 刚被用过
    c.get_or_set(("k", 9), lambda: 9)      # 新的，应当挤掉最久没用的 1

    assert len(c) == 4
    assert c.get_or_set(("k", 0), lambda: "被挤了") == 0, "0 刚被用过，不该先被挤掉"
    assert c.get_or_set(("k", 1), lambda: "被挤了") == "被挤了", "1 最久没用，应当已被挤掉"


def test_上界必须是正数():
    with pytest.raises(ValueError):
        cache_mod.BoundedTTLCache(maxsize=0)


def test_TTL只影响让位不影响正确性():
    """TTL 设成 0（每次都过期）时，**结果仍然必须是对的**。

    这条钉的是「TTL 不承担正确性」：如果哪天有人把版本号从键里去掉、
    改成「靠 TTL 兜底」，那么 TTL 调大就会开始返回陈旧读，
    而上面那几条用例全都看不出来（它们跑得很快，TTL 没到）。
    """
    c = cache_mod.BoundedTTLCache(maxsize=8, ttl_seconds=0.0)
    assert c.get_or_set(("k",), lambda: 1) == 1
    assert c.get_or_set(("k",), lambda: 2) == 2, "TTL=0 时应当每次重算"


# ====================================================================== 7


def test_版本号每条语句只加一次(db, book, make_voucher):
    """**语句级触发器的语义 —— 锁竞争的防线。**

    一次插 500 行分录：语句级 +1（左右），行级 +500。
    ``book`` 行是整个账套共用的那一行，500 次 UPDATE 就是 500 次排队 ——
    跨请求缓存本是为了让读变快，不能反过来把写堵死。

    这条用例会挡住「有人顺手把触发器改成行级」。
    """
    bid = book.id
    v = make_voucher("2026-03-01", dr=[("1001", 1, "x")], cr=[("2001", 1, "x")])
    db.expire_all()

    subs = _subjects(db, bid)
    before = _version(db, bid)

    # **一条** INSERT 语句插 500 行分录（250 借 + 250 贷）。
    # 借贷必须配平：trg_entry_balance 是延迟到提交时校验的约束，
    # 只插借方会在 commit 时被护栏拦下，跟本条要测的东西完全无关。
    #
    # 行号从 **3** 起：make_voucher 自己已经建了 1、2 两行，
    # 而 ``uq_entry_voucher_line_no``（迁移 0038）规定同一张凭证里
    # 行号不能重复 —— 从 2 开始插会撞上它已有的那一行。
    # （那条唯一约束挡的正是「并发改同一张凭证留下的重复分录」，
    # 同一笔钱在账上记两遍，而借贷仍然平。）
    db.execute(
        text("""INSERT INTO entry
                 (voucher_id, book_id, period_ord, line_no, subject_id,
                  direction, amount, summary)
               SELECT :vid, :bid, (SELECT ord FROM period WHERE id = :pid),
                      g.i,
                      CASE WHEN g.i % 2 = 0 THEN :dr ELSE :cr END,
                      CASE WHEN g.i % 2 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
                      0.01, '批量'
               FROM generate_series(3, 502) AS g(i)"""),
        {"vid": v.id, "bid": bid, "pid": v.period_id,
         "dr": subs["1001"].id, "cr": subs["2001"].id})
    db.commit()
    db.expire_all()

    inserted = db.scalar(
        text("SELECT count(*) FROM entry WHERE voucher_id = :vid"), {"vid": v.id})
    # make_voucher 自己已经建了 2 行（1 借 1 贷），这里再加 500 行
    assert inserted == 502, f"测试数据没插进去（实际 {inserted} 行），本条用例失去意义"

    delta = _version(db, bid) - before
    assert delta <= 3, (
        f"插 500 行分录，版本号涨了 {delta} —— 触发器可能不是语句级。"
        "行级会让 book 行排队 500 次，那正是这条 revision 想避免的瓶颈")


def test_触发器必须是语句级(db, book):
    """直接查 ``information_schema`` 把「语句级」这件事钉死。

    上面那条靠版本号增量间接判断；这条直接读触发器自身的属性 ——
    有人改了触发器定义时它会立刻红，不需要等数据量堆到 500 行。
    """
    rows = db.execute(text("""
        SELECT trigger_name FROM information_schema.triggers
        WHERE trigger_schema = current_schema() AND trigger_name LIKE 'trg_entry_dv_%'
    """)).scalars().all()
    assert len(rows) == 3, f"entry 上应该有 3 个版本号触发器，实际 {rows}"

    # pg_trigger.tgtype 的最低位是 1 时表示 FOR EACH ROW
    row_level = db.execute(text("""
        SELECT t.tgname FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relname = 'entry' AND t.tgname LIKE 'trg_entry_dv_%'
          AND (t.tgtype & 1) = 1
    """)).scalars().all()
    assert not row_level, (
        f"这些触发器是行级（{row_level}）—— 必须改成 FOR EACH STATEMENT，"
        "否则一次 5000 行导入会让 book 行排队 5000 次")


# ====================================================================== 引擎契约


def test_版本号必须先于任何聚合查询读(db, book):
    """**顺序是硬约束。**

    构造引擎时的**第一条** SQL 必须是读 ``book.data_version``。
    反序（先查数据、后读版本号）会把「旧数据」缓存进「新版本」的键 ——
    那个键之后会一直被命中，直到下一次写入。
    """
    order: list[str] = []
    orig = db.scalar

    def spy(stmt, *a, **kw):
        order.append(str(stmt))
        return orig(stmt, *a, **kw)

    db.scalar = spy
    try:
        LedgerEngine.for_ledger(db, book.id)
    finally:
        db.scalar = orig

    assert order, "构造引擎时一条查询都没发"
    assert "data_version" in order[0], (
        "构造引擎的第一条查询不是读版本号 —— 顺序反了会把旧数据"
        "缓存进新版本的键，而那是最难查的一类 bug。")


def test_缓存键带上所有会改变结果的过滤条件(db, book):
    """include_unposted / exclude_voucher_id 漏进键的后果不是「慢」，是**算错**：
    未记账凭证的余额不出现、被排除的凭证仍在。"""
    e = LedgerEngine.for_ledger(db, book.id)
    salt = e._key_salt
    assert e.book_id in salt
    assert e._data_version in salt
    assert e.include_unposted in salt
    assert e.exclude_voucher_id in salt
    assert e._caliber in salt
