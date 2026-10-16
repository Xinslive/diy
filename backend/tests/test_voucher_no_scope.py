"""凭证发号的两个脆弱假设（审计 P2-07）。

审计把这条标成 C 级「机制脆弱 + 需复核」，说探针输出与预期失败模式
不完全吻合、怀疑还有第三个未识别因素。这里就是那次复核：

**不需要第三个因素，是审计的预期写错了。** 探针（单前缀、固定宽度、
只改变已存在凭证号）13 组里 **10 组结果错误**：

    库里只有      期望      修复前实测
    记009         记010     记010   ✓
    记099         记100     记100   ✓
    记100         记101     记001   ✗
    记199         记200     记100   ✗
    记999         记1000    记100   ✗
    记1000        记1001    记001   ✗
    银009         银010     记010   ✗   固定偏移 3 吃掉了前缀
    银存009       银存010   记010   ✗
    记账凭证009    记010     记001   ✗   前缀 4 字，截断落在数字里
    记100 + 记99  记101     记010   ✗   文本 max 是字典序
    记-2          记001     记003   ✗   手工号被当成序号基准

注意前两行「看起来是对的」纯属巧合：``记009`` → substring 得 ``"09"``，
丢掉的首位零刚好被三位补零补了回来。序号一旦到三位（记100），
被吃掉的就是实打实的一位数字，于是 ``int("00") + 1 = 1`` → 记001。

所以这条不是「需复核」，是**确认缺陷**，而且审计低估了范围。

补零位数 2026-10-16 由三位改回四位（``organize.NO_WIDTH``），所以**发出去的号**
现在是「记0001」这一路；下面表格里的期望值是当年（三位）的记录，留着是为了对照
「修复前发错成什么样」—— 现在同样的输入会发成记0010、记0100 这样。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.accounting.vouchers import next_voucher_no
from app.models import Period, Subject, Voucher


@pytest.fixture
def period(db, book, periods):
    return db.get(Period, periods[2].id)


@pytest.fixture
def seed(db, book, period, admin_user):
    """把该期间清空，塞进指定的凭证号。"""
    def _seed(nos):
        for v in db.scalars(select(Voucher).where(Voucher.period_id == period.id)).all():
            db.delete(v)
        db.flush()
        for n in nos:
            db.add(Voucher(book_id=book.id, period_id=period.id, voucher_no=n,
                           voucher_date=date(2026, 2, 1), status="UNPOSTED",
                           source="MANUAL", created_by=admin_user.id))
        db.flush()
        return _seed
    return _seed


def _sid(db, book, code):
    return db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == code))


# --------------------------------------------------------------------- 序号边界


@pytest.mark.parametrize("existing,expect", [
    ([], "记0001"),
    (["记001"], "记0002"),
    (["记009"], "记0010"),           # 审计里「看起来对」的那行
    (["记099"], "记0100"),
    (["记100"], "记0101"),           # ← 修复前发出 记001
    (["记199"], "记0200"),           # ← 修复前发出 记100
    (["记998"], "记0999"),
    (["记999"], "记1000"),           # ← 修复前发出 记100（1000 本来就够四位）
    (["记1000"], "记1001"),          # ← 修复前发出 记001
    (["记001", "记002", "记010"], "记0011"),
    (["记100", "记99"], "记0101"),   # ← 修复前发出 记010
    (["记009", "记0100"], "记0101"), # 混宽度：按数值取最大，不是按字符串
    (["记007", "记008", "记009"], "记0010"), # 删过中间的号，仍按最大值续
])
def test_按数值取下一个号(db, seed, period, existing, expect):
    seed(existing)
    assert next_voucher_no(db, period) == expect


# --------------------------------------------------------------------- 前缀宽度


@pytest.mark.parametrize("prefix", ["记", "银", "银存", "记账凭证"])
def test_各种前缀宽度都按传入的前缀发号(db, seed, period, prefix):
    """``substring(no, 3)`` 固定从第 3 字截，等于假设前缀恰好 2 个汉字。

    而 ``organize.NO_PATTERN`` 允许 1~4 个非数字字符，导入也允许用户
    自带凭证号 —— 非标准前缀在真实数据里确实存在。
    """
    seed([f"{prefix}009"])
    assert next_voucher_no(db, period, prefix) == f"{prefix}0010"


def test_前缀一字且序号三位时不丢位(db, seed, period):
    """修复前：``银100`` → substring 得 "00" → 发出 银001。"""
    seed(["银100"])
    assert next_voucher_no(db, period, "银") == "银0101"


# --------------------------------------------------------------------- 手工号


def test_手工号不参与发号(db, seed, period):
    """``记-年结`` 解析不出序号，不该把自动号跳到没人用过的空档上。

    修复前 substring("记-年结", 3) = "-年结" 不是数字 → seq=1，这条恰好对；
    但 ``记-2`` 会被当成序号 2 → 发出 记003。
    """
    seed(["记-2", "记-年结"])
    assert next_voucher_no(db, period) == "记0001"


def test_手工号与自动号混存时以自动号为准(db, seed, period):
    seed(["记-9", "记005", "2026年终"])
    assert next_voucher_no(db, period) == "记0006"


# --------------------------------------------------------------------- 与落库一致


def test_连续发号不撞号(db, book, period, admin_user):
    """发号 → 建凭证 → 再发号，一路下来不能撞上已有的号。

    这是真正会伤到用户的一条：撞号会被唯一约束拒掉，用户看着
    「明明还有空号」却存不进去。
    """
    from app.accounting.vouchers import create_voucher

    used = []
    for i in range(12):
        no = next_voucher_no(db, period)
        assert no not in used, f"第 {i + 1} 次发出重复的号 {no}"
        used.append(no)
        v = create_voucher(db, book_id=book.id, user=admin_user,
                           voucher_date=date(2026, 2, 10), entries=[
                               {"subject_id": _sid(db, book, "1002"),
                                "direction": "DEBIT", "amount": "10", "summary": "x"},
                               {"subject_id": _sid(db, book, "5001"),
                                "direction": "CREDIT", "amount": "10", "summary": "x"},
                           ], voucher_no=no, commit=True)
        assert v.voucher_no == no
    assert used[:3] == ["记0001", "记0002", "记0003"]
    assert used[-1] == "记0012"


def test_只发号不落库不会跳号(db, period):
    """next_voucher_no 是纯读函数：调几次都该给同一个号。

    前端「下一个号码」预览和真正落库走的是同一个函数（api/vouchers.py），
    用户在预览和保存之间再点一次别的菜单是很常见的。
    """
    assert next_voucher_no(db, period) == "记0001"
    assert next_voucher_no(db, period) == "记0001"
