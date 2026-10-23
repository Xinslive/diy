"""凭证侧的两处护栏：结转凭证的**签名**与零金额分录（审查修复）。

* **签名不可伪造**（``accounting/periods._is_system_carry_forward``）
  —— 原来「系统生成」的判据只有 remark 尾部 + 分录摘要，**两样都是人能
  写对的**。伪造一张已记账的结转凭证，「重新结转损益」就会把它删掉 ——
  而删除保护网对 CARRY_FORWARD 开的正是这个口子。
  现在 remark 末尾多一段 ``#<16 位十六进制>``，它是服务端密钥算出来的
  HMAC：**没有密钥算不出来**。
  但**存量凭证必须仍然认得出来** —— 一次上线不能把所有存量账套的
  「重新结转」都变成「来源不明的结转凭证」。

* **零金额分录逐条被拒**（``accounting/vouchers._validate_entries``）
  —— 以前只在**所有**分录都为 0 时报错，「借 100 / 贷 100 / 贷 0」
  这种照样能存进去。数据库那道 ``ck_entry_amount_nonzero`` 其实拦得住，
  但它抛的是数据库原文；应用层先拦一道，并且**说清是哪一行**。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models import Period, Voucher


def _period(db, book, month: int) -> Period:
    p = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == month))
    assert p is not None
    return p


def _carry_vouchers(db, period) -> list[Voucher]:
    return list(db.scalars(select(Voucher).where(
        Voucher.period_id == period.id, Voucher.source == "CARRY_FORWARD")).all())


def _make_carry_forward_voucher(db, book, admin_user, subjects, *, remark: str):
    """造一张「长得像系统结转凭证」的凭证（用来模拟伪造）。"""
    from app.accounting.vouchers import create_voucher

    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 31),
        entries=[
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "1000.00", "summary": "期间损益结转"},
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "1000.00", "summary": "期间损益结转"},
        ],
        source="CARRY_FORWARD", remark=remark, commit=False)
    db.commit()
    return v


# --------------------------------------------------------------------- 1. 签名


def test_结转凭证带上不可猜的签名(db, book, admin_user, subjects, make_voucher):
    """系统生成的结转凭证，remark 末尾必须有一段算不出来的签名。"""
    from app.accounting import periods as svc

    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    period = _period(db, book, 1)
    db.commit()

    result = svc.carry_forward_profit(db, book.id, period, admin_user)
    assert result["vouchers"], "前置：应当生成结转凭证"

    for v in _carry_vouchers(db, period):
        assert "#" in v.remark, f"结转凭证没有签名尾巴：{v.remark}"
        token = v.remark.rsplit("#", 1)[1]
        assert len(token) == 16, token
        assert all(c in "0123456789abcdef" for c in token), token


def test_签名对不上的凭证不会被重新结转删掉(db, book, admin_user, subjects,
                                           make_voucher):
    """remark 与摘要都抄对了、只有签名是乱写的 → **不是**系统生成的。

    修好之前：这张凭证会被「重新结转」悄悄删掉（而它可能已经记账）。
    """
    from app.accounting import periods as svc

    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    forged = _make_carry_forward_voucher(
        db, book, admin_user, subjects,
        remark="2026-01 结转损益（收入类） #0000000000000000")
    forged_id = forged.id
    period = _period(db, book, 1)
    db.commit()

    with pytest.raises(ValueError) as exc:
        svc.carry_forward_profit(db, book.id, period, admin_user)
    assert "来源不明" in str(exc.value), str(exc.value)
    db.rollback()
    db.expire_all()

    assert db.get(Voucher, forged_id) is not None, \
        "签名对不上的凭证被重新结转删掉了 —— 那正是这道护栏要防的事"


def test_存量的老签名凭证仍然认得出来(db, book, admin_user, subjects, make_voucher):
    """**没有**签名尾巴的老凭证（升级前生成的）仍然认 —— 存量数据不能废。

    这是向后兼容那条硬约束：一次上线不能把所有存量账套的「重新结转」
    都变成「来源不明的结转凭证」。
    """
    from app.accounting import periods as svc

    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    legacy = _make_carry_forward_voucher(
        db, book, admin_user, subjects, remark="2026-01 结转损益（收入类）")
    legacy_id = legacy.id
    period = _period(db, book, 1)
    db.commit()

    result = svc.carry_forward_profit(db, book.id, period, admin_user)
    assert result["vouchers"], "老样式的结转凭证应当被认出来并替换掉"
    assert result["removed"], "老凭证应当被删掉重做"
    db.expire_all()
    assert db.get(Voucher, legacy_id) is None


def test_签名时代之后的无签名凭证判为伪造(db, book, admin_user, subjects,
                                        make_voucher):
    """系统在本账套生成过带签名的凭证之后，再冒出来的老样式凭证是伪造的。

    「兼容老签名」不能变成一条永久的后门：老样式与「升级之后伪造的
    老样式」长得一模一样，分界线是**这个账套里第一张带签名的凭证 id**
    —— 系统从那一刻起生成的每一张都带签名，之后的无签名凭证只可能是
    伪造的。
    """
    from app.accounting import periods as svc

    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    period = _period(db, book, 1)
    db.commit()

    # 先做一次真正的结转 → 签名时代成立
    svc.carry_forward_profit(db, book.id, period, admin_user)
    db.commit()

    forged = _make_carry_forward_voucher(
        db, book, admin_user, subjects, remark="2026-01 结转损益（收入类）")
    forged_id = forged.id
    db.commit()

    with pytest.raises(ValueError, match="来源不明"):
        svc.carry_forward_profit(db, book.id, period, admin_user)
    db.rollback()
    db.expire_all()
    assert db.get(Voucher, forged_id) is not None, "伪造凭证被重新结转删掉了"


def test_连续两次结转都成功(db, book, admin_user, subjects, make_voucher):
    """正向路径：签名检查不能把真正的结转凭证也拦下。

    「月末结完发现金额不对，改完再结一次」是日常动作（replace 语义）。
    """
    from app.accounting import periods as svc

    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    period = _period(db, book, 1)
    db.commit()

    first = svc.carry_forward_profit(db, book.id, period, admin_user)
    second = svc.carry_forward_profit(db, book.id, period, admin_user)
    assert first["vouchers"] and second["vouchers"]
    assert len(_carry_vouchers(db, period)) == len(second["vouchers"]), \
        "重新结转之后结转凭证应当被替换而不是叠加"


# --------------------------------------------------------------------- 2. 零金额分录


def test_金额为0的分录逐条被拒并带行号(db, book, admin_user, subjects):
    """借 100 / 贷 100 / 贷 0 —— 合计是平的，但第三行是空的。"""
    from app.accounting.vouchers import create_voucher

    with pytest.raises(ValueError) as exc:
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
            entries=[
                {"subject_id": subjects["1001"].id, "direction": "DEBIT",
                 "amount": "100.00", "summary": "x"},
                {"subject_id": subjects["5001"].id, "direction": "CREDIT",
                 "amount": "100.00", "summary": "x"},
                {"subject_id": subjects["1002"].id, "direction": "CREDIT",
                 "amount": "0.00", "summary": "x"},
            ], commit=False)
    message = str(exc.value)
    assert "第 3 行" in message, f"报错要点名是第几行：{message}"
    assert "不能是 0" in message, message
    db.rollback()


def test_红字冲销与同侧相抵不受影响(db, book, admin_user, subjects):
    """负数是红字冲销，正负相抵也是平的 —— 都不能被「金额为 0」误伤。"""
    from app.accounting.vouchers import create_voucher

    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=[
            {"subject_id": subjects["1001"].id, "direction": "DEBIT",
             "amount": "111.00", "summary": "x"},
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "-111.00", "summary": "x"},
        ], commit=False)
    assert v.id
    db.rollback()
