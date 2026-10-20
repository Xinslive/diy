"""发票文本解析的容量边界（审查报告 P2-1）。

原来「大写金额」那条正则是 ``([零壹…整]+)\\s*¥\\d+(?:\\.\\d+)?``，直接
``search()`` 全文。载荷是「N 个中文数字 + N 个空白 + 没有 ¥」时，它先贪婪
吃掉 N 个中文数字、再吃掉 N 个空白、然后要求 ¥ 失败；回溯时 ``\\s*`` 逐个
退格，而 ``search()`` 会在**每一个起始位置**重试整段匹配 —— 总代价 O(n²)。

实测：文本层 20 万字符 → 232 秒 CPU；按上传上限 20MB 折算，一次请求能
烧掉约 **9.7 分钟** CPU（发票上传端点是同步 ``def``，跑在 Starlette 的
线程池里，几个并发请求就能把整个应用的所有同步端点一起拖慢）。

现在改成「先找极大中文数字串 + 再看它后面是不是 ¥金额」两步，代价线性。
这里钉住的就是这个性质：**20MB 的输入，处理时间有界**。
"""
from __future__ import annotations

import time

import pytest

from app.invoice.text_parser import extract_meta, find_big_amount, parse_page

#: 20MB 的恶意载荷：1000 万个中文数字 + 1000 万个空白，**没有 ¥**。
#: 正是这条载荷让旧写法变成 O(n²)。
PAYLOAD_CHARS = 20_000_000


@pytest.fixture(scope="module")
def payload() -> str:
    half = PAYLOAD_CHARS // 2
    return "壹" * half + " " * half


def test_大写金额的查找在20MB载荷下是线性时间(payload):
    """旧写法在这一条载荷上要几分钟；现在必须是**有界**的。"""
    start = time.perf_counter()
    found = find_big_amount(payload)
    elapsed = time.perf_counter() - start

    assert found == "", "这条载荷里没有 ¥，不该匹配出大写金额"
    assert elapsed < 5.0, f"20MB 载荷处理了 {elapsed:.1f} 秒 —— 又是二次回溯？"


def test_整页解析在20MB载荷下有界(payload):
    """整条解析路径（有界）：一次请求不能把 CPU 烧掉几分钟。"""
    start = time.perf_counter()
    parse_page(0, payload)
    elapsed = time.perf_counter() - start

    # 旧写法这一条是几百秒；上限放宽到 15 秒是为了不给 CI 抖动留误报的口子
    # （本机实测约 1.1 秒），同时仍然把「几分钟」那种量级挡在外面。
    assert elapsed < 15.0, f"整页解析处理了 {elapsed:.1f} 秒 —— 二次回溯没修掉？"


def test_大写金额仍然认得(payload):
    """修的只是代价，不是功能：正常写法照样要认出来。

    注意「元」**不在**大写数字的字符集里（``[零壹…整]``），所以
    「壹佰零陆元整」这种写法旧正则与新写法都只取到「整」—— 行为一致，
    这里改的只是代价。
    """
    assert find_big_amount("价税合计（大写）壹佰零陆整 ¥106.00") == "壹佰零陆整"
    assert find_big_amount("零壹贰¥5") == "零壹贰"
    assert find_big_amount("壹佰元整") == "", "后面没有 ¥ 就不算大写金额"
    assert find_big_amount("") == ""


def test_元信息解析不受影响(payload):
    """extract_meta 走的是同一条路，也要快。"""
    start = time.perf_counter()
    meta = extract_meta("发票号码：24417000000011111111\n开票日期：2026年10月08日\n"
                         "价税合计（大写）壹佰零陆整 ¥106.00")
    elapsed = time.perf_counter() - start
    assert meta.invoice_no == "24417000000011111111"
    assert meta.total_in_c_c == "壹佰零陆整"
    assert elapsed < 5.0
