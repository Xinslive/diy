"""金额对外的**唯一**形态：两位小数的字符串。

**为什么要有这一份**
------------------
同一个金额在系统里有好几条通道要出去：页面（报表单元格）、Excel 导出、
HTML 打印、MCP 的 ``render_report``。审计 §3.26 的原症状就是「屏幕按两位
舍入、导出保留原始 Decimal」—— 两处对不上，而且没有任何提示；修法不是给每条
通道各写一份舍入，而是**在这里定一次**，所有通道引用同一份
（审计 §4.5：同一个量只允许一个权威实现）。

具体到报表：``app/reports/evaluate.py`` 是报表数值的**唯一生产者**，
页面 / 打印预览 / Excel 导出 / MCP 全部读它同一个输出。所以口径定在
``evaluate`` 那一侧，四条通道就自动一致 —— 改一处，四处同时改。

**两条硬约束**

* **全程 Decimal，一次 float 都不经过**：金额在库里是 ``Numeric(20,2)``、
  在计算链路里是 Decimal，退回 float 等于在最末端把精度丢掉（大额只剩
  15~17 位有效数字，逐格累加还会漂）。
* **以字符串承载**：JSON 里的浮点数是二进制近似，agent / 前端拿到的数要能
  和界面**逐字**对上（``12800.00`` 转成 float 再转回字符串会得到
  ``12800.000000000002``）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

#: 金额的小数位：两位。
MONEY_SCALE = Decimal("0.01")

#: 舍入规则：**四舍六入五成双**（HALF_EVEN）。
#:
#: 为什么是它而不是「四舍五入」：这套口径原本就在 MCP 通道上跑着
#: （旧 ``app/mcp/caliber.py`` 的 ``money_str``，用的就是 Decimal 的默认
#: 上下文舍入）。报表通道要跟它对齐，就沿用它 —— 改规则会让**已经在用**的
#: 那条通道变形，而「四舍六入五成双」本身也是会计准则（IAS 8）在汇总口径上
#: 推荐的写法，聚合时偏差最小。
#:
#: 规则写在这里（而不是靠 Decimal 的默认值）是刻意的：默认值会变，
#: 金额的口径不能跟着变。
MONEY_ROUNDING = ROUND_HALF_EVEN


def quantize_money(value) -> Decimal:
    """金额 → 两位小数的 Decimal（全程 Decimal，一次 float 都不经过）。

    位数超过 Decimal 上下文的精度（默认 28 位有效数字）时 ``quantize`` 会抛
    ``InvalidOperation``；那种值**原样返回** —— 宁可多给几位，也不要在这里
    把整张报表炸掉。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，但**不是金额**。不挡的话 ``Decimal(str(True))``
        # 抛的是 InvalidOperation —— 那个报错看不出「传错了类型」。
        raise TypeError("金额不能是布尔值")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    try:
        return value.quantize(MONEY_SCALE, rounding=MONEY_ROUNDING)
    except InvalidOperation:
        return value


def money_str(value) -> str:
    """金额 → 两位小数的字符串（``None`` 记作 ``0.00``）。"""
    if value is None:
        return "0.00"
    return f"{quantize_money(value):f}"


def money_str_or_text(value) -> str:
    """金额 → 两位小数的字符串；**不是金额就原样返回**。

    公式可能留下文本（公式错误说明、模板里的占位符），那些不是金额 ——
    硬转成 ``0.00`` 会把「这里有错」变成「这里是零」。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    try:
        d = Decimal(text)
    except (ArithmeticError, ValueError):
        return str(value)
    return f"{quantize_money(d):f}"
