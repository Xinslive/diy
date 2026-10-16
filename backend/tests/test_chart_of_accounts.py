"""内置科目表与内置报表必须对得上（编码体系 4-2-2）

**这里防的是哪一种事故。** 内置报表（会小企01表资产负债表、利润表）是**按科目
编码**取数的，而内置科目表是一张独立维护的表。两者一旦各走各的：

* 编码不存在 —— 那一行在报表上永远是空的。用户看到的是「这家公司没有这笔业务」，
  实际上只是编码对不上；
* **同码不同义** —— 4001 从「实收资本」变成「生产成本」、4101 从「盈余公积」变成
  「制造费用」。这个更糟：它不报错，报表会**静默取到另一个科目的数**，钱算错了
  也没人知道。

2026-10 换用友 4-2-2 编码表时就踩了后者：真实账套（星悦荟商管等 1390 个科目的
那几本）本来就是 5001/3001/5601 这一套，而默认表还是 6001/4001/6601 ——
资产负债表上有 18 个编码压根不存在，还有 4 个会取到另一个科目的数。

下面这几条不是形式检查，是这条事故的唯一防线。
"""
from app.seed.builtin_reports import BALANCE_ROWS, INCOME_ROWS
from app.seed.chart_of_accounts import CHART


def _flatten(rows, out=None):
    out = {} if out is None else out
    for code, name, *_rest in rows:
        out[code] = name
        if _rest and _rest[-1]:
            _flatten(_rest[-1], out)
    return out


def _referenced_codes():
    """两张内置报表取数用到的全部科目编码。"""
    codes = set()
    for row in BALANCE_ROWS:
        for side in (row.left, row.right):
            codes.update(c.lstrip("-") for c in (side.codes or ()))
    for _no, _label, _level, _bold, row_codes, _kind in INCOME_ROWS:
        codes.update(c.lstrip("-") for c in (row_codes or []))
    return codes


CHART_NAMES = _flatten(CHART)
REFERENCED = _referenced_codes()


def test_报表引用的编码都在科目表里():
    """少一个就是一行空白 —— 不报错，只是永远没数。"""
    missing = sorted(c for c in REFERENCED if c not in CHART_NAMES)
    assert not missing, f"报表引用了科目表里没有的编码，那一行会永远是空的：{missing}"


def test_编码规则是四二二():
    """用友导出的表是 4-2-2；编码段长度不一致会让导入器判为非法。"""
    widths = {}
    for code in CHART_NAMES:
        level = 4 if len(code) <= 4 else 6 if len(code) <= 6 else 8
        widths.setdefault(level, set()).add(len(code) - (0 if level == 4 else 4 if level == 6 else 6))
    assert widths == {4: {4}, 6: {2}, 8: {2}}, widths


def test_成本类不参与结转损益():
    """成本月末滚进主营业务成本，不是直接结进损益。"""
    carried = []
    for code, name, category, _d, carry, children in CHART:
        if carry:
            carried.append((code, name, category))
        for c in children:
            if c[4]:
                carried.append((c[0], c[1], c[2]))
    assert carried, "损益类科目总得有几个开结转，否则月末结转没东西可结"
    assert all(category == "PROFIT" for _c, _n, category in carried), carried


def test_关键编码的指向别被改回去():
    """这几条是「用友 4-2-2」这个选择的锚点，也是同码不同义的高发区。"""
    anchors = {
        "1101": "短期投资",       # 旧表误当成交易性金融资产
        "1123": "预付账款",       # 旧表误当成预付款项
        "3001": "实收资本",       # 旧表是 4001
        "3101": "盈余公积",       # 旧表是 4101
        "4001": "生产成本",       # 旧表是实收资本 —— 同码不同义的典型
        "4101": "制造费用",       # 旧表是盈余公积
        "5001": "主营业务收入",   # 旧表是 6001
        "5401": "主营业务成本",   # 旧表是 6401
        "5601": "销售费用",       # 旧表是 6601
    }
    for code, name in anchors.items():
        assert CHART_NAMES.get(code) == name, f"{code} 现在是 {CHART_NAMES.get(code)}，应为 {name}"
