"""scripts/merge_book_years.py 的行为（审计 P2-04）。

这个脚本此前**一条测试都没有**（``grep -rl merge_book_years tests/`` 零命中），
而它带着两个必然出问题的地方：

1. **期初行的 subject_id 没有改指**。``period_opening.subject_id`` 的外键是
   RESTRICT，而 ``apply`` 的最后一步就是删从账套的科目 —— 于是只要从账套
   带着期初（跨年账套存在的全部意义），删科目就撞外键。事务完整，所以从不
   丢数据，只是**可靠地失败**。
2. **只认「紧接主账套的下一年」**，于是 2026→2027→2028 这样的连年链会被
   切成两组，2028 变成永远并不进 2026 的孤儿。

下面每条都对着真实行为断言，不看代码长什么样。
"""
from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from app.accounting.periods import create_periods  # noqa: E402
from app.accounting.years import ensure_book_year  # noqa: E402
from app.models import Book, Period, PeriodOpening, Subject  # noqa: E402
from app.seed.chart_of_accounts import install_chart  # noqa: E402


def _make_book(db, *, name: str, year: int, company: str) -> Book:
    b = Book(company_name=company, name=name, start_year=year, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id, year)
    # **初始化状态挂在年度上**（book_year，迁移 0032）：账套上不再有那两对列，
    # 科目表与期初余额都是**年度**的属性。
    book_year = ensure_book_year(db, b, year)
    book_year.init_subjects_at = datetime.now(timezone.utc)
    book_year.init_subjects_source = "CHART"
    db.commit()
    db.refresh(b)
    return b


def _first_period(db, book) -> Period:
    return db.scalars(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord)
    ).first()


def _write_opening(db, book, code: str = "1001", amount: str = "1234.56") -> PeriodOpening:
    """给账套的第一个期间写一条期初。

    这一行就是合并失败的全部原因：它让 ``period_opening.subject_id``
    指向从账套的科目。
    """
    period = _first_period(db, book)
    subject = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == code))
    assert subject is not None, f"标准科目表里没有 {code}"
    from decimal import Decimal

    # PeriodOpening 是**有符号**的一列 amount，不是借贷两列 ——
    # 正数记借方，负数记贷方。
    row = PeriodOpening(period_id=period.id, book_id=book.id, subject_id=subject.id,
                        amount=Decimal(amount))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _groups_touching(groups, ids) -> list:
    """只返回**涉及本用例建的账套**的分组。

    find_groups 扫的是全库 —— 全量跑时别的用例留下的账套都在里面，
    直接断言 ``find_groups(db) == []`` 会被那些残留账套打翻（本文件第一版
    就栽在这：单独跑全绿，跟着全量跑就红）。断言只该关心自己造的那几个。
    """
    want = set(ids)
    return [(m, ds) for m, ds in groups if m.id in want or any(d.id in want for d in ds)]


@pytest.fixture
def rollover(db):
    """主账套 2026 + 从账套 2027，命名符合系统自动生成的那种，各带期初。"""
    tag = uuid.uuid4().hex[:8]
    company = f"连锁公司{tag}"
    main = _make_book(db, name=f"租金账套{tag}", year=2026, company=company)
    donor = _make_book(db, name=f"租金账套{tag} 2027", year=2027, company=company)
    return main, donor


# ------------------------------------------------------------------ 期初改指


def test_带期初的跨年账套能真的合并(db, rollover):
    """**这是回归点**：原来这里必然抛 ForeignKeyViolation。

    从账套带着一条期初（而期初正是跨年账套存在的意义），合并应当成功，
    并且期初最终指向**主账套的**科目。
    """
    import merge_book_years as m

    main, donor = rollover
    opening = _write_opening(db, donor)
    donor_subject_id = opening.subject_id

    groups = _groups_touching(m.find_groups(db), {main.id, donor.id})
    assert groups and groups[0][0].id == main.id, "主账套没认出来"
    assert [d.id for d in groups[0][1]] == [donor.id]

    m.apply(db, groups)          # 原来在这里抛 IntegrityError / ForeignKeyViolation

    db.expire_all()
    assert db.get(Book, donor.id) is None, "从账套没被删掉"

    # **科目表每个年度各有一份**（迁移 0032）。主账套**没有** 2027 年的
    # 科目表时，从账套那张表整体搬过来 —— 搬完之后它就是主账套 2027 年的
    # 科目表。id 不变：父指针指着它，换 id 就断了。
    main_subject = db.scalar(select(Subject).where(
        Subject.book_id == main.id, Subject.year == 2027, Subject.code == "1001"))
    assert main_subject is not None, "主账套没有接管从账套 2027 年的科目表"
    assert main_subject.id == donor_subject_id, "搬过去的科目不该换 id（父指针指着它）"

    # 期初是「删掉、按新归属重插」（库层 trg_opening_guard 禁止 UPDATE），
    # 所以主键必然变 —— 按内容找，而不是按原来的 id 找。
    survivors = db.scalars(select(PeriodOpening).where(
        PeriodOpening.book_id == main.id, PeriodOpening.subject_id == main_subject.id)
    ).all()
    assert len(survivors) == 1, f"期初行不见了或多出来 —— 那是丢数据（找到 {len(survivors)} 条）"
    assert str(survivors[0].amount) == "1234.56", "金额在搬的过程中变了"


def test_主账套缺同编码科目时中止而不是硬凑(db, rollover):
    """静默丢掉一条期初 = 静默改数，比合并不下去严重得多。

    **主账套已经有那一年**才谈得上「缺同编码科目」：科目表每个年度各有一份
    （迁移 0032），主账套**没有** 2027 的科目表时，从账套那张表整体搬过来
    正是正确答案 —— 那不是「缺」，那是「补上」。
    """
    import merge_book_years as m

    main, donor = rollover
    _write_opening(db, donor)
    # 主账套装一份 2027 的科目表，再从中删掉 1001
    install_chart(db, main.id, 2027)
    db.execute(
        Subject.__table__.delete().where(
            Subject.book_id == main.id, Subject.year == 2027,
            Subject.code == "1001"))
    db.commit()

    groups = _groups_touching(m.find_groups(db), {main.id, donor.id})
    with pytest.raises(SystemExit) as e:
        m.apply(db, groups)
    assert "1001" in str(e.value)
    assert db.get(Book, donor.id) is not None, "中止了却把账套删了"


# ------------------------------------------------------------------ 连年链


def test_三年链整条并进最早的那个(db):
    """2026/2027/2028 应当并成**一组**。

    原来的判据是 ``start_year == main.start_year + 1``，2028 只跟 2027 挨着，
    而 2027 已经被消费掉 —— 它成了永远并不进 2026 的孤儿。
    """
    import merge_book_years as m

    tag = uuid.uuid4().hex[:8]
    company = f"三连年{tag}"
    main = _make_book(db, name=f"租金{tag}", year=2026, company=company)
    y27 = _make_book(db, name=f"租金{tag} 2027", year=2027, company=company)
    y28 = _make_book(db, name=f"租金{tag} 2028", year=2028, company=company)

    mine = _groups_touching(m.find_groups(db), {main.id, y27.id, y28.id})
    assert len(mine) == 1, f"三年链被切成了 {len(mine)} 组"
    assert mine[0][0].id == main.id
    assert [d.id for d in mine[0][1]] == [y27.id, y28.id]


def test_年份不连续的不合并(db):
    """中间断一年的仍然不该合并 —— 保守是这里的刻意设计。"""
    import merge_book_years as m

    tag = uuid.uuid4().hex[:8]
    company = f"断链{tag}"
    a = _make_book(db, name=f"账{tag}", year=2026, company=company)
    b = _make_book(db, name=f"账{tag} 2028", year=2028, company=company)
    assert _groups_touching(m.find_groups(db), {a.id, b.id}) == []


def test_不同单位名的不合并(db):
    import merge_book_years as m

    tag = uuid.uuid4().hex[:8]
    a = _make_book(db, name=f"账{tag}", year=2026, company=f"A公司{tag}")
    b = _make_book(db, name=f"账{tag} 2027", year=2027, company=f"B公司{tag}")
    assert _groups_touching(m.find_groups(db), {a.id, b.id}) == []


# ------------------------------------------------------------------ 重复期初


def test_同月同期两份期初以主账套为准(db):
    """撞 ``uq_period_opening`` 时删从账套那份，不报错**也不相加**。

    相加会把余额凭空翻倍 —— 那比重复更难被发现。

    搭法：主账套 2026 起，**并且已经有 2027-01 这个期间**（账套可以往后
    多开期间）；从账套 2027 起，于是它头一个期间与主账套撞上，走的是
    「同月同期已存在」那条分支。起始年度相同的两套不会被 find_groups 配对，
    所以这里必须让主账套真的多出一个下一年期间。
    """
    import merge_book_years as m
    from app.models.book import period_ord

    tag = uuid.uuid4().hex[:8]
    company = f"重复{tag}"
    main = _make_book(db, name=f"账{tag}", year=2026, company=company)
    # 主账套多开一个 2027-01，**并且已经有 2027 年的科目表**。
    # **年度要先登记**：period 上有一条指向 ``book_year(book_id, year)`` 的
    # 组合外键（迁移 0032）；科目表也必须与期间同年度（库层护栏）。
    ensure_book_year(db, main, 2027)
    install_chart(db, main.id, 2027)
    db.add(Period(book_id=main.id, year=2027, month=1,
                  ord=period_ord(2027, 1, main.start_year)))
    db.commit()

    donor = _make_book(db, name=f"账{tag} 2027", year=2027, company=company)

    # 两边各自的 2027-01，各有一条 1001 的期初
    main_p2027 = db.scalar(select(Period).where(
        Period.book_id == main.id, Period.year == 2027, Period.month == 1))
    # 期初挂在 2027-01 上，科目就必须取**2027 年**那一份
    # （库层护栏 fn_opening_subject_year_guard 就是这么管的）
    main_sub = db.scalar(select(Subject).where(
        Subject.book_id == main.id, Subject.year == 2027, Subject.code == "1001"))
    keep = PeriodOpening(period_id=main_p2027.id, book_id=main.id,
                         subject_id=main_sub.id, amount=Decimal("100.00"))
    db.add(keep)
    db.commit()
    db.refresh(keep)
    drop = _write_opening(db, donor, amount="999.00")

    groups = [g for g in m.find_groups(db) if g[0].id == main.id]
    assert groups, "这条用例的前置没搭好：主账套与从账套没被配成一对"
    m.apply(db, groups)
    db.expire_all()

    survivors = db.scalars(select(PeriodOpening).where(
        PeriodOpening.period_id == main_p2027.id)).all()
    assert len(survivors) == 1, f"同科目同期间留下了 {len(survivors)} 条"
    assert str(survivors[0].amount) == "100.00", (
        "留的不是主账套那份 —— 两份相加会让余额凭空翻倍")
    # 从账套那份是「删掉、按新归属重插」，id 必然变；断言的是它没有作为
    # 第二条留在同一 (期间, 科目) 上，而不是它的主键。
    from app.models.book import PeriodOpening as _PO
    dup_rows = db.scalars(select(_PO).where(_PO.book_id == main.id)).all()
    assert len(dup_rows) == 1, f"合并后主账套下留了 {len(dup_rows)} 条期初"
