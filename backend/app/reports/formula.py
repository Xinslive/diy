"""报表公式引擎。

用户为每个单元格编写类 Excel 公式，系统求值出金额。
刻意不使用 eval()：解析器是手写的递归下降，函数集小而封闭，
既安全也便于扩展（加函数不动语法）。

语法
----
    expr    := term (('+' | '-') term)*
    term    := factor (('*' | '/') factor)*
    factor  := '-' factor | primary
    primary := 数字 | 字符串 | 函数(参数列表) | 区域(A1:B5) | 单元格(A1) | '(' expr ')'

函数
----
    科目(编码, 取数口径[, 期间])   取数口径 ∈ 期初 / 借方累计 / 贷方累计 / 期末
                                          / 本期借方 / 本期贷方
    合计(区域) / SUM(区域)         区域求和
    上期(单元格)                   同一行的上一列
    IF(条件, 真值, 假值)
    四舍五入(值, 位数) / ROUND(值, 位数)
    ABS(x) / MAX(...) / MIN(...)

期间写法
--------
    2026            2026 年第 12 期
    "2026-09"       2026 年第 9 期
    {{本期}}        当前所选期间
    {{上期}}        上一期
    {{年初}}        本年第 1 期
    省略            等同 {{本期}}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Optional

ZERO = Decimal("0")

# 标识符首字符与后续字符分开判断；用带锚点的整串正则会让 B2 只吃下 B
IDENT_START = re.compile(r"[一-鿿A-Za-z_]")
IDENT_PART = re.compile(r"[一-鿿A-Za-z0-9_]")
CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]+)$")
NUM_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")

# 公式长度上限。一个单元格里的公式写几万字符没有任何业务含义，
# 但它会让解析器、递归求值都跟着一起做无用功。
MAX_FORMULA_LEN = 4000
# 括号嵌套深度上限。递归下降解析 + 递归求值都是按栈走的，
# 深度不设限的话 "(" * 5000 这种输入能把 Python 栈打爆。
MAX_NEST_DEPTH = 64


class FormulaError(Exception):
    pass


# --------------------------------------------------------------------- 词法


@dataclass
class Token:
    kind: str
    value: Any


def tokenize(src: str) -> list[Token]:
    if len(src) > MAX_FORMULA_LEN:
        raise FormulaError(
            f"公式过长（{len(src)} 字，上限 {MAX_FORMULA_LEN}）")
    tokens: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "{":
            j = src.find("}}", i)
            if j < 0:
                raise FormulaError("占位符未闭合，应写成 {{本期}} 这种形式")
            tokens.append(Token("PLACEHOLDER", src[i:j + 2]))
            i = j + 2
        elif ch in "<>=!":
            two = src[i:i + 2]
            if two in ("<=", ">=", "<>", "!="):
                tokens.append(Token("OP", "!=" if two == "!=" else two))
                i += 2
            else:
                tokens.append(Token("OP", ch))
                i += 1
        elif ch in "+-*/":
            tokens.append(Token("OP", ch))
            i += 1
        elif ch in "(),:":
            tokens.append(Token("PUNCT", ch))
            i += 1
        elif ch in "\"'":
            quote = ch
            j = i + 1
            buf = []
            while j < n and src[j] != quote:
                buf.append(src[j])
                j += 1
            if j >= n:
                raise FormulaError("字符串未闭合")
            tokens.append(Token("STRING", "".join(buf)))
            i = j + 1
        elif ch.isdigit() or ch == ".":
            j = i
            while j < n and (src[j].isdigit() or src[j] == "."):
                j += 1
            text = src[i:j]
            if not NUM_RE.match(text):
                raise FormulaError(f"非法数字: {text}")
            tokens.append(Token("NUMBER", Decimal(text)))
            i = j
        elif IDENT_START.match(ch):
            j = i
            while j < n and IDENT_PART.match(src[j]):
                j += 1
            word = src[i:j]
            cell = CELL_RE.match(word)
            if cell:
                tokens.append(Token("CELL", (col_to_no(cell.group(1)), int(cell.group(2)))))
            else:
                tokens.append(Token("IDENT", word))
            i = j
        else:
            # **必须有这一步。**
            # 上面每个分支都自己推进 i，可它们一个都没覆盖 ``@`` ``#`` ``\``
            # ``$`` 这类字符 —— 循环条件 ``while i < n`` 于是永远成立，
            # 而 i 纹丝不动：单个非法字符就能让这个请求永久挂起，
            # 前端只会一直转圈。审计用 ``tokenize("1@2")`` 加 2 秒超时复现过：
            # 进程无输出被超时杀死。
            #
            # 这是典型的「漏一个分支就死循环」：往上翻 git 也看不出来，
            # 因为所有已有分支看起来都正常。
            raise FormulaError(f"无法识别的字符 {ch!r}（第 {i + 1} 字）")
    tokens.append(Token("EOF", None))
    return tokens


def col_to_no(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def no_to_col(no: int) -> str:
    s = ""
    while no > 0:
        no, r = divmod(no - 1, 26)
        s = chr(65 + r) + s
    return s


# --------------------------------------------------------------------- 语法


class Parser:
    def __init__(self, src: str):
        # 允许用户像 Excel 那样以 "=" 开头
        src = src.strip()
        if src.startswith("="):
            src = src[1:].strip()
        self.src = src
        self.tokens = tokenize(src)
        self.pos = 0
        # 括号 / 函数的嵌套深度。递归下降解析和递归求值都是拿 Python 调用栈
        # 扛的，深度不设限的话 "((((...))))" 这种输入能把栈打爆 ——
        # 那是 500 层的 RecursionError，接口直接 500，用户只看到「转圈然后出错」。
        self.depth = 0

    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def eat(self, kind: str, value: Any = None) -> Token:
        tok = self.cur
        if tok.kind != kind or (value is not None and tok.value != value):
            raise FormulaError(f"位置 {self.pos} 期望 {value or kind}，实际 {tok.value}")
        self.pos += 1
        return tok

    def parse(self):
        if not self.src.strip():
            return None
        # parse() 可能被多次调用，__init__ 里已经去掉过 "="
        node = self.expr()
        if self.cur.kind != "EOF":
            raise FormulaError(f"公式尾部有多余内容: {self.src[self.pos:]}")
        return node

    def expr(self):
        node = self.compare()
        while self.cur.kind == "OP" and self.cur.value in "+-":
            op = self.cur.value
            self.pos += 1
            node = ("bin", op, node, self.compare())
        return node

    def compare(self):
        node = self.term()
        while self.cur.kind == "OP" and self.cur.value in ("<", ">", "<=", ">=", "=", "<>", "!="):
            op = self.cur.value
            self.pos += 1
            node = ("cmp", op, node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.cur.kind == "OP" and self.cur.value in "*/":
            op = self.cur.value
            self.pos += 1
            node = ("bin", op, node, self.factor())
        return node

    def factor(self):
        if self.cur.kind == "OP" and self.cur.value == "-":
            self.pos += 1
            return ("neg", self.factor())
        if self.cur.kind == "OP" and self.cur.value == "+":
            self.pos += 1
            return self.factor()
        return self.primary()

    def primary(self):
        self.depth += 1
        if self.depth > MAX_NEST_DEPTH:
            self.depth -= 1
            raise FormulaError(
                f"公式嵌套层数超过 {MAX_NEST_DEPTH}，请拆成几行分别计算")
        try:
            return self._primary()
        finally:
            self.depth -= 1

    def _primary(self):
        tok = self.cur
        if tok.kind == "NUMBER":
            self.pos += 1
            return ("num", tok.value)
        if tok.kind == "STRING":
            self.pos += 1
            return ("str", tok.value)
        if tok.kind == "PLACEHOLDER":
            self.pos += 1
            return ("placeholder", tok.value)
        if tok.kind == "CELL":
            self.pos += 1
            if self.cur.kind == "PUNCT" and self.cur.value == ":":
                self.pos += 1
                end = self.eat("CELL").value
                return ("range", tok.value, end)
            return ("cell", tok.value)
        if tok.kind == "IDENT":
            self.pos += 1
            self.eat("PUNCT", "(")
            args = []
            if not (self.cur.kind == "PUNCT" and self.cur.value == ")"):
                args.append(self.expr())
                while self.cur.kind == "PUNCT" and self.cur.value == ",":
                    self.pos += 1
                    args.append(self.expr())
            self.eat("PUNCT", ")")
            return ("call", tok.value, args)
        if tok.kind == "PUNCT" and tok.value == "(":
            self.pos += 1
            node = self.expr()
            self.eat("PUNCT", ")")
            return node
        raise FormulaError(f"无法解析的记号: {tok.value}")


# --------------------------------------------------------------------- 求值


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1) if value else Decimal(0)
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if value is None or value == "":
        return ZERO
    raise FormulaError(f"无法转为金额的值: {value!r}")


class Evaluator:
    """求值上下文由调用方提供，实现与账簿引擎解耦。"""

    def __init__(self, ctx):
        self.ctx = ctx  # 需要: subject_id(code), opening/debit_cum/credit_cum/closing, period_dr/cr,
        #              cell(row, col), current_period, resolve_period(expr), label(row)

    # -- 单元格（带环检测） --
    def cell_value(self, row: int, col: int) -> Decimal:
        return self.ctx.cell(row, col)

    def run(self, src: str) -> Decimal:
        node = Parser(src).parse()
        if node is None:
            return ZERO
        return D(self._eval(node))

    # --------------------------------------------------------------
    def _eval(self, node) -> Any:
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind == "str":
            return node[1]
        if kind == "placeholder":
            return node[1]  # 原样保留 {{本期}} / {{年初}} 交给上下文解析
        if kind == "neg":
            return -D(self._eval(node[1]))
        if kind == "bin":
            return self._binary(node[1], self._eval(node[2]), self._eval(node[3]))
        if kind == "cmp":
            return self._compare(node[1], self._eval(node[2]), self._eval(node[3]))
        if kind == "cell":
            return self.cell_value(node[1][1], node[1][0])
        if kind == "range":
            (c1, r1), (c2, r2) = node[1], node[2]
            total = ZERO
            for r in range(min(r1, r2), max(r1, r2) + 1):
                for c in range(min(c1, c2), max(c1, c2) + 1):
                    total += self.cell_value(r, c)
            return total
        if kind == "call":
            return self._call(node[1], node[2])
        raise FormulaError(f"未知节点 {kind}")

    def _binary(self, op: str, a: Any, b: Any) -> Decimal:
        x, y = D(a), D(b)
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            if y == 0:
                raise FormulaError("除数为 0")
            return x / y
        raise FormulaError(f"未知运算符 {op}")

    # --------------------------------------------------------------
    def _compare(self, op: str, a: Any, b: Any) -> bool:
        x, y = D(a), D(b)
        return {
            "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y,
            "=": x == y, "<>": x != y, "!=": x != y,
        }[op]

    def _call(self, name: str, args: list) -> Any:
        fn: Callable | None = getattr(self, f"_f_{_slug(name)}", None)
        if fn is None:
            raise FormulaError(f"未知函数: {name}")
        return fn(args)

    # -- 科目取数 --
    def _f_subject(self, args: list) -> Decimal:
        if not args:
            raise FormulaError("科目() 缺少参数")
        code = self._eval(args[0])
        if not code:
            return ZERO
        kind = self._eval(args[1]) if len(args) > 1 else "期末"
        period = self.ctx.resolve_period(self._eval(args[2]) if len(args) > 2 else None)
        sid = self.ctx.subject_id(str(code).strip())
        if sid is None:
            raise FormulaError(f"科目不存在: {code}")
        m = {
            "期初": "opening", "借方累计": "debit_cum", "贷方累计": "credit_cum",
            "期末": "closing", "本期借方": "period_dr", "本期贷方": "period_cr",
            "本期金额": "period_amount", "本年累计": "ytd_amount",
            # 按科目自然方向取正数：负债/权益类在资产负债表中显示为正数
            "期末余额": "closing_amt", "期初余额": "opening_amt",
            "本期余额": "period_amt", "本年余额": "ytd_amt",
        }
        if kind not in m:
            raise FormulaError(f"未知取数口径: {kind}（可用：{'、'.join(m)}）")
        return getattr(self.ctx, m[kind])(sid, period)

    def _f_sum(self, args: list) -> Decimal:
        return sum((D(self._eval(a)) for a in args), ZERO)

    def _f_total(self, args: list) -> Decimal:  # 合计
        return self._f_sum(args)

    def _f_prev(self, args: list) -> Decimal:
        node = args[0]
        if node[0] != "cell":
            raise FormulaError("上期() 只能接受单元格引用，例如 上期(D5)")
        col, row = node[1]
        if col <= 1:
            return ZERO
        return self.cell_value(row, col - 1)

    def _f_cell(self, args: list) -> Decimal:  # 单元格
        node = args[0]
        if node[0] != "cell":
            raise FormulaError("单元格() 只能接受单元格引用")
        col, row = node[1]
        return self.cell_value(row, col)

    def _f_if(self, args: list) -> Any:
        if len(args) != 3:
            raise FormulaError("IF(条件, 真值, 假值)")
        return self._eval(args[1]) if _truthy(self._eval(args[0])) else self._eval(args[2])

    def _f_round(self, args: list) -> Decimal:
        value = D(self._eval(args[0]))
        digits = int(D(self._eval(args[1]))) if len(args) > 1 else 2
        quant = Decimal(1).scaleb(-digits)
        return value.quantize(quant, rounding=ROUND_HALF_UP)

    def _f_abs(self, args: list) -> Decimal:
        return abs(D(self._eval(args[0])))

    def _f_max(self, args: list) -> Decimal:
        return max((D(self._eval(a)) for a in args), default=ZERO)

    def _f_min(self, args: list) -> Decimal:
        return min((D(self._eval(a)) for a in args), default=ZERO)

    def _f_abs2(self, args: list) -> Decimal:  # 绝对值
        return self._f_abs(args)


def _slug(name: str) -> str:
    table = {
        "科目": "subject", "合计": "total", "SUM": "sum", "上期": "prev",
        "单元格": "cell", "IF": "if", "四舍五入": "round", "ROUND": "round",
        "ABS": "abs", "MAX": "max", "MIN": "min", "绝对值": "abs2",
    }
    if name in table:
        return table[name]
    return name.lower()


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    try:
        return D(v) != 0
    except FormulaError:
        return bool(v)


# ------------------------------------------------------------- 地址改写


def rewrite_refs(src: str, row_shift: tuple[int, int] | None = None,
                 col_shift: tuple[int, int] | None = None) -> str:
    """按行列位移改写公式里所有单元格地址。

    ``row_shift=(at, delta)`` 表示「行号 >= at 的引用加上 delta」，插入时传
    ``(at, +1)``，删除时传 ``(at + 1, -1)``（删除要跳过被删的那一行本身）。

    为什么必须有这个：公式是**按地址**写的（``合计(B2:B8)``）。在中间插一行，
    下面那些行整体下移，但公式里的 8 还指着原来那行 —— 报表照样能算出一个
    看起来正常的数字，错了也看不出来。这类静默错误比报错危险得多。

    字符串字面量和 ``{{期间}}`` 占位符原样保留：``科目("1001","期末",{{本期}})``
    里的 "1001" 是科目编码，不是地址。
    """
    if not src or (not row_shift and not col_shift):
        return src

    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'":
            j = src.find(ch, i + 1)
            j = n - 1 if j < 0 else j
            out.append(src[i:j + 1])
            i = j + 1
            continue
        if ch == "{":
            j = src.find("}}", i)
            if j < 0:
                out.append(src[i:])
                break
            out.append(src[i:j + 2])
            i = j + 2
            continue
        if IDENT_START.match(ch):
            j = i
            while j < n and IDENT_PART.match(src[j]):
                j += 1
            word = src[i:j]
            m = CELL_RE.match(word)
            if m:
                col, row = col_to_no(m.group(1)), int(m.group(2))
                if row_shift and row >= row_shift[0]:
                    row += row_shift[1]
                if col_shift and col >= col_shift[0]:
                    col += col_shift[1]
                # 被删没了的引用留空，好过留一个指向不存在单元格的非法地址
                word = "" if (col < 1 or row < 1) else f"{no_to_col(col)}{row}"
            out.append(word)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)
