"""期初快照的上卷：自身快照与子树快照不能重复计入（审计 P2-01）。

写入侧的硬约束是「``period_opening`` 只对末级科目写」
（``accounting/periods._refresh_opening``、``api/books.set_openings``），
父级期初天然由子树汇总。但科目树是活的，「先做末级、后来加下级」是
常规操作，这时那个科目会**同时**留下一条自己的快照和下级的快照。

以前 ``_openings()`` 是「自身 + 整棵子树」相加，同一笔余额被计入两次：

    222101 在账套启用时被录了 1000 的期初（那时它是末级）
    之后在它下面挂了 22210199，并给它录了 1000 的期初
    → 222101 的期初变成 2000，而它下面明明只有 1000

这条 bug 的特点是**列与列之间看不出破绽**（期初 + 本期 = 期末 恒成立），
必须拿绝对数字钉住，所以下面的用例全部给了具体金额。
"""
from __future__ import annotations

from decimal import Decimal

from app.ledger.engine import LedgerEngine
from app.models import PeriodOpening, Subject


def _opening(db, book, period, code):
    eng = LedgerEngine.for_ledger(db, book.id)
    sid = db.scalar(select_id(db, book, code))
    return eng.opening(sid, period.ord)


def select_id(db, book, code):
    from sqlalchemy import select
    return select(Subject.id).where(Subject.book_id == book.id, Subject.code == code)


def _add_child(db, book, parent_code: str, code: str, name: str, opening: Decimal,
               period) -> None:
    """在 parent_code 下面挂一个新科目，并给它录一张期初快照。

    **这里是直接构造底层状态，不是走真实的科目接口**（审计 P2-04 的收尾）。

    上一版的 docstring 写的是「走真实的科目接口（父级会自动变成非末级）」，
    而代码是 ``db.add(child)`` —— **文档与代码自相矛盾**。当时之所以没被
    发现，是因为叶子护栏只检查「被改的那一行」，插一行子科目不会触发它；
    现在库里多了 ``trg_subject_leaf_parent_guard``（延迟到提交时查父节点），
    「插子科目却不动父节点」会**在 COMMIT 上被拒**。于是这里必须说清事实：

    * 这条用例要验的是**引擎的上卷算术**（绝对金额），不是科目的创建路径；
    * 所以直接写库，但**必须把这条状态造对** —— 父节点同时改成非末级，
      与 ``api/subjects.py`` 的 ``_sync_leaf_flags`` 结果一致；
    * 引擎刻意**不看 ``is_leaf``**（``ledger/engine.py`` 的期初上卷契约），
      所以改不改它都不影响下面那些断言 —— 改它是为了让库自洽，
      而不是为了让用例通过。

    如果哪天真的要走接口，改成 ``client.post(.../subjects, parent_id=...)``
    并给这几条用例补上 ``client`` / ``auth`` 夹具即可，算术不变。
    """
    parent_id = db.scalar(select_id(db, book, parent_code))
    parent = db.get(Subject, parent_id)
    assert parent is not None, f"父科目不存在：{parent_code}"
    # 年度跟着父科目：父子必须在同一张科目表上（库层护栏 fn_subject_parent_year_guard）
    child = Subject(book_id=book.id, year=parent.year, code=code, name=name,
                    category="ASSET",
                    direction="DEBIT", level=parent_id and 3 or 2,
                    parent_id=parent_id, sort_no=99)
    db.add(child)
    # 与真实创建路径同一个终态：父节点不再是末级。
    parent.is_leaf = False
    db.flush()
    db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                         subject_id=child.id, amount=opening))
    db.commit()


def test_父级期初等于下级之和(db, book, periods, subjects):
    """基线：父级没有自己的快照时，期初就是子树的和（既有行为不能变）。

    父级用 1101 短期投资（借方资产、有下级）；1001/1002 银行存款那三兄弟
    在用友 4-2-2 表里都是末级，撑不起「父级」这个角色。
    """
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["110101"].id, amount=Decimal("700")))
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["110102"].id, amount=Decimal("300")))
    db.commit()

    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["1101"].id, periods[1].ord) == Decimal("1000")


def test_先末级后加下级_期初不被计入两次(db, book, periods, subjects):
    """主用例：222101 自己有 1000 的期初快照，后来又有了下级 22210199（也是 1000）。

    正确答案是 1000（同一笔钱），不是 2000。
    """
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["222101"].id, amount=Decimal("1000")))
    db.commit()
    _add_child(db, book, "222101", "22210199", "下级", Decimal("1000"), periods[1])

    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["222101"].id, periods[1].ord) == Decimal("1000"), (
        "父级自己的快照已经包含了它的下级，再整棵子树加一遍就是重复计入")


def test_只有一个下级有快照时父级仍然只算一份(db, book, periods, subjects):
    """父级自身快照 1000；下级 A 有 600 的快照、下级 B 什么都没有。

    父级的 1000 里本来就含着这两家，加 A 的 600 是重复计入 → 仍是 1000。
    """
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["110101"].id, amount=Decimal("400")))
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["110102"].id, amount=Decimal("600")))
    db.commit()

    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["1101"].id, periods[1].ord) == Decimal("1000"), (
        "父级自身快照是基准，下级只补增量，不能把下级整棵再加一遍")


def test_三层_下级只补增量(db, book, periods, subjects):
    """三级里最有意思的一种：父级留着自己**过时**的快照，中级没有，下级后来才有。

    2221 在还是末级时录了 1000 的期初；222101 那时还没下级所以没有快照；
    之后挂了 22210199 并给它录了 200 —— 这 200 是父级快照之外新长出来的，
    要加上去：2221 = 1000 + 200 = 1200。

    以前这里是「自身 + 整棵子树」= 1000 + 200 = 1200 也对，但同一份数据
    再叠一个「自身有快照的中间层」就变成 1000 + 600 + 200 = 1800（见下一条）。

    这条必须用全表唯一的三层链 2221 → 222101 → 22210199（22210199 是
    这里现挂上去的，科目表里本来没有）；换成两层就验不出「中间层没快照」这一路。
    """
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["2221"].id, amount=Decimal("1000")))
    db.commit()
    _add_child(db, book, "222101", "22210199", "孙级", Decimal("200"), periods[1])

    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["222101"].id, periods[1].ord) == Decimal("200")
    assert eng.opening(subjects["2221"].id, periods[1].ord) == Decimal("1200")


def test_三层_中间层也有快照时不叠加(db, book, periods, subjects):
    """上一条的加强版：中间层 222101 自己也留着一张 600 的快照。

    那张 600 是它还是末级时录的，**已经包含**了下级那 200，所以
    222101 = 600（不是 800）、2221 = 1000（不是 1800）。
    """
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["2221"].id, amount=Decimal("1000")))
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["222101"].id, amount=Decimal("600")))
    db.commit()
    _add_child(db, book, "222101", "22210199", "孙级", Decimal("200"), periods[1])

    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["222101"].id, periods[1].ord) == Decimal("600"), (
        "中间层的快照本身就是总额，再加下级就是重复计入")
    assert eng.opening(subjects["2221"].id, periods[1].ord) == Decimal("1000")


def test_没有自身快照的父级仍是纯子树和(db, book, periods, subjects):
    """加了子科目之后，纯净路径必须一点没变：父级没有自身快照 → 就是子树之和。"""
    _add_child(db, book, "222101", "22210199", "下级", Decimal("400"), periods[1])

    eng = LedgerEngine.for_ledger(db, book.id)
    assert eng.opening(subjects["222101"].id, periods[1].ord) == Decimal("400")
    assert eng.opening(subjects["2221"].id, periods[1].ord) == Decimal("400")


def test_月中补录期初不会把别的科目清零(db, book, periods, subjects, make_voucher):
    """用户在月中给某期补录期初、只录了其中一个科目时，其余科目不能被清零。

    以前 ``_openings()`` 会给「有快照的那一期 × 所有科目」都返回一条 0，
    ``compute()`` 看到有快照就把期初重置成 0 —— 于是没录的那几个科目余额凭空归零。
    """
    make_voucher("2026-02-10", dr=[("1001", 1000, "x")], cr=[("5001", 1000, "x")],
                 posted=True)

    eng = LedgerEngine.for_ledger(db, book.id)
    bank = subjects["1001"].id
    cash = subjects["1012"].id
    before = eng.closing(cash, 5)

    # 5 月只给 1001 补录期初（1012 一个数都没录）
    db.add(PeriodOpening(book_id=book.id, period_id=periods[5].id,
                         subject_id=bank, amount=Decimal("7777")))
    db.commit()

    eng2 = LedgerEngine.for_ledger(db, book.id)
    assert eng2.opening(bank, 5) == Decimal("7777")
    assert eng2.opening(cash, 5) == before, (
        "没录期初的科目期初应该从上期滚过来，不该被这一期的快照清零")
