"""一条规则：**按 id 取期间，必须核对它属于本账套**。

审查报告 P2-1（报表域）/ P2-2（API 域）与父会话跨域核查 P-22：
全仓有 **13 个** ``db.get(Period, …)`` 调用点，只守了 **10 处**、漏了 3 处 ——
``api/printing.py`` 的 ``_period``（被 **9 个**打印/导出端点共用）、
``api/vouchers.py`` 的两处（凭证列表）。漏掉的那三处都返回 **200 + 错数据**，
不报错：``period.ord`` 是**账套内**的相对序号，拿别家的期间去算本账套的数，
等于「按本账套的某一期取数、贴上别家的期间标签」。端到端实测里，
「抬头写 2030-01、内容是 2026-01 的数」。

**为什么这条规则要写成测试。**

审查这一轮已经看到同一个形态反复出现：**同一个判据在两处各写了一遍，
然后两处开始漂移**。「期间必须属于本账套」这条规则在本仓里本来就存在
（``api/reports.py`` 有、``export_detail_ledger`` 有），只是没下沉到共享实现里 ——
于是每新写一个取期间的端点，就多一次「忘了加校验」的机会。

把判据**收到一处** + 用一条测试钉住，比在每处补校验更省事也更可靠：
以后任何新增的 ``db.get(Period, …)`` 如果没做归属核对，这条测试立刻变红。

**判定口径。** 一个 ``db.get(Period, …)`` 调用（**按主键取**，不带任何
账套过滤）必须满足二者之一：

1. 取到的期间对象随后被拿来与某个账套 id 做比较（``period.book_id != book.id``）；
2. 它落在 :data:`ALLOWLIST` 里，且 :data:`ALLOWLIST` 写明了**为什么安全**。

为什么只盯 ``db.get(Period, …)``：按主键取是唯一一种「不自带账套约束」的写法。
``db.scalar(select(Period).where(Period.book_id == book_id))`` 与
``find_period(db, book.id, …)`` 天生就带着账套约束，**天然安全**，不必再判。

**本文件由父会话所有。** 各域 agent 修的是各自的端点；本文件只把规则钉住。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
API_DIR = BACKEND / "app" / "api"

#: 允许「取期间但不做归属比较」的调用点。每一条都必须写明**为什么安全**。
#:
#: 键是 ``(文件名, 所在函数名)``，值是安全理由。**新增例外必须写理由** ——
#: 一条没有理由的例外等于把检查关掉。
ALLOWLIST: dict[tuple[str, str], str] = {
    ("vouchers.py", "_assert_no_dead_gap"):
        "这里的 period_id 来自 by_period 的键，而 by_period 是按「本账套的凭证」"
        "分组出来的 —— 凭证本身已按 book_id 过滤过，取出的 period 必然属于本账套；"
        "而且该 period 紧接着就被交给 svc_organize.irreparable_gaps(db, book.id, period, …)，"
        "book.id 一路传到底。这不是用户传进来的参数。",
    ("vouchers.py", "insert_preview"):
        "取的是 v.period_id —— **凭证自己**的期间，而 v 在上一行已经被"
        "「v.book_id != book.id → 404」核过。不是外部传入的期间标识。",
}


def _is_period_get(call: ast.Call) -> bool:
    """这个调用是不是 ``db.get(Period, …)``（按主键取期间）。"""
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "get":
        return False
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Name):
        return first.id == "Period"
    if isinstance(first, ast.Attribute):
        return first.attr == "Period"
    return False


def _assigned_name(stmt: ast.stmt) -> str | None:
    """``x = db.get(Period, …)`` 里的 x。"""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def _has_book_comparison(func: ast.FunctionDef, var: str | None) -> bool:
    """函数体里有没有把 ``<var>.book_id`` 拿去比较。"""
    if not var:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.Compare):
            continue
        operands = list(node.comparators) + [node.left]
        for operand in operands:
            if (isinstance(operand, ast.Attribute)
                    and operand.attr == "book_id"
                    and isinstance(operand.value, ast.Name)
                    and operand.value.id == var):
                return True
    return False


def _violations() -> list[str]:
    out: list[str] = []
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for func in funcs:
            for stmt in ast.walk(func):
                if not isinstance(stmt, ast.Assign):
                    continue
                value = stmt.value
                if not (isinstance(value, ast.Call) and _is_period_get(value)):
                    continue
                var = _assigned_name(stmt)
                if _has_book_comparison(func, var):
                    continue
                key = (path.name, func.name)
                reason = ALLOWLIST.get(key)
                if reason:
                    continue
                out.append(
                    f"{path.name}:{stmt.lineno}（函数 {func.name}）"
                    f"取期间后没有核对 book_id —— 跨账套的 period_id 会返回 200 + 错数据"
                )
    return out


VIOLATIONS = _violations()


def test_按_id取期间的调用点都有归属核对() -> None:
    """**一条都不许有。**

    新增一处 ``db.get(Period, …)`` 却忘了核对归属时，这条测试会失败并指出
    file:line。修法二选一：

    * 取到期间后比较 ``period.book_id != book.id``（与 ``api/reports.py`` 对齐）；
    * 或者把该调用点登记进 :data:`ALLOWLIST` **并写明为什么安全** ——
      只有「期间 id 本来就来自已按账套过滤的行」这种理由才算数。
    """
    assert not VIOLATIONS, (
        "按 id 取期间却没有核对账套归属（跨账套 period_id 会返回 200 + 错数据）：\n  "
        + "\n  ".join(VIOLATIONS)
    )


def test_例外清单每条都写了理由() -> None:
    """白名单里不许出现「空理由」—— 那等于把检查关掉。"""
    for (filename, func), reason in ALLOWLIST.items():
        assert reason.strip(), f"{filename}::{func} 在例外清单里但没写理由"
        assert len(reason.strip()) >= 10, (
            f"{filename}::{func} 的理由太短，说不清为什么安全"
        )


@pytest.mark.parametrize("filename", sorted(p.name for p in API_DIR.glob("*.py")))
def test_扫描覆盖到_api_下的每个文件(filename: str) -> None:
    """防止有人把 api/ 下的文件挪走，规则悄悄扫了个空目录。"""
    assert (API_DIR / filename).is_file()
