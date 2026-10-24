"""进项发票不单列税额的账务规则。

用户是小微企业，不做进项抵扣：发票上的进项税额没有对应的资产，按约定整笔并入
**第一条明细行**，价税合计直接进成本费用。所以这一条不进 ``2221 应交税费`` 的
进项明细 —— 那正是「不考虑进项税额处理」的实际含义。

销项不受影响：销项税额是向客户收的钱、要交出去，和进项不是一回事。

这两段抽成纯函数（``service._input_entries`` / ``service._output_entries``）就是
为了让账务规则可以被直接断言 —— 金额错在账上不会报错，只会在总账里悄悄错。
所以这里盯三件事：**税额落在哪一行**、**只加一次**、**借贷恒等**。

另一条同样要盯：**一张发票一个凭证，项目逐项列支**。``items`` 传进来的每一项
都要变成一条独立的分录，摘要用发票上的项目名 —— 不按科目合并。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.invoice import service


def _d(value: str) -> Decimal:
    return Decimal(value)


def _items(*rows: tuple[int, str, str]) -> list[dict]:
    """把 (科目, 金额, 摘要) 摊成入账行 —— 与 service 里的 items 同构。"""
    return [{"subject_id": subject, "amount": _d(amount), "summary": summary}
            for subject, amount, summary in rows]


def _sum(entries: list[dict], direction: str) -> Decimal:
    return sum((_d(e["amount"]) for e in entries if e["direction"] == direction), Decimal("0"))


def _balances(entries: list[dict]) -> bool:
    """任何一张凭证都必须借贷相等 —— 这是所有场景的共同前提。"""
    return _sum(entries, "DEBIT") == _sum(entries, "CREDIT")


# --------------------------------------------------------------- 进项：税额去向


def test_单行进项_税额并入该行():
    """买办公用品含税 1060（不含税 1000 + 税 60）：借管理费用 1060，不挂税额科目。"""
    entries = service._input_entries(
        items=_items((1, "1000.00", "办公用品")),
        tax=_d("60.00"),
        settlement_subject_id=100,
        total=_d("1060.00"),
        summary="发票 XX",
    )
    debits = [e for e in entries if e["direction"] == "DEBIT"]
    assert len(debits) == 1, "只有一条借方明细 —— 税额没有单独成行"
    assert debits[0]["subject_id"] == 1
    assert debits[0]["amount"] == "1060.00"
    assert _balances(entries)


def test_进项不再产生任何税额分录():
    """进项路径里不该再出现第二条借方 —— 那是原来「进项税额」科目的位置。"""
    entries = service._input_entries(
        items=_items((1, "1000.00", "办公用品")),
        tax=_d("60.00"),
        settlement_subject_id=100,
        total=_d("1060.00"),
        summary="发票 XX",
    )
    assert all(e["subject_id"] != 2221 for e in entries), "不应挂应交税费科目"
    assert len(entries) == 2, f"应为 1 借 + 1 贷，实际 {len(entries)} 条"


def test_多行进项_税额落在第一条明细行():
    """两行分别记管理费用与租赁费：税全进第一条（管理费用）。"""
    entries = service._input_entries(
        items=_items((1, "1000.00", "办公用品"), (2, "2000.00", "房租")),
        tax=_d("60.00"),
        settlement_subject_id=100,
        total=_d("3060.00"),
        summary="发票 XX",
    )
    amounts = {e["subject_id"]: e["amount"] for e in entries if e["direction"] == "DEBIT"}
    assert amounts == {1: "1060.00", 2: "2000.00"}, amounts
    assert _balances(entries)


def test_同科目的两行也各占一条借方():
    """两行挂同一个科目时**不能合并**。

    合并过一次是原来的做法，但那样凭证上看不见发票买了哪几样东西。
    合并还有一个隐蔽的坏处：两条 500 合成一条 1000，税额「加在第一条」
    就没法判断该加在哪 —— 所以现在是两条借方，各自 500，税额只加第一条。
    """
    entries = service._input_entries(
        items=_items((1, "500.00", "油"), (1, "500.00", "米")),
        tax=_d("100.00"),
        settlement_subject_id=100,
        total=_d("1100.00"),
        summary="发票 XX",
    )
    debits = [e for e in entries if e["direction"] == "DEBIT"]
    assert len(debits) == 2, f"同科目的两行也要各占一条，实际 {len(debits)} 条"
    assert [e["amount"] for e in debits] == ["600.00", "500.00"], "税只加第一条"
    assert _balances(entries)


def test_无税额发票不标含税():
    """免税/零税率发票没有税额可并入，摘要不该挂一个空的「含税」。"""
    entries = service._input_entries(
        items=_items((1, "100.00", "办公用品")),
        tax=_d("0.00"),
        settlement_subject_id=100,
        total=_d("100.00"),
        summary="发票 XX",
    )
    assert all("含税" not in e["summary"] for e in entries)
    assert _balances(entries)


def test_并入税额的行摘要标含税():
    """复核的人要能看出这笔费用为什么比发票明细那行大。"""
    entries = service._input_entries(
        items=_items((1, "1000.00", "办公用品"), (2, "2000.00", "房租")),
        tax=_d("60.00"),
        settlement_subject_id=100,
        total=_d("3060.00"),
        summary="发票 XX",
    )
    marked = {e["subject_id"] for e in entries if e["summary"].endswith("（含税）")}
    assert marked == {1}, f"只有第一条该标含税，实际 {marked}"


def test_明细摘要用发票项目名():
    """摘要里要有项目名 —— 用户核对的是「买的是什么」，不是「发票号是多少」。"""
    entries = service._input_entries(
        items=_items((1, "1000.00", "*油料*粮油米面"), (2, "200.00", "*美容护肤品*日用杂品")),
        tax=_d("0.00"),
        settlement_subject_id=100,
        total=_d("1200.00"),
        summary="发票 XX",
    )
    assert [e["summary"] for e in entries if e["direction"] == "DEBIT"] == [
        "*油料*粮油米面", "*美容护肤品*日用杂品"]
    # 结算行是另一回事：它对应的是整张发票的钱
    credit = [e for e in entries if e["direction"] == "CREDIT"]
    assert len(credit) == 1 and credit[0]["summary"] == "发票 XX"


def test_没有明细时不产生借方():
    """空明细交给上层报「没有可入账的发票明细」，这里不该凭空造一条借方。"""
    entries = service._input_entries(
        items=[],
        tax=_d("60.00"),
        settlement_subject_id=100,
        total=_d("60.00"),
        summary="发票 XX",
    )
    assert [e["direction"] for e in entries] == ["CREDIT"]


# ------------------------------------------------------------------ 销项不受影响


def test_销项税额照旧单列贷方():
    """销项税额是收来的钱，要交出去 —— 不能跟着进项一起被并掉。"""
    entries = service._output_entries(
        items=_items((1, "1000.00", "服务费")),
        tax=_d("60.00"),
        tax_subject_id=2221,
        settlement_subject_id=100,
        total=_d("1060.00"),
        summary="发票 XX",
    )
    tax_lines = [e for e in entries if e["subject_id"] == 2221]
    assert len(tax_lines) == 1, "销项税额应当仍然单独一行"
    assert tax_lines[0]["direction"] == "CREDIT"
    assert tax_lines[0]["amount"] == "60.00"
    assert tax_lines[0]["summary"].endswith("（销项税额）")
    assert _balances(entries)


def test_销项明细逐项列支且摘要用项目名():
    """销项同理：每个项目一行贷方，摘要写项目名。"""
    entries = service._output_entries(
        items=_items((1, "1000.00", "咨询服务"), (2, "500.00", "培训服务")),
        tax=_d("60.00"),
        tax_subject_id=2221,
        settlement_subject_id=100,
        total=_d("1560.00"),
        summary="发票 XX",
    )
    credits = [e for e in entries if e["direction"] == "CREDIT" and e["subject_id"] != 2221]
    assert [e["summary"] for e in credits] == ["咨询服务", "培训服务"]
    assert [e["amount"] for e in credits] == ["1000.00", "500.00"]
    assert _balances(entries)


def test_销项无税额时不产生空行():
    entries = service._output_entries(
        items=_items((1, "100.00", "服务费")),
        tax=_d("0.00"),
        tax_subject_id=2221,
        settlement_subject_id=100,
        total=_d("100.00"),
        summary="发票 XX",
    )
    assert all(e["subject_id"] != 2221 for e in entries)
    assert _balances(entries)


# ---------------------------------------------------------------------- 恒等式


@pytest.mark.parametrize(
    "rows,tax",
    [
        (((1, "1000.00", "甲"),), _d("60.00")),
        (((1, "1000.00", "甲"), (2, "2000.00", "乙")), _d("60.00")),
        (((1, "500.00", "甲"), (1, "500.00", "乙")), _d("100.00")),
        (((1, "0.01", "甲"), (2, "0.02", "乙"), (3, "0.03", "丙")), _d("0.01")),
        (((1, "100.00", "甲"),), _d("0.00")),
        # 整张发票全是免税：明细合计即价税合计
        (((1, "1000.00", "甲"), (2, "2000.00", "乙")), _d("0.00")),
    ],
)
def test_进项凭证恒定借贷平衡(rows, tax):
    """借方合计必须恒等于 Σ明细 + 税额。全程 Decimal，不经过 float。"""
    items = _items(*rows)
    total = sum((item["amount"] for item in items), Decimal("0")) + tax
    entries = service._input_entries(
        items=items, tax=tax, settlement_subject_id=100, total=total, summary="发票 XX",
    )
    assert _balances(entries), (
        f"借贷不平：借 {_sum(entries, 'DEBIT')} vs 贷 {_sum(entries, 'CREDIT')}")


def test_进项税额绝不出现在任何一条分录上():
    """整条规则的存在意义：税额没有被单独列示成一行。"""
    for rows, tax, total in [
        (((1, "1000.00", "甲"),), _d("60.00"), _d("1060.00")),
        (((1, "1000.00", "甲"), (2, "2000.00", "乙")), _d("60.00"), _d("3060.00")),
    ]:
        entries = service._input_entries(
            items=_items(*rows), tax=tax, settlement_subject_id=100,
            total=total, summary="发票 XX",
        )
        # 只有「明细科目 + 结算科目」两类 subject_id，不该出现第三个
        assert {e["subject_id"] for e in entries} <= {1, 2, 100}
