"""报表引用**上级科目**时，取到的数必须与账簿引擎**同一个**。

审计 P1-3（父会话裁定：**维持 P1，但限定为「潜在漂移」** —— 常规操作路径被
``api/subjects.py`` 的 ``_assert_no_own_balance`` 挡着，**直接改库仍可触发**，
而且两处口径确实不同）。

两处口径
--------
* ``app/ledger/engine.py``（账簿引擎）：期初走「**自身快照是基准，下级只补
  增量**」—— 自身有快照：期初 = 自身快照 + Σ下级(子树期初 − 下级自身快照)；
  自身无快照：期初 = Σ下级子树期初。分录也上卷到父科目。
* ``app/reports/evaluate.py``（报表层）：以前是「**整棵子树的末级相加**」。

同一个科目、同一棵树，两个答案。而且旧实现里 ``len(ids) == 1`` 与
``len(ids) >= 2`` 两条分支**结构上就不同**（一条用引擎值、一条用末级之和），
于是**同一条公式的答案随末级后代的个数而变**。

统一到**引擎那一侧**：引擎是账簿的既有语义（总账、科目余额表、打印导出都
按它算），而「末级相加」建立在一个**已经不成立**的前提上 ——
「凭证只记到末级，上级科目自己身上永远是 0」。

这里造的是**直接改库**才造得出的状态（父科目自己身上有期初快照、下面还挂着
子科目）—— 写接口会拦，所以只能直接写库，这也正是「潜在漂移」的含义。

**父科目一律用借方资产**（1001 库存现金 / 1002 银行存款）：报表的「期初余额」
口径是**按科目自然方向取正数**的，贷方科目的符号与引擎的带符号值相反 ——
拿贷方科目来比，两边永远不相等，比的就不是口径而是符号。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.ledger.engine import LedgerEngine
from app.models import PeriodOpening, Subject
from app.reports.evaluate import EvalContext
from app.reports.formula import Evaluator


def _sid(db, book, code: str) -> int:
    sid = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == code))
    assert sid is not None, f"科目不存在：{code}"
    return sid


def _add_child(db, book, parent_code: str, code: str, name: str,
               opening: Decimal | None, period) -> int:
    """在 ``parent_code`` 下面挂一个新科目，并（可选）给它录一张期初快照。

    **直接写库，不是走真实的科目接口** —— 写接口会拦「给有余额的科目挂下级」
    （``api/subjects.py::_assert_no_own_balance``），而这正是本条要验的状态。

    父节点必须同时改成非末级：库里那个 ``trg_subject_leaf_parent_guard``
    是在**提交时**查父节点的，「插子科目却不动父节点」会被 COMMIT 拒掉。
    """
    parent = db.get(Subject, _sid(db, book, parent_code))
    child = Subject(book_id=book.id, code=code, name=name, category=parent.category,
                    direction=parent.direction, level=parent.level + 1,
                    parent_id=parent.id, sort_no=99)
    db.add(child)
    parent.is_leaf = False
    db.flush()
    if opening is not None:
        db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                             subject_id=child.id, amount=opening))
    db.commit()
    return child.id


def _report(db, book, period, formula: str) -> Decimal:
    ctx = EvalContext(db, book, period, {})
    return Evaluator(ctx).run(formula)


# --------------------------------------------------------------------- 主用例


def test_上级科目自身有快照且下级也有快照_报表与账簿引擎同一个数(
    db, book, periods, subjects):
    """1002 自己留着 600 的期初快照（它还是末级时录的），后来又挂了两个下级，
    两个下级各有自己的快照（200 / 300）。

    * 账簿引擎：600 + (200 − 200) + (300 − 300) = **600**
      （自身快照是**总数**不是增量，下级只补增量）。
    * 报表层（旧）：末级相加 = 200 + 300 = **500** —— 另一个数。

    断言的是「**两边同一个数**」，而不是把 600 写死：口径归一才是这条的目的，
    写死数字只能钉住一次实现。
    """
    period = periods[1]
    parent_sid = _sid(db, book, "1002")
    db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                         subject_id=parent_sid, amount=Decimal("600")))
    db.commit()

    _add_child(db, book, "1002", "100291", "下级一", Decimal("200"), period)
    _add_child(db, book, "1002", "100292", "下级二", Decimal("300"), period)
    db.expire_all()

    eng = LedgerEngine.for_ledger(db, book.id)
    engine_opening = eng.opening(parent_sid, period.ord)
    engine_closing = eng.closing(parent_sid, period.ord)
    assert engine_opening == Decimal("600"), engine_opening

    assert _report(db, book, period, '科目("1002","期初余额")') == engine_opening
    assert _report(db, book, period, '科目("1002","期末余额")') == engine_closing
    assert _report(db, book, period, '科目("1002","期初")') == engine_opening
    assert _report(db, book, period, '科目("1002","期末")') == engine_closing


def test_上级科目自身有快照而下级没有快照_报表与账簿引擎同一个数(
    db, book, periods, subjects):
    """1002 银行存款还是末级时录了 1000 的期初，之后才挂上下级（子级没有快照）。

    * 账簿引擎：1000 + (0 − 0) = **1000**
    * 报表层（旧）：末级相加 = **0** —— 报表上「银行存款」那一行恒为 0。

    这是审计报告里的原样复现路径。
    """
    period = periods[1]
    parent_sid = _sid(db, book, "1002")
    db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                         subject_id=parent_sid, amount=Decimal("1000")))
    db.commit()

    _add_child(db, book, "1002", "100291", "下级一", None, period)
    _add_child(db, book, "1002", "100292", "下级二", None, period)
    db.expire_all()

    eng = LedgerEngine.for_ledger(db, book.id)
    engine_opening = eng.opening(parent_sid, period.ord)

    assert engine_opening == Decimal("1000"), engine_opening
    assert _report(db, book, period, '科目("1002","期初余额")') == engine_opening
    assert _report(db, book, period, '科目("1002","期末余额")') == eng.closing(
        parent_sid, period.ord)


# --------------------------------------------------------------------- 不随末级个数变


def test_同一个科目的答案不随末级后代的个数变化(db, book, periods, subjects):
    """「1 个末级后代」与「2 个末级后代」必须给同一个答案。

    旧实现里这两条分支**结构上就不同**（一条用引擎值、一条用末级之和），
    于是同一个科目、同一条公式，答案随末级后代的个数而变 —— 0 与 1000。

    这里造两个父科目（同样有 1000 的自身快照、同样没有子级快照），一个挂 1 个
    子级、一个挂 2 个，断言两者的报表值与引擎值都相同。
    """
    period = periods[1]
    one_sid = _sid(db, book, "1002")
    two_sid = _sid(db, book, "1001")
    for sid in (one_sid, two_sid):
        db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                             subject_id=sid, amount=Decimal("1000")))
    db.commit()

    _add_child(db, book, "1002", "100291", "下级一", None, period)
    _add_child(db, book, "1001", "100191", "下级一", None, period)
    _add_child(db, book, "1001", "100192", "下级二", None, period)
    db.expire_all()

    eng = LedgerEngine.for_ledger(db, book.id)
    one = _report(db, book, period, '科目("1002","期初余额")')
    two = _report(db, book, period, '科目("1001","期初余额")')

    assert one == two == Decimal("1000"), (one, two)
    assert one == eng.opening(one_sid, period.ord)
    assert two == eng.opening(two_sid, period.ord)


# --------------------------------------------------------------------- 干净路径不许变


def test_父级没有自身快照时仍然是子树之和(db, book, periods, subjects):
    """归一**不许**改掉干净路径：父级没有自身快照 → 期初就是子树之和。

    1101 短期投资本来就有下级（110101/110102/110103），只给其中一个录期初：
    父级的期初必须是那一个的数。这是绝大多数真实账套的样子（写接口只允许给
    末级录期初），所以它必须一点没变 —— 变了就是「修一条漂移、弄坏所有正常账」。
    """
    period = periods[1]
    db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                         subject_id=_sid(db, book, "110101"), amount=Decimal("400")))
    db.commit()
    db.expire_all()

    eng = LedgerEngine.for_ledger(db, book.id)
    parent_sid = _sid(db, book, "1101")

    assert eng.opening(parent_sid, period.ord) == Decimal("400")
    assert _report(db, book, period, '科目("1101","期初余额")') == Decimal("400")


def test_末级科目的取数不受影响(db, book, periods, subjects, make_voucher):
    """末级走的是引擎自己的值 —— 归一只动上级科目，末级一个字节都不该变。"""
    make_voucher("2026-01-10", dr=[("110101", 1000, "x")], cr=[("1123", 1000, "x")],
                 posted=True)
    period = periods[1]

    assert _report(db, book, period, '科目("110101","期末余额")') == Decimal("1000.00")
    # 上级仍然是子树的和（1101 干净，没有自身快照）
    assert _report(db, book, period, '科目("1101","期末余额")') == Decimal("1000.00")


def test_发生额口径也走引擎的上卷(db, book, periods, subjects, make_voucher):
    """「本期借方」「借方累计」这些发生额口径同样归一 —— 父科目自己的分录
    以前会被「末级相加」整段丢掉。
    """
    make_voucher("2026-01-10", dr=[("110101", 1000, "x")], cr=[("1123", 1000, "x")],
                 posted=True)
    period = periods[1]
    eng = LedgerEngine.for_ledger(db, book.id)
    parent_sid = _sid(db, book, "1101")

    assert _report(db, book, period, '科目("1101","本期借方")') == eng.figures(
        parent_sid, period.ord).period_dr
    assert _report(db, book, period, '科目("1101","借方累计")') == eng.figures(
        parent_sid, period.ord).dr_cum
