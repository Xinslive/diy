"""凭证号里的**号段数字** —— 全系统只有这一份判据。

SQL 表达式（``seq_expr``，列表排序与凭证顺序用它）与 Python 版（``seq_of``，
凭证录入页的时点余额用它）必须对同一个号给出**同一个数**。

两份实现分道扬镳的后果很具体：「翻到的那张」和「排出来的那张」不是同一张 ——
用户在前一张凭证上看到的余额，是按另一张的位置算的。

（同名但不同题目的另一份用例在 ``test_voucher_no.py``：那是「凭证号补零」。）
"""
from __future__ import annotations

from sqlalchemy import literal, select

from app.core.voucher_no import seq_expr, seq_of

#: 正常号、补零号、混着字的号、一个数字都没有的号、结尾不是数字的号。
SAMPLES = [
    "记0001", "记0012", "记0100", "记100", "记",
    "wochakan", "0012", "记 12", "记-3", "A-0007", "12",
]


def test_Python版与SQL表达式给出同一个数(db):
    """两份实现必须掐得一样 —— 这条用例就是那个「一样」的凭据。"""
    for no in SAMPLES:
        sql_value = db.execute(select(seq_expr(literal(no)))).scalar()
        assert sql_value == seq_of(no), (
            f"「{no}」两边对不上：SQL 算出 {sql_value}，Python 算出 {seq_of(no)}")


def test_抠不出数字给负一而不是报错():
    """脏号不拖垮排序：抠不出就排最前。

    库里真有过这种号 —— "wochakan" 让凭证列表的四种排序全部 500
    （2026-10-19 生产事故，见 ``app.core.voucher_no`` 的模块说明）。
    """
    assert seq_of("wochakan") == -1
    assert seq_of("") == -1
    assert seq_of(None) == -1


def test_按整数比而不是按字符串比():
    """凭证号是补零的：按字符串比，"记100" 排在 "记099" 前面。"""
    assert seq_of("记0100") == 100
    assert seq_of("记099") == 99
    assert seq_of("记0100") > seq_of("记099")
