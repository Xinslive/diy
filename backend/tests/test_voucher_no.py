"""凭证号补零：位数收窄到三位，且只补零、不改数字。"""
from datetime import date
from decimal import Decimal

from app.models import Entry, Subject, Voucher
from app.seed.voucher_no import repad_voucher_numbers


def _mk(db, book, make_voucher, no):
    v = make_voucher("2026-01-05", dr=[("1002", 100, "x")], cr=[("5001", 100, "x")])
    v.voucher_no = no
    db.flush()
    return v


def test_四位号补成三位(db, book, make_voucher):
    """只补零、不改数字：记0001 → 记001，数字部分一个都不动。

    这里断言的是**精确的 2**，而不是 ``>= 2``。早先只能写 ``>= 2``：
    repad 扫的是整张 voucher 表，而前面的用例会 commit 留下一批凭证 ——
    那些也是「前缀+数字」，同样会被计入，计数本来就可能大于 2，于是这条
    变成「单跑绿、全量红」（2026-10-14 复现过，``assert 10 == 0``）。

    现在给 repad 传了 ``book_id``，它只看本用例的账套，返回值重新变成一个
    确定的东西 —— 精确断言才敢写回来。真正要守的仍然是这两张凭证本身：
    补零没做、对做错了，下面两行照样红。
    """
    a = _mk(db, book, make_voucher, "记0001")
    b = _mk(db, book, make_voucher, "记0022")
    changed, _ = repad_voucher_numbers(db, book_id=book.id)
    assert changed == 2, f"本账套只有这两张四位号，改了几张却不是 2：{changed}"
    db.refresh(a); db.refresh(b)
    assert a.voucher_no == "记001"     # 数字没变，只是少了一个零
    assert b.voucher_no == "记022"


def test_已经三位的不动(db, book, make_voucher):
    a = _mk(db, book, make_voucher, "记007")
    assert repad_voucher_numbers(db, book_id=book.id)[0] == 0
    db.refresh(a)
    assert a.voucher_no == "记007"


def test_补零不会撞号(db, book, make_voucher):
    """补零在整数上是一一对应的，落到唯一约束上就不会撞。"""
    a = _mk(db, book, make_voucher, "记0007")
    repad_voucher_numbers(db, book_id=book.id)
    db.refresh(a)
    nos = [v.voucher_no for v in db.query(Voucher).filter(Voucher.book_id == book.id).all()]
    assert nos.count(a.voucher_no) == 1


def test_手工号原样不动(db, book, make_voucher):
    """非「前缀 + 数字」的号是人工起的，不该被这套补零逻辑改写。"""
    a = _mk(db, book, make_voucher, "记-年结")
    b = _mk(db, book, make_voucher, "2026年终")
    assert repad_voucher_numbers(db, book_id=book.id)[0] == 0
    db.refresh(a); db.refresh(b)
    assert a.voucher_no == "记-年结" and b.voucher_no == "2026年终"


def test_跑第二遍是零改动(db, book, make_voucher):
    _mk(db, book, make_voucher, "记0003")
    assert repad_voucher_numbers(db, book_id=book.id)[0] == 1
    assert repad_voucher_numbers(db, book_id=book.id)[0] == 0     # 幂等


def test_已结账期间的号不动(db, book, make_voucher):
    """结账 = 这段账簿定稿，凭证号是账的一部分，不是排版。
    护栏（fn_voucher_guard）本来就禁止改，这里要验的是我们压根不往那儿伸手 ——
    早先的全表写法会被触发器顶回、整个事务回滚，而异常被吞成一行日志，
    页面上看着像「什么都没发生」。"""
    v = _mk(db, book, make_voucher, "记0005")
    book.periods[0].locked = True
    db.flush()

    changed, skipped = repad_voucher_numbers(db, book_id=book.id)
    assert changed == 0
    assert skipped >= 1                      # 跳过的张数是要报出来的，不能闷头吞掉
    db.refresh(v)
    assert v.voucher_no == "记0005"


def test_触发器确实拦得住(db, book, make_voucher):
    """补一道：证明上面那条「不动」不是靠运气，是护栏本来就拦。"""
    from sqlalchemy.exc import DBAPIError
    v = _mk(db, book, make_voucher, "记0005")
    book.periods[0].locked = True
    db.flush()
    v.voucher_no = "记005"
    try:
        db.flush()
        raised = False
    except DBAPIError:
        db.rollback()
        raised = True
    assert raised, "已结账期间的凭证居然改成功了，护栏失效"


def test_别处有脏数据也不改变本账套的计数(db, book, make_voucher):
    """**这条是回归守卫**，钉住 2026-10-14 那次全量跑的偶发失败。

    测试库里各用例的账套是逐个累积的（``db`` 夹具的 rollback 被夹具内部的
    commit 架空，这是仓库既有设计，见 conftest 的说明）。补零以前扫的是整张
    voucher 表，于是别处留下的四位号会被算进「本次改了几张」—— 那条
    ``test_跑第二遍是零改动`` 在全量跑时就红过（``assert 10 == 0``），而单独
    跑必绿。

    这条用例**主动把脏数据摆出来**，不靠 xdist 恰好怎么分发：
    先在另一个账套塞 10 张四位号并提交（commit，模拟别的用例留下的残留），
    再看本账套的精确计数。
    """
    from app.accounting.periods import create_periods
    from app.models import Book
    from app.seed.chart_of_accounts import install_chart

    # ---- 造脏数据：另一个账套 + 10 张四位号，commit 落库 ----
    dirty = Book(company_name="脏数据公司", name="脏数据账套",
                 start_year=2026, start_month=1)
    db.add(dirty)
    db.flush()
    create_periods(db, dirty)
    install_chart(db, dirty.id)
    subs = {s.code: s.id for s in db.query(Subject).filter(Subject.book_id == dirty.id)}
    for i in range(10):
        v = Voucher(
            book_id=dirty.id,
            period_id=dirty.periods[0].id,
            voucher_no=f"记{i + 1:04d}",          # 四位号，正是会被补零的目标
            voucher_date=date(2026, 1, 5),
            status="UNPOSTED", source="MANUAL", remark="脏数据",
            attachments=0, created_by="脏数据", posted_by=None,
        )
        v.entries = [
            Entry(subject_id=subs["1002"], line_no=1,
                  direction="DEBIT", amount=Decimal("1"), summary="脏"),
            Entry(subject_id=subs["5001"], line_no=2,
                  direction="CREDIT", amount=Decimal("1"), summary="脏"),
        ]
        db.add(v)
    db.commit()                                  # ← 真的落库，模拟别的用例留下的残留

    # ---- 关键断言：脏数据在场，本账套的计数仍然精确 ----
    _mk(db, book, make_voucher, "记0003")
    assert repad_voucher_numbers(db, book_id=book.id)[0] == 1, (
        "本账套只有一张四位号，改了几张却不是 1 —— 说明计数被别处的脏数据污染了")
    assert repad_voucher_numbers(db, book_id=book.id)[0] == 0, "第二次跑不是幂等"
