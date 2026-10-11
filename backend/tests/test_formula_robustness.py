"""公式解析器的健壮性（审计 P2-01）。

``tokenize`` 是个 ``while i < n`` 循环，所有分支各自推进 ``i``，
但**没有一个 else 兜底**。于是 ``@`` ``#`` ``\\`` ``$`` 这类字符
一个都不匹配，i 纹丝不动 —— 单个非法字符就能让请求永久挂起。

审计实测：``tokenize("1@2")`` 加 2 秒超时，进程无输出被超时杀死。
虽是认证接口内可达的可用性风险，但用户写错一个字符就能让报表卡死，
而前端只会一直转圈。

这里最要紧的是**不能让它挂住测试进程**：所以非法字符用例放进子进程
加超时跑。写不成 ``assert`` 的死循环不是测试，是一颗定时炸弹。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from app.reports.formula import (MAX_FORMULA_LEN, MAX_NEST_DEPTH, FormulaError,
                                  Parser, tokenize)


def _tokenize_in_subprocess(src: str, timeout: float = 5.0):
    """在子进程里跑 tokenize，带超时。

    故意让它有挂死的可能：如果 bug 复现，子进程会被超时杀掉，
    这里拿到的是「超时」而不是「永远等下去」。
    """
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.reports.formula import tokenize, FormulaError\n"
        "try:\n"
        "    tokenize(%r)\n"
        "    print('OK')\n"
        "except FormulaError as e:\n"
        "    print('FormulaError:' + str(e))\n" % (str(__import__("pathlib").Path(__file__).resolve().parents[1]), src)
    )
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"tokenize({src!r}) 超时未返回 —— 死循环复现")
    assert "InsecureKeyLength" not in p.stderr, p.stderr[:500]
    return p.stdout.strip()


@pytest.mark.parametrize("bad", ["1@2", "1#2", "1$2", "1\\2", "1&2", "1;2",
                                 "1`2", "1?2", "1[2", "1]2", "1{2", "1}2"])
def test_非法字符必须快速抛错而不是挂死(bad):
    """审计要求：任意非法输入在毫秒级抛出 FormulaError。"""
    out = _tokenize_in_subprocess(bad)
    assert out.startswith("FormulaError:"), \
        f"tokenize({bad!r}) 既没报错也没超时，返回了 {out!r}"


def test_合法公式仍然正常():
    # 注意 "=" 是 Parser 剥的，tokenize 照样把它当一个 OP 吐出来
    assert len(tokenize("1+2")) == 4                       # 1 + 2 EOF
    assert len(tokenize("=合计(A1:A5)")) == 8              # = 合计 ( A1 : A5 ) EOF
    assert Parser("=合计(A1:A5)").parse() is not None


def test_非法字符报错要指明位置():
    with pytest.raises(FormulaError) as ei:
        tokenize("1@2")
    msg = str(ei.value)
    assert "@" in msg, msg
    assert "2" in msg, f"报错要能定位到是哪一字：{msg}"


def test_超长公式被拦():
    with pytest.raises(FormulaError, match="过长"):
        tokenize("1+" * MAX_FORMULA_LEN)


def test_过深嵌套被拦而不是爆栈():
    """括号套 500 层：没有守卫时是 500 层 RecursionError，接口直接 500。"""
    src = "(" * 200 + "1" + ")" * 200
    with pytest.raises(FormulaError, match="嵌套"):
        Parser(src).parse()


def test_深度上限内正常嵌套仍可用():
    src = "(" * (MAX_NEST_DEPTH - 5) + "1" + ")" * (MAX_NEST_DEPTH - 5)
    assert Parser(src).parse() is not None


def test_循环引用被检出(db, book, periods):
    """A1 引用 B1、B1 引用 A1 —— 没有环检测就是无限递归。

    守护在 ``evaluate.ReportContext.cell`` 的 ``_visiting`` 集合里
    （审计建议补的那一条里，它本来就有）。这里钉住它不被后续改动弄坏：
    报错必须是 FormulaError，不能是 RecursionError ���— 后者会变成 HTTP 500。
    """
    from app.reports.evaluate import EvalContext

    ctx = EvalContext(db, book, periods[1], {(1, 1): "=B1", (1, 2): "=A1"})

    with pytest.raises(FormulaError, match="循环引用"):
        ctx.cell(1, 1)
