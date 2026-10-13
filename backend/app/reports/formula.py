"""报表公式引擎。

用户为每个单元格编写类 Excel 公式，系统求值出金额。
刻意不使用 eval()：解析器是手写的递归下降，函数集小而封闭，
既安全也便于扩展（加函数不动语法）。

语法
----
    compare := expr (('<'|'>'|'<='|'>='|'='|'<>'|'!=') expr)*   优先级最低
    expr    := term (('+' | '-') term)*
    term    := factor (('*' | '/') factor)*
    factor  := ('-' | '+') factor | primary
    primary := 数字 | 字符串 | 函数(参数列表) | 区域(A1:B5) | 单元格(A1) | '(' expr ')'

    地址支持 ``$`` 锁定：``B5`` / ``$B5`` / ``B$5`` / ``$B$5`` 都合法。
    ``$`` 的含义就是「插删行列时不移动」，改写公式时原样保留。

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
    {{上期}}        上一期（账套首期没有「上期」，写了会报错而不是给一个假数）
    {{年初}}        本年第 1 期
    省略            等同 {{本期}}

值的类型（已知限制，P3-5）
-------------------------
    单元格最终要的是**金额**，所以公式的整体结果不能是文本：
    ``IF(1>0,"是","否")`` 会明确报错，而不是把「是」塞进金额格。
    **比较**则支持文本：``IF(名称="甲",1,0)`` 是能用的。
    想要文本分支，请改用数字标志位，例如 ``IF(1>0,1,0)``。
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Any, Callable, Iterator, Optional

ZERO = Decimal("0")

# 标识符首字符与后续字符分开判断；用带锚点的整串正则会让 B2 只吃下 B
IDENT_START = re.compile(r"[一-鿿A-Za-z_]")
IDENT_PART = re.compile(r"[一-鿿A-Za-z0-9_]")
# 单元格地址。``$`` 是锁定标记，四个分组依次是：列锁定 / 列字母 / 行锁定 / 行号。
#
# 尾部是一个否定前瞻而不是 ``^`` 锚点：这里要能在**字符串中间**切出一个地址
# （``合计(B2:B8)`` 里的 B2），但不能把 ``B2X`` 切成一个地址加一个标识符 ——
# 前者该报错，后者（老行为）整体当函数名处理。前瞻保证后面紧跟的不是一个标识符字符。
# 同理**不能**加 ``^``：``pattern.match(src, i)`` 只在 i==0 时才认 ``^``，
# 加了会让「地址总是整串第一个记号」这个假设在扫描到第二个记号时失效
# （表现是 B2 被当成函数名，然后报「位置 3 期望 (」这种看不懂的错）。
#
# 以前 tokenizer 根本不认 ``$``（走到 else 分支报「无法识别的字符 '$'"），
# 而地址改写器 ``rewrite_refs`` 却带着 ``\$?`` 在匹配 —— 两边对不上，
# 写 ``$B$5`` 的公式存得进去、渲染时必报错。现在两边共用这一条正则，
# 并且锁定标记在改写时**原样保留**：$ 的含义就是不随插删行列移动。
ADDR_RE = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([0-9]+)(?![A-Za-z0-9_一-鿿])")
NUM_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")

# 公式长度上限。一个单元格里的公式写几万字符没有任何业务含义，
# 但它会让解析器、递归求值都跟着一起做无用功。
MAX_FORMULA_LEN = 4000
# 括号嵌套深度上限。递归下降解析 + 递归求值都是按栈走的，
# 深度不设限的话 "(" * 5000 这种输入能把 Python 栈打爆。
MAX_NEST_DEPTH = 64
# 同一条左倾链（连续 + - 或连续 * / 或连续比较）上的运算数上限。
#
# 为什么长度上限不管用（审计 P1-12）：MAX_FORMULA_LEN 拦的是**字符数**，
# 而 "1+"*1990+"1" 只有 3981 字、合法通过；解析时 ``expr`` 的 while 循环
# 是迭代的（不爆栈），但它把 AST 堆成 1991 层左倾 ``bin``，求值时
# ``_eval`` 一层层递归下去 —— 那是 RecursionError，不是 FormulaError，
# 接口直接 500。所以这条上限按**运算数个数**拦，跟字符长度是两回事。
MAX_CHAIN_OPS = 200
# 单个区域一次最多展开多少格。合计(A1:ZZZ99999) 是合法公式、只有 12 个字符，
# 却会让求值循环跑七千万次：不是报错，是把请求线程挂住。
MAX_RANGE_CELLS = 20_000
# 四舍五入的位数范围。位数完全由用户公式控制，而 Decimal 的 context 精度是 28，
# 四舍五入(1, 999999999) 在 scaleb 上直接抛 InvalidOperation（审计 P1-12）。
# Excel 自己把四舍五入位数限制在 15 位以内，这里沿用同一个口径。
MIN_ROUND_DIGITS = -15
MAX_ROUND_DIGITS = 15


class FormulaError(Exception):
    pass


@contextmanager
def formula_guard() -> Iterator[None]:
    """把两类「不该逃到接口层」的异常就地翻译成 FormulaError（审计 P1-12）。

    ``RecursionError`` 和 ``DecimalException``（InvalidOperation / Overflow /
    DivisionByZero）都不是 FormulaError：``evaluate`` 逐格只捕 FormulaError，
    ``main`` 又只注册了 DBAPIError 的 handler，所以它们一路穿出去就是 HTTP 500，
    用户只看到「转圈然后出错」，连是哪一格都定位不了。

    刻意放在**公式引擎内部**而不是让上层宽泛兜底：上层兜底会把「公式太复杂」
    和「数据库断了」混成同一种失败，排查时看不出真实原因；而且 ``cell()`` 递归
    求另一个单元格时栈更深，只有每一层自己兜才不会漏。

    解析期与求值期都已经有前置上限（MAX_NEST_DEPTH / MAX_CHAIN_OPS /
    MAX_RANGE_CELLS / MAX_ROUND_DIGITS），这里是最后一道网 ——
    上限将来被调大时，这里保证异常仍然是那支带文案的 FormulaError。
    """
    try:
        yield
    except RecursionError:
        raise FormulaError(
            f"公式太复杂：运算层数超限（单条加减链 {MAX_CHAIN_OPS} 项、"
            f"嵌套 {MAX_NEST_DEPTH} 层），请拆成几行分别计算") from None
    except DecimalException as e:
        raise FormulaError(
            f"公式算出数值超范围（{e.__class__.__name__}）："
            f"请检查四舍五入的位数（限 {MIN_ROUND_DIGITS}~{MAX_ROUND_DIGITS} 位）"
            f"或参与运算的金额") from None


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
        elif ADDR_RE.match(src, i):
            # 地址单独一个分支：**先于**标识符分支，否则 ``$B$5`` 连词都切不开
            # （IDENT_PART 不含 ``$``，扫描会在第一个 ``$`` 处停住）。
            m = ADDR_RE.match(src, i)
            tokens.append(Token("CELL", (col_to_no(m.group(2)), int(m.group(4)))))
            i = m.end()
        elif IDENT_START.match(ch):
            j = i
            while j < n and IDENT_PART.match(src[j]):
                j += 1
            tokens.append(Token("IDENT", src[i:j]))
            i = j
        else:
            # **必须有这一步。**
            # 上面每个分支都自己推进 i，可它们一个都没覆盖 ``@`` ``#`` ``\``
            # 这类字符 —— 循环条件 ``while i < n`` 于是永远成立，
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
        # 入口取**最松**的那条规则（比较）：比较号两边才能各含加减。
        node = self.compare()
        if self.cur.kind != "EOF":
            raise FormulaError(f"公式尾部有多余内容: {self.src[self.pos:]}")
        return node

    def expr(self):
        """加减。比比较紧、比乘除松。"""
        return self._left_assoc(self.term, "+-", "bin", "加减")

    def compare(self):
        """比较 —— 全链条上**最松**的一条：``compare -> expr -> term``。

        层级不能反过来。写成 ``expr -> compare -> term``（加减在外层）
        时，``1+2>5`` 会解析成 ``1 + (2>5)``：加减先吃掉 ``1``，
        再把比较的结果当成自己的右操作数 —— 求值 1 而不是 0，
        ``IF(1+2>5,...)`` 于是走进错误分支，**且全程不报错**。
        """
        return self._left_assoc(
            self.expr, ("<", ">", "<=", ">=", "=", "<>", "!="), "cmp", "比较")

    def term(self):
        return self._left_assoc(self.factor, "*/", "bin", "乘除")

    def _left_assoc(self, operand, ops, kind: str, what: str):
        """左倾循环：``node = node op operand``。

        这里有两件事必须一起做（审计 P1-12）：

        1. 循环本身是迭代的，解析期不会爆栈；
        2. 但它堆出来的 AST 是 n 层左倾 ``bin``，求值时 ``_eval`` 一层层递归 ——
           ``"1+"*1990+"1"`` 只有 3981 字（远在 MAX_FORMULA_LEN 之内），
           却在求值时炸成 RecursionError → HTTP 500。

        所以这里按**运算数个数**设上限，而不是指望字符长度兜住。
        三条链共用这一个实现，免得三个地方各写一遍上限、以后只改两处。
        """
        node = operand()
        n_ops = 0
        while self.cur.kind == "OP" and self.cur.value in ops:
            n_ops += 1
            if n_ops > MAX_CHAIN_OPS:
                raise FormulaError(
                    f"同一条{what}链上超过 {MAX_CHAIN_OPS} 个运算符，"
                    f"请拆成几行分别计算")
            op = self.cur.value
            self.pos += 1
            node = (kind, op, node, operand())
        return node

    def factor(self):
        if self.cur.kind == "OP" and self.cur.value == "-":
            # 一元负号链（``---1``）也是递归下降：以前它完全绕过了 depth 计数
            # （计数只写在 primary 里），"-"*1500 在**解析期**就爆栈。
            with self._nested("一元正负号"):
                self.pos += 1
                return ("neg", self.factor())
        if self.cur.kind == "OP" and self.cur.value == "+":
            with self._nested("一元正负号"):
                self.pos += 1
                return self.factor()
        return self.primary()

    @contextmanager
    def _nested(self, what: str = "括号 / 函数嵌套"):
        self.depth += 1
        try:
            if self.depth > MAX_NEST_DEPTH:
                raise FormulaError(
                    f"公式{what}超过 {MAX_NEST_DEPTH} 层，请拆成几行分别计算")
            yield
        finally:
            self.depth -= 1

    def primary(self):
        with self._nested():
            return self._primary()

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
            # 实参同样从最松的规则起步：IF(1+2>5,100,200) 里那个
            # 比较号必须能在实参内部被吃掉，否则会一路冒泡成
            # 「公式尾部有多余内容」。
            if not (self.cur.kind == "PUNCT" and self.cur.value == ")"):
                args.append(self.compare())
                while self.cur.kind == "PUNCT" and self.cur.value == ",":
                    self.pos += 1
                    args.append(self.compare())
            self.eat("PUNCT", ")")
            return ("call", tok.value, args)
        if tok.kind == "PUNCT" and tok.value == "(":
            self.pos += 1
            node = self.compare()
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
        # 解析与求值都套在 formula_guard 里：RecursionError / DecimalException
        # 不是 FormulaError，evaluate 逐格只捕 FormulaError，它们一路穿出去
        # 就是 HTTP 500（审计 P1-12）。上层宽泛兜底会把「公式太复杂」和
        # 「数据库断了」混成同一种失败，所以兜在引擎内部。
        with formula_guard():
            node = Parser(src).parse()
            if node is None:
                return ZERO
            value = self._eval(node)
        if isinstance(value, str):
            # 单元格最终要的是金额。以前这里直接 D("是") 报「无法转为金额的值」，
            # 用户看不出是自己想放文本（审计 P3-5）：说清楚该写什么。
            raise FormulaError(
                f"公式结果是文本 {value!r}，而单元格只放金额。"
                f"要按条件切换请返回数字，例如 IF(1>0,1,0)"
                f"（文本**比较**是支持的，如 IF(名称=\"甲\",1,0)）")
        return D(value)

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
            lo_r, hi_r = min(r1, r2), max(r1, r2)
            lo_c, hi_c = min(c1, c2), max(c1, c2)
            # 区域大小完全由用户控制，而 合计(A1:ZZZ99999) 只有 12 个字符。
            # 不设上限的话这不是报错，是让请求线程空跑几千万次 ——
            # 接口挂住和 500 一样是「转圈然后出错」，但连日志都难对齐。
            cells = (hi_r - lo_r + 1) * (hi_c - lo_c + 1)
            if cells > MAX_RANGE_CELLS:
                raise FormulaError(
                    f"区域 {no_to_col(lo_c)}{lo_r}:{no_to_col(hi_c)}{hi_r} 有 {cells} 格，"
                    f"超过单次上限 {MAX_RANGE_CELLS} 格，请缩小范围")
            total = ZERO
            for r in range(lo_r, hi_r + 1):
                for c in range(lo_c, hi_c + 1):
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
        x, y = _cmp_operand(a), _cmp_operand(b)
        if isinstance(x, Decimal) != isinstance(y, Decimal):
            raise FormulaError(f"不能拿金额和文本比大小: {a!r} 与 {b!r}")
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
        raw = D(self._eval(args[1])) if len(args) > 1 else Decimal(2)
        if raw != raw.to_integral_value():
            raise FormulaError(f"四舍五入的位数必须是整数，实际 {raw}")
        digits = int(raw)
        # 位数完全由用户公式控制。Decimal 的 context 精度是 28，
        # 四舍五入(1, 999999999) 在 scaleb 上抛的是 decimal.InvalidOperation ——
        # 不是 FormulaError，穿出 evaluate 就是 HTTP 500（审计 P1-12）。
        # 在这里按业务口径拦下：Excel 自己就把四舍五入位数限制在 15 位以内。
        if not (MIN_ROUND_DIGITS <= digits <= MAX_ROUND_DIGITS):
            raise FormulaError(
                f"四舍五入的位数 {digits} 超出范围（{MIN_ROUND_DIGITS} ~ {MAX_ROUND_DIGITS}）")
        try:
            quant = Decimal(1).scaleb(-digits)
            return value.quantize(quant, rounding=ROUND_HALF_UP)
        except DecimalException as e:
            # 位数合法但金额太大：结果位数超出 context 精度，同样是 DecimalException
            raise FormulaError(
                f"四舍五入({value}, {digits}) 失败：金额超出可表示的精度上限") from e

    def _f_abs(self, args: list) -> Decimal:
        return abs(D(self._eval(args[0])))

    def _f_max(self, args: list) -> Decimal:
        return max((D(self._eval(a)) for a in args), default=ZERO)

    def _f_min(self, args: list) -> Decimal:
        return min((D(self._eval(a)) for a in args), default=ZERO)

    def _f_abs2(self, args: list) -> Decimal:  # 绝对值
        return self._f_abs(args)


def _slug(name: str) -> str:
    """函数名 -> ``_f_`` 方法名。

    **这是本引擎的安全边界，不要改。** ``_call`` 用 ``getattr(self, f"_f_{slug}")``
    取方法，可及面被这个前缀锁死在下面对这十来个 ``_f_*`` 方法里 —— 公式里
    写 ``eval("1")`` 只得到「未知函数: eval」，而不是执行它。
    刻意不用 eval/exec：全后端 grep 零命中是这个设计的可验证前提。
    """
    table = {
        "科目": "subject", "合计": "total", "SUM": "sum", "上期": "prev",
        "单元格": "cell", "IF": "if", "四舍五入": "round", "ROUND": "round",
        "ABS": "abs", "MAX": "max", "MIN": "min", "绝对值": "abs2",
    }
    if name in table:
        return table[name]
    return name.lower()


def _cmp_operand(v: Any):
    """比较的操作数：金额按 Decimal 比，文本按字典序比。

    过去一律走 ``D()``，于是任何涉及文本的比较都报「无法转为金额的值」。
    文本比较本身没有歧义（Excel 也支持），所以按原样支持；金额与文本混比则
    明确报错 —— 那一定是写错了，静默给个 False 只会让人查半天（审计 P3-5）。
    """
    if isinstance(v, str):
        return v
    try:
        return D(v)
    except FormulaError:
        return str(v)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    try:
        return D(v) != 0
    except FormulaError:
        return bool(v)


# ------------------------------------------------------------- 地址改写


def rewrite_refs(src: str, row_shift: tuple[int, int] | None = None,
                 col_shift: tuple[int, int] | None = None, *,
                 drop_row: int | None = None, drop_col: int | None = None) -> str:
    """按行列位移改写公式里所有单元格地址。

    ``row_shift=(at, delta)`` 表示「行号 >= at 的引用加上 delta」，插入时传
    ``(at, +1)``，删除时传 ``(at + 1, -1)``（删除要跳过被删的那一行本身）。

    ``drop_row`` / ``drop_col`` 是**被删掉的那一行/列**：指向它的引用不再位移，
    而是按 0 参与计算。这一对参数就是审计 P1-13 的修复 ——
    以前删行只传了 ``row_shift``，指向被删行的引用于是整体上移一格、
    **静默指向原来上面那一行**（第 4103 行引 B35，删掉第 35 行后 B35 变成
    原第 34 行的数），报表照样渲染出一个看起来正常的错误数字。

    为什么必须有这个改写：公式是**按地址**写的（``合计(B2:B8)``）。在中间插一行，
    下面那些行整体下移，但公式里的 8 还指着原来那行 —— 报表照样能算出一个
    看起来正常的数字，错了也看不出来。这类静默错误比报错危险得多。

    三条附加规则：

    * **区域收缩**：``合计(B2:B5)`` 删掉第 3 行后变成 ``合计(B2:B4)`` ——
      区域内部被删不需要特殊处理（后面的整体上移，区间自然短一格），
      只有**端点**正好是被删的那行/列时需要收缩；整段只剩被删的那一格时
      按 ``0`` 处理。产出的一定还是合法公式：以前是把地址清空，
      ``合计(B2:)`` 是语法错误，那张表从此再也渲染不出来（审计 P2-17）。
    * **``$`` 锁定标记原样保留**：``$B$5`` 的含义就是不随插删行列移动，
      以前改写时把 ``$`` 丢了（写 ``$B$5`` 存得进去、渲染必报错）。
    * 字符串字面量和 ``{{期间}}`` 占位符原样保留：``科目("1001","期末",{{本期}})``
      里的 "1001" 是科目编码，不是地址。
    """
    if not src or (not row_shift and not col_shift
                   and drop_row is None and drop_col is None):
        return src

    def shift(v: int, sh: tuple[int, int] | None) -> int:
        if sh and v >= sh[0]:
            v += sh[1]
        return max(v, 1)   # 防御：位移不该把行/列号压到 0 或负数

    def span(lo: int, hi: int, drop: int | None) -> tuple[int, int] | None:
        """区域两端在 ``drop`` 那一项被删掉之后的新位置；整段删光返回 None。"""
        if drop is None or not (lo <= drop <= hi):
            return lo, hi
        lo2 = lo + 1 if lo == drop else lo
        hi2 = hi - 1 if hi == drop else hi
        return None if lo2 > hi2 else (lo2, hi2)

    def addr(m) -> tuple[int, int, bool, bool]:
        # (列号, 行号, 列锁定, 行锁定)
        return col_to_no(m.group(2)), int(m.group(4)), bool(m.group(1)), bool(m.group(3))

    def render(c: int, r: int, c_abs: bool, r_abs: bool) -> str:
        return f"{'$' if c_abs else ''}{no_to_col(c)}{'$' if r_abs else ''}{r}"

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
        m = ADDR_RE.match(src, i)
        if m:
            c1, r1, c1_abs, r1_abs = addr(m)
            # 看看这是不是一个区域的左端点（B2:B8）—— 是的话两端要一起算
            k = m.end()
            while k < n and src[k].isspace():
                k += 1
            m2 = ADDR_RE.match(src, k + 1) if k < n and src[k] == ":" else None
            if m2:
                c2, r2, c2_abs, r2_abs = addr(m2)
                span_r = span(min(r1, r2), max(r1, r2), drop_row)
                span_c = span(min(c1, c2), max(c1, c2), drop_col)
                if span_r is None or span_c is None:
                    out.append("0")          # 区域整段被删光
                else:
                    # 一维的两端都锁定了，这一维就不位移（Excel 的 $ 语义）
                    rr = span_r if (r1_abs and r2_abs) else tuple(
                        shift(v, row_shift) for v in span_r)
                    cc = span_c if (c1_abs and c2_abs) else tuple(
                        shift(v, col_shift) for v in span_c)
                    out.append(f"{render(cc[0], rr[0], c1_abs, r1_abs)}:"
                               f"{render(cc[1], rr[1], c2_abs, r2_abs)}")
                i = m2.end()
                continue
            if (drop_row is not None and r1 == drop_row) or \
                    (drop_col is not None and c1 == drop_col):
                # 指向被删掉的那一行/列：按 0 参与计算，绝不留一个错位的地址
                out.append("0")
            else:
                out.append(render(
                    shift(c1, col_shift) if not c1_abs else c1,
                    shift(r1, row_shift) if not r1_abs else r1,
                    c1_abs, r1_abs))
            i = m.end()
            continue
        if IDENT_START.match(ch):
            j = i
            while j < n and IDENT_PART.match(src[j]):
                j += 1
            out.append(src[i:j])
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)
