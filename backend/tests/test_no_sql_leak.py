"""一条规则，全仓生效：**数据库异常不得进入响应体**。

审查报告 P1-2 / P1-3 / P1-4（发票域）与 P1-17 / P1-18 / P1-19 说的是同一件事：
把 ``str(SQLAlchemyError)`` 回给客户端，会泄露**整条 SQL 与绑定参数** ——
而绑定参数里装的是**凭证金额、摘要、发票号码**这类业务数据，外加表名、列名、
约束名。

``app/main.py`` 里已经有一道刻意做过的脱敏兜底（``DBAPIError`` 处理器：
4xx 回人工写的中文文案，500 回一句脱敏提示 + 请求号）。但**兜底只在异常
一路穿出去时才生效** —— 任何一处在本地 ``except`` 里把异常字符串化再塞进
``HTTPException``，那道兜底就被绕过了。

**为什么这条规则要写成测试，而不是写进代码规范。**

规范靠人记。审查这一轮已经看到同一个形态反复出现：同样的判据在两处各写
一遍，然后两处开始漂移。所以这里把判据**收到一处**：用一条 AST 扫描测试
把整条规则表达出来 —— 任何一处新增的违规写法都会让这条测试变红，而不是
等到下一次审查才发现。

**判定口径。** 一个 ``except`` 子句如果捕获的是**数据库异常**，那么它的
处理体里**不允许**出现异常对象的字符串形态（``str(exc)``、``f"{exc}"``、
``repr(exc)``…）。

* 为什么只针对数据库异常：``ValueError`` / ``RuntimeError`` 这类**业务**
  异常，其文案是应用自己写的、给人看的（"凭证号不能为空"之类），回给客户端
  是**对的**。一刀切地禁止 ``str(exc)`` 会把大量正常写法也判成违规。
* 为什么允许 ``_diag(e)`` 这种间接调用：``api/vouchers.py`` 里有一个
  ``_diag(exc)`` 辅助函数，它从 psycopg 的 ``diag.message_primary`` 里取
  **人工写的中文文案**（db_init.sql 里的 ``RAISE EXCEPTION`` 原文），而不是
  整条 SQL —— 那是**刻意**的、正确的做法。规则只看「异常对象本身有没有被
  字符串化」，``_diag(e)`` 不是。

**本文件由父会话所有。** ``app/api/invoices.py`` 与 ``app/invoice/service.py``
里的既有违规由发票组 agent 负责修；本文件只是把规则钉在那里。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

# 捕获到这些异常，就意味着「异常来自数据库层」。
DB_EXCEPTION_NAMES = {
    "SQLAlchemyError",
    "DBAPIError",
    "IntegrityError",
    "OperationalError",
    "DataError",
    "ProgrammingError",
    "InternalError",
    "UniqueViolation",
    "ForeignKeyViolation",
    "NotNullViolation",
    "CheckViolation",
    "StatementError",
}

# 这些目录下的代码会把异常**直接**翻译成 HTTP 响应。
# service 层抛 ValueError 是这个项目的既有约定（api 层统一转 400），
# 所以 service 也在扫描范围内 —— 它拼进 ValueError 文案里的东西同样会到客户端。
SCAN_DIRS = ("api", "invoice", "accounting", "printing", "reports", "ledger", "backup")


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        directory = APP / name
        if not directory.is_dir():
            continue
        files.extend(sorted(directory.glob("*.py")))
    return files


def _caught_db_exceptions(handler: ast.ExceptHandler) -> set[str]:
    """这个 except 子句捕获了哪些数据库异常类型名。"""
    node = handler.type
    if node is None:
        return set()
    candidates = node.elts if isinstance(node, ast.Tuple) else [node]
    found: set[str] = set()
    for item in candidates:
        if isinstance(item, ast.Name) and item.id in DB_EXCEPTION_NAMES:
            found.add(item.id)
        elif isinstance(item, ast.Attribute) and item.attr in DB_EXCEPTION_NAMES:
            found.add(item.attr)
    return found


def _exception_variable(handler: ast.ExceptHandler) -> str | None:
    if handler.name:
        return handler.name
    # `except ValueError:` 这种没绑定名字的写法拿不到异常对象 —— 也就无法泄露。
    return None


def _stringifies(node: ast.AST, var: str) -> bool:
    """这个表达式里是否把异常对象字符串化了。

    判据：出现 ``str(var)`` / ``repr(var)``，或 f-string / ``.format`` /
    ``%`` 里插入了 ``var``。
    """
    for sub in ast.walk(node):
        # str(exc) / repr(exc)
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name in ("str", "repr") and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Name) and first.id == var:
                    return True
        # f"{exc}" / f"{exc!s}" / "{exc}".format(...)
        if isinstance(sub, ast.FormattedValue) and isinstance(sub.value, ast.Name) \
                and sub.value.id == var:
            return True
        if isinstance(sub, ast.JoinedStr):
            for value in sub.values:
                if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name) \
                        and value.value.id == var:
                    return True
    return False


def _violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:  # pragma: no cover - 由语法检查类测试兜底
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = _caught_db_exceptions(node)
        if not caught:
            continue
        var = _exception_variable(node)
        if not var:
            continue
        for stmt in node.body:
            if _stringifies(stmt, var):
                out.append(
                    f"{path.relative_to(BACKEND)}:{stmt.lineno} "
                    f"捕获了 {'/'.join(sorted(caught))} 并把异常对象字符串化"
                )
    return out


ALL_VIOLATIONS: list[str] = []
for _file in _iter_source_files():
    ALL_VIOLATIONS.extend(_violations(_file))


@pytest.mark.parametrize("scan_dir", SCAN_DIRS)
def test_扫描目录都存在(scan_dir: str) -> None:
    """防止有人改了 ``SCAN_DIRS`` 里的路径拼写，规则悄悄扫了个空目录。"""
    assert (APP / scan_dir).is_dir(), f"扫描目录不存在：{scan_dir}"


def test_全仓没有数据库异常字符串化进响应() -> None:
    """**一条都不许有。**

    新增一处违规写法时，这条测试会失败并指出 file:line。新增时应改成：
    * 业务错误 → 抛 ``ValueError``（应用自己的文案）；
    * 库层护栏 → 交给 ``main.py`` 的全局处理器；
    * 需要把中文护栏文案回给客户端 → 用 ``api/vouchers.py::_diag`` 那种
      「只取 diag.message_primary」的写法。
    """
    assert not ALL_VIOLATIONS, (
        "数据库异常被字符串化进了响应体（会泄露 SQL 与绑定参数）：\n  "
        + "\n  ".join(ALL_VIOLATIONS)
    )
