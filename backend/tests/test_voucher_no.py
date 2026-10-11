"""凭证号补零：位数收窄到三位，且只补零、不改数字。"""
from app.models import Voucher
from app.seed.voucher_no import repad_voucher_numbers


def _mk(db, book, make_voucher, no):
    v = make_voucher("2026-01-05", dr=[("100201", 100, "x")], cr=[("600101", 100, "x")])
    v.voucher_no = no
    db.flush()
    return v


def test_四位号补成三位(db, book, make_voucher):
    a = _mk(db, book, make_voucher, "记0001")
    b = _mk(db, book, make_voucher, "记0022")
    changed, _ = repad_voucher_numbers(db)
    assert changed == 2
    db.refresh(a); db.refresh(b)
    assert a.voucher_no == "记001"     # 数字没变，只是少了一个零
    assert b.voucher_no == "记022"


def test_已经三位的不动(db, book, make_voucher):
    a = _mk(db, book, make_voucher, "记007")
    assert repad_voucher_numbers(db)[0] == 0
    db.refresh(a)
    assert a.voucher_no == "记007"


def test_补零不会撞号(db, book, make_voucher):
    """补零在整数上是一一对应的，落到唯一约束上就不会撞。"""
    a = _mk(db, book, make_voucher, "记0007")
    repad_voucher_numbers(db)
    db.refresh(a)
    nos = [v.voucher_no for v in db.query(Voucher).filter(Voucher.book_id == book.id).all()]
    assert nos.count(a.voucher_no) == 1


def test_手工号原样不动(db, book, make_voucher):
    """非「前缀 + 数字」的号是人工起的，不该被这套补零逻辑改写。"""
    a = _mk(db, book, make_voucher, "记-年结")
    b = _mk(db, book, make_voucher, "2026年终")
    assert repad_voucher_numbers(db)[0] == 0
    db.refresh(a); db.refresh(b)
    assert a.voucher_no == "记-年结" and b.voucher_no == "2026年终"


def test_跑第二遍是零改动(db, book, make_voucher):
    _mk(db, book, make_voucher, "记0003")
    assert repad_voucher_numbers(db)[0] == 1
    assert repad_voucher_numbers(db)[0] == 0     # 幂等


def test_已结账期间的号不动(db, book, make_voucher):
    """结账 = 这段账簿定稿，凭证号是账的一部分，不是排版。
    护栏（fn_voucher_guard）本来就禁止改，这里要验的是我们压根不往那儿伸手 ——
    早先的全表写法会被触发器顶回、整个事务回滚，而异常被吞成一行日志，
    页面上看着像「什么都没发生」。"""
    v = _mk(db, book, make_voucher, "记0005")
    book.periods[0].locked = True
    db.flush()

    changed, skipped = repad_voucher_numbers(db)
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
