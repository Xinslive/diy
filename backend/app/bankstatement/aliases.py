"""对账单的列名别名表，以及「这一列是哪一列」的判据。

这一张表是整个解析器里**唯一**与具体银行有关的东西，而它写的也不是
「某某银行的表头叫啥」，而是**概念**：

    收入 ← 收入 / 存入金额 / 贷方发生额 / 贷方 / 收入金额
    支出 ← 支出 / 支出金额 / 借方发生额 / 借方 / 付出
    …

九家银行，同一个概念叫法完全不同，但**概念**只有十几个。所以银行差异
被压进这张表，而不是散成一堆 ``if 银行 == ...`` 的分支 —— 将来多一家银行，
多半只是往这张表里加几个别名。

**关于「借/贷」的方向，务必记住这一条**（写错会让每一行的方向都反过来）::

    银行账：贷方发生额 = 账户余额**增加** = 收入
    会计上：银行存款增加记**借方**

    所以本模块恒定：借方 = 支出（OUT），贷方 = 收入（IN）。

两套借贷方向是**相反**的，这是对账单解析最容易踩的坑，解析结果里的
``direction`` 一律按「账户余额增减」定义，不按会计借贷定义。
"""
from __future__ import annotations

import re

#: 内部字段 → 表头别名（越靠前越优先；匹配时先精确后包含）。
#:
#: 别名里刻意不写「交易类型」「现转标志」这类**可能**含方向含义的列：
#: 农商的「现转标志」值是「转帐」（不含方向），平安的「交易类型」值是
#: 「转入/转出」（含方向）。把它们当方向列会误判，所以归到 EXTRA，
#: 方向由 :func:`app.bankstatement.semantics.resolve_direction` 的多条通路
#: 自己去认值。
ALIASES: dict[str, tuple[str, ...]] = {
    # 「记账日」**不在** DATE 里：建行的表头同时给「记账日」和「交易日期」，
    # 业务上该用交易日期（记账日是银行入账那天，可能晚几天）。它归
    # POSTING_DATE；真遇到只有记账日的表，语义层会把 POSTING_DATE 顶到 DATE。
    "DATE": ("交易日期", "交易日", "业务日期", "发生日期", "入账日期", "日期"),
    "POSTING_DATE": ("记账日", "入账日", "记账日期"),
    "TIME": ("交易时间", "时间", "交易时分秒"),
    "IN": ("收入", "存入金额", "存金额", "贷方发生额", "贷方金额", "贷方", "收入金额", "存入"),
    "OUT": ("支出", "支出金额", "付金额", "借方发生额", "借方金额", "借方", "支出发生额", "付出"),
    "AMOUNT": ("交易金额", "发生额", "金额", "交易额"),
    "BALANCE": ("账户余额", "余额", "账面余额", "交易后余额", "当前余额"),
    "DIRECTION": ("借/贷", "借贷方向", "收支方向", "借贷标志", "收付标志", "收付方向",
                  "收入/支出", "借贷", "方向"),
    # 付款方/收款方**不进** CP_NAME/CP_ACCOUNT：平安那种表两个方向各有一列，
    # 混进 CP_NAME 就分不出「哪一边是对方」了，方向判定会失效。它们单列。
    "CP_NAME": ("对方户名", "对方名称", "对方账号名称", "对方单位名称", "对方账户名称",
                "交易对手名称", "对方名"),
    "CP_ACCOUNT": ("对方账号", "对方账户", "对方帐号"),
    "PAYER_NAME": ("付款方姓名", "付款人姓名", "付款方名称", "付款人名称"),
    "PAYER_ACCOUNT": ("付款方账号", "付款人账号", "付款方帐号"),
    "PAYEE_NAME": ("收款方姓名", "收款人姓名", "收款方名称", "收款人名称"),
    "PAYEE_ACCOUNT": ("收款方账号", "收款人账号", "收款方帐号"),
    "SUMMARY": ("摘要", "交易摘要", "交易备注", "业务摘要", "交易说明", "交易详情"),
    "PURPOSE": ("用途", "附言", "客户附言", "交易附言", "备注", "用途说明"),
    "TX_TYPE": ("交易类型", "现转标志", "交易渠道", "业务类型", "借贷标志"),
    "EXTRA": ("交易流水号", "流水号", "凭证号", "凭证代号", "对方行名", "对方开户行",
              "币种", "序号", "交易地点", "交易城市", "网点名称", "对账日期", "票据号"),
}

#: 别名 → 字段的反查表（精确匹配时用）。
_LOOKUP: dict[str, str] = {}
for _field, _names in ALIASES.items():
    for _name in _names:
        _LOOKUP.setdefault(_name, _field)

#: 归一化时要抹掉的字符：空白、全角空格、制表符，以及中文标点。
#:
#: 「交易日期」与「交易 日期」在 WPS 导出的 HTML 里是同一个东西；建行的
#: 表头还带尾随空格（``'摘要 '``）。所以比较之前统一去掉这些字符。
_STRIP = re.compile(r"[\s　\t]+")
_PUNCT = re.compile(r"[，,。.：:；;、（）()【】\[\]「」『』\-—_/\\|]+")


def norm(text: str) -> str:
    """表头归一化：去空白、去常见标点、转小写。

    归一化**只用于比对**，不用于展示 —— 页面上要显示银行原样的表头。
    """
    if not text:
        return ""
    value = _STRIP.sub("", str(text)).lower()
    return _PUNCT.sub("", value)


def match_field(text: str) -> tuple[str, str] | None:
    """一个表头单元格 → ``(字段, 命中的别名)``；认不出返回 ``None``。

    三级判据，从严到宽：

    1. **完全匹配**：``交易日期`` → ``DATE``。
    2. **归一化后匹配**：``交易 日期``、``交易日期 `` → ``DATE``。
    3. **包含匹配**：表头是 ``对方账号名称``，别名里有 ``对方账号名称``（命中
       CP_NAME）也有 ``对方账号``（命中 CP_ACCOUNT）。**取最长的那个别名**，
       否则 ``对方账号名称`` 会被判成「对方账号」——把户名当成账号，方向
       判定与去重就全错了。
    """
    if not text:
        return None
    raw = str(text).strip()
    field = _LOOKUP.get(raw)
    if field:
        return field, raw
    normalized = norm(raw)
    if not normalized:
        return None
    for name, target in _LOOKUP.items():
        if norm(name) == normalized:
            return target, name
    best: tuple[str, str] | None = None
    best_length = 0
    for name, target in _LOOKUP.items():
        key = norm(name)
        if not key or key not in normalized:
            continue
        if len(key) > best_length:
            best = (target, name)
            best_length = len(key)
    return best


def fields_of(cells: list[str]) -> dict[str, int]:
    """一行表头 → ``{字段: 列号}``。

    一列只能认一个字段；两个字段抢同一列时**留空**（不猜），并在
    ``ambiguous`` 里点名，让上层把它显示成「需要人工指定」。
    """
    mapping: dict[str, int] = {}
    ambiguous: list[str] = []
    for index, cell in enumerate(cells):
        hit = match_field(cell)
        if hit is None:
            continue
        field, alias = hit
        if field in mapping:
            # 同一字段出现在两列（少见，但「摘要」与「备注」都可能命中）——
            # 记下来，交给结构证据或人工指定。
            ambiguous.append(f"第 {index + 1} 列「{cell}」与第 {mapping[field] + 1} 列"
                             f"都像「{field}」")
            continue
        mapping[field] = index
    return mapping


#: 方向词 → 方向。
#:
#: ``IN``/``OUT`` 按「账户余额增加/减少」定义，不是会计借贷。
DIRECTION_WORDS: dict[str, str] = {
    "收入": "IN", "转入": "IN", "转入款": "IN", "存入": "IN", "存现": "IN",
    "汇入": "IN", "汇入汇款": "IN", "来账": "IN", "收款": "IN", "进账": "IN",
    "贷": "IN", "贷方": "IN", "贷方发生额": "IN", "贷方金额": "IN", "cr": "IN", "credit": "IN",
    "支出": "OUT", "转出": "OUT", "转出款": "OUT", "支取": "OUT", "取现": "OUT",
    "汇出": "OUT", "汇出汇款": "OUT", "付款": "OUT", "往账": "OUT", "借": "OUT",
    "借方": "OUT", "借方发生额": "OUT", "借方金额": "OUT", "dr": "OUT", "debit": "OUT",
}

#: 摘要里出现这些词时**提示**方向（只是提示，不单独定案）。
#:
#: 之所以只当提示：同一句摘要在不同银行可能含义相反（「跨行转出」在付款方
#: 视角是支出，在收款方视角是收入）。拿它定案会整列翻向。
DIRECTION_HINTS: dict[str, str] = {
    "结息": "IN", "利息": "IN", "收入": "IN", "转入": "IN", "存入": "IN", "收款": "IN",
    "跨行转出": "OUT", "转出": "OUT", "取现": "OUT", "手续费": "OUT", "支出": "OUT",
    "付款": "OUT", "网银转账": "OUT", "工资": "OUT", "代发": "OUT",
}


def direction_from_word(text: str) -> str | None:
    """一个词（收入/转出/借/贷…）→ ``IN``/``OUT``；不认识返回 ``None``。"""
    if not text:
        return None
    key = _STRIP.sub("", str(text)).lower()
    if key in DIRECTION_WORDS:
        return DIRECTION_WORDS[key]
    for word, direction in DIRECTION_WORDS.items():
        if word and word in key:
            return direction
    return None
