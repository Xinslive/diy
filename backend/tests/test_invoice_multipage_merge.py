"""分页发票（一张发票拆成几页印）要合成**一张**发票。

增值税电子发票的「分页」票会把一张票拆成几页印：每页都印着**同一个发票
号码**、同一个买卖方，每页只有自己那几行明细，价税合计只印在最后一页。

解析层原来「一页一张发票」，于是 3 页的票被当成 3 张发票 —— 它们号码相同，
于是「本次上传中有重复发票号码，无法导入」把整批拦下。用户明明只传了
一张票，却被告知重复。

这里钉住三件事：

1. **同一张票的多个页合成一张发票**（明细接起来，价税合计取总计那一页）；
2. **同一张票传两遍照旧报重复** —— 合成不能把判重放宽；
3. **页脚「共N页 第M页」是硬证据**，没有它时靠「各页明细互补」判。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.invoice.text_parser import (
    merge_invoice_pages, pagination_marker, parse_page,
)

#: 发票抬头（每页都一样 —— 票面就是这么印的）
HEAD = """电子发票（普通发票）
发票号码：24417000000033333333
开票日期：2026年07月15日
某某商贸有限公司
91420100MA1234567X
某某供应链有限公司
91310000MA2AYXC79Y
项目名称 规格型号 单位 数量 单价 金额 税率/征收率 税额
"""

INVOICE_NO = "24417000000033333333"

#: 第 1 页：只有自己那几行明细与本页小计，**没有**价税合计
PAGE1 = HEAD + """*水果*蜜梨 约500g 份 1 10.00 10.00 10% 1.00
合 计
小 计
¥30.00 ¥3.00
共2页 第1页
"""

#: 第 2 页：末页，印着整张票的价税合计
PAGE2 = HEAD + """*水果*苹果 约500g 份 1 20.00 20.00 10% 2.00
合 计
小 计
¥30.00 ¥3.00
价税合计（大写）叁拾叁圆整 （小写）¥33.00
共2页 第2页
"""


def _pages(*texts: str) -> list:
    return [parse_page(i, t) for i, t in enumerate(texts)]


# ------------------------------------------------------------------ 页脚标记


def test_页脚标记认得出共几页第几页():
    assert pagination_marker("……共3页 第2页") == (3, 2)
    assert pagination_marker("共12页第12页") == (12, 12)


def test_没有页脚标记时不当成有():
    assert pagination_marker("发票号码：12345") is None
    # 只有「第3页」没有「共N页」—— 那半句不说明任何事
    assert pagination_marker("第3页") is None
    # 页码比总页数还大：不是分页票，是别的「第M页」
    assert pagination_marker("共2页 第5页") is None


# ------------------------------------------------------------------ 合成


def test_分页票合成一张发票():
    """两页 → 一张，明细接起来，价税合计取末页那个。"""
    merged = merge_invoice_pages(_pages(PAGE1, PAGE2))
    assert len(merged) == 1, f"两页的同一张票被当成了 {len(merged)} 张"
    invoice = merged[0]
    assert invoice.meta.invoice_no == INVOICE_NO
    assert invoice.page_index == 0
    assert invoice.page_span == 2
    assert len(invoice.lines) == 2, "两页的明细都要留下"
    assert invoice.meta.total_incl_tax == Decimal("33.00")
    assert invoice.meta.total_excl_tax == Decimal("30.00")
    assert invoice.meta.total_tax == Decimal("3.00")


def test_合成之后金额校验要通过():
    """合成的目的就是让「明细之和 = 价税合计」重新成立。

    单看第 1 页是 10.00 + 1.00 = 11.00，而票上写着 33.00 —— 每页单独看
    都不成立；**合起来**才成立。这正是原来那句「金额校验未通过」的由来。
    """
    merged = merge_invoice_pages(_pages(PAGE1, PAGE2))
    assert merged[0].parse_warning == "", f"合成后还在报警：{merged[0].parse_warning}"


def test_每页自己的金额警告不算进合成后的那张():
    """第 1 页那条「金额校验未通过」比的是**本页**小计，合成后不再成立。"""
    pages = _pages(PAGE1, PAGE2)
    assert "金额校验未通过" in pages[0].parse_warning, "前置：单页本来就该报"
    merged = merge_invoice_pages(pages)
    assert "金额校验未通过" not in merged[0].parse_warning


# ------------------------------------------------------------------ 跨页折扣


def test_跨页的折扣行只并一次():
    """折扣行的项目印在上一页末尾 —— 合成时不能把它并两次。

    折扣合并是**原地改金额**的（``prev.amount += discount.amount``）。
    如果「判是不是同一张票」那一步也调一次它、合成那一步再调一次，
    跨页的折扣行就被并了两遍：10.00 被减成 6.00 而不是 8.00。
    """
    p1 = HEAD + """*水果*蜜梨 约500g 份 1 10.00 10.00 10% 1.00
合 计
小 计
¥28.00 ¥2.80
共2页 第1页
"""
    p2 = HEAD + """*水果*蜜梨 -2.00 10% -0.20
*水果*苹果 约500g 份 1 20.00 20.00 10% 2.00
合 计
小 计
¥28.00 ¥2.80
价税合计（大写）叁拾圆捌角整 （小写）¥30.80
共2页 第2页
"""
    merged = merge_invoice_pages(_pages(p1, p2))
    assert len(merged) == 1
    lines = merged[0].lines
    assert len(lines) == 2, f"折扣行应该并进项目行，还剩 {len(lines)} 行"
    pear = next(line for line in lines if "蜜梨" in line.name)
    assert pear.amount == Decimal("8.00"), f"蜜梨金额是 {pear.amount} —— 折扣被并了两次？"
    assert pear.tax == Decimal("0.80"), f"蜜梨税额是 {pear.tax} —— 折扣被并了两次？"
    assert merged[0].meta.total_incl_tax == Decimal("30.80")
    assert merged[0].parse_warning == "", merged[0].parse_warning


# ------------------------------------------------------------------ 判重不被放宽


def test_同一张票传两遍不会合成一张():
    """两遍各 2 页 → 仍然是**两张**发票（号码相同），查重照旧能报出来。

    合成如果越界，这两条就会被合成一张 4 页的票 —— 用户传了两遍，
    账上却只多一张发票，静默吞掉一次重复上传。
    """
    merged = merge_invoice_pages(_pages(PAGE1, PAGE2, PAGE1, PAGE2))
    assert len(merged) == 2, f"两遍被合成成了 {len(merged)} 张 —— 判重被放宽了"
    assert merged[0].meta.invoice_no == merged[1].meta.invoice_no == INVOICE_NO
    assert merged[0].page_span == 2 and merged[1].page_span == 2


def test_分页标记让第二遍另起一组():
    """第二遍的第 1 页自称「第1页」—— 那是另一张票的第一页。"""
    merged = merge_invoice_pages(_pages(PAGE1, PAGE2, PAGE1, PAGE2))
    assert [i.page_index for i in merged] == [0, 2], "两组应该各占一段页码"


# ------------------------------------------------------------------ 不该合的


def test_相邻但号码不同的两页不合成():
    merged = merge_invoice_pages(_pages(PAGE1, PAGE2.replace(INVOICE_NO, "24417000000044444444")))
    assert len(merged) == 2
    assert all(i.page_span == 1 for i in merged)


def test_没有页脚标记时靠明细互补判():
    """去掉「共N页 第M页」，但各页明细加起来正好是票上的合计 → 仍然合成。

    这是给**没有页脚标记**的分页票留的判据：票面说不清自己是几页的，
    但「这几页的明细加来正好是整张票」一样能认出来。
    """
    p1 = PAGE1.replace("共2页 第1页\n", "")
    p2 = PAGE2.replace("共2页 第2页\n", "")
    merged = merge_invoice_pages(_pages(p1, p2))
    assert len(merged) == 1, "明细互补的两页没合成"
    assert merged[0].meta.total_incl_tax == Decimal("33.00")
    assert merged[0].parse_warning == ""


def test_明细对不上又没有页脚标记时不合成():
    """两条判据都不成立 → 不合并，留给查重去报「重复发票号码」。

    这一条是**兜底**：宁可让用户看到「重复」这个可能不准确的提示，
    也不能把两次上传悄悄合成一张票。
    """
    p1 = PAGE1.replace("共2页 第1页\n", "").replace("10.00 10.00 10% 1.00", "10.00 10.00 10% 9.90")
    p2 = PAGE2.replace("共2页 第2页\n", "").replace("20.00 20.00 10% 2.00", "20.00 20.00 10% 9.90")
    merged = merge_invoice_pages(_pages(p1, p2))
    assert len(merged) == 2, "对不上的两页不该合成"


def test_单页发票原样通过():
    """普通一张票：不受合成逻辑影响。"""
    merged = merge_invoice_pages(_pages(PAGE2.replace("共2页 第2页\n", "")))
    assert len(merged) == 1
    assert merged[0].page_span == 1
