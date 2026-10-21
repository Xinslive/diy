"""MCP 的**只读护栏**测试。

这一组盯的是「MCP 永远不能写」这件事。为什么值得单独一组：

* MCP 的令牌**永不过期** —— 一旦它能写，泄漏的后果从「读了不该读的账」升级
  成「改了账」，而改账会牵动结账、结转、审计快照这一整条链；
* 现有 HTTP 写接口全都有 ``require_book_write`` 这道门，MCP **不经过它**
  （MCP 走自己的鉴权链）—— 所以「MCP 没有写能力」不能靠「写接口都挂了
  门禁」来保证，只能靠**工具集里根本没有写工具**。

所以这里有四种形态的断言：

1. **白名单**：工具清单**恰好**是计划里定的那 10 个 —— 多一个少一个都红。
   「少一个」也是错：有人悄悄删掉一个工具，agent 的能力就无声地变了。
2. **词汇**：工具名与描述里不出现写入动词。
3. **行为**：调用前后对业务表做计数快照，**一个数都不许变**。
4. **结构**：MCP 包的源码里不 import 任何写路径。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models import Entry, Period, Subject, Voucher

#: 计划 §3.5 定死的 10 个工具。**白名单**。
EXPECTED_TOOLS = {
    "list_books",
    "list_periods",
    "find_subjects",
    "search_vouchers",
    "latest_payment",
    "check_period_payments",
    "ledger_balance",
    "income_expense_trend",
    "render_report",
    "accounting_context",
}

#: 写入动词。出现在**工具名**里就是写工具；出现在**描述**里也要看一眼
#: （描述是给模型看的，写着「删除」的只读工具会把模型带偏）。
#:
#: 注意别把**正常词**收进来 —— 第一版收了 ``book``，结果 ``list_books``
#: 被判成写工具。判据要挑**只有写路径才会用**的词。
WRITE_VERBS = (
    "create", "insert", "add_", "update", "edit", "modify", "set_",
    "delete", "remove", "drop", "post", "unpost", "close", "unclose",
    "carry", "year_end", "import", "write", "save", "commit", "revoke",
    "issue", "rotate", "adjust", "amend", "void",
)

#: MCP 包里**不允许**出现的写路径符号。
FORBIDDEN_IMPORTS = (
    "create_voucher", "delete_voucher", "update_voucher", "post_voucher",
    "close_period", "unclose_period", "carry_forward", "year_end",
    "require_book_write", "require_write", "set_openings",
    "apply_organize", "plan_organize", "import_vouchers", "commit_voucher",
)


def _mcp_source_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "app" / "mcp"
    return sorted(p for p in root.rglob("*.py"))


def _registered_tool_names() -> set[str]:
    import asyncio

    from app.mcp.server import mcp_server

    return {t.name for t in asyncio.run(mcp_server.list_tools())}


# --------------------------------------------------------------------- ① 白名单

def test_工具清单恰好是计划定的那十个():
    """工具清单**恰好**等于计划 §3.5 的 10 个。

    多一个 = 有人加了能力（哪怕是只读的，也要走计划评审）；
    少一个 = 有人悄悄删了能力（agent 的能力就无声地变了）。
    """
    assert _registered_tool_names() == EXPECTED_TOOLS


def test_MCP端点真的挂上了():
    """``/api/mcp`` 在**路由表**里。

    端点不在表里 ⇒ 请求根本路由不到（404），而外部 agent 看到的只是一句
    「not found」，排查方向会跑偏到「令牌是不是不对」。
    """
    from app.main import app
    from app.mcp.asgi import MCP_PATH

    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert MCP_PATH in paths, f"{MCP_PATH} 不在路由表里：{sorted(p for p in paths if p)}"


def test_启动期自检会在没挂上时拒绝启动():
    """自检本身得**真的会失败** —— 否则它只是一段没人验证过的代码。

    计划 §5.5：「MCP 挂载异常导致 /api/mcp 不可用 ⇒ 启动期就暴露」。
    这里用一个假的 app 对象把两种「没挂上」都跑一遍：路由缺失、端点没就绪。
    """
    from app.main import _assert_mcp_mounted

    class _Fake:
        def __init__(self, paths, endpoint):
            self.router = type("R", (), {"routes": [
                type("R2", (), {"path": p})() for p in paths]})()
            self.state = type("S", (), {"mcp_endpoint": endpoint})()

    # 路由缺失
    with pytest.raises(RuntimeError, match="没有挂上"):
        _assert_mcp_mounted(_Fake([], None))

    # 路由在，但端点里没有就绪的服务（请求会回 503）
    with pytest.raises(RuntimeError, match="没有就绪"):
        _assert_mcp_mounted(_Fake(["/api/mcp"], None))


def test_没有写工具():
    """工具名里不出现写入动词。"""
    for name in _registered_tool_names():
        lowered = name.lower()
        for verb in WRITE_VERBS:
            assert verb not in lowered, f"工具 {name!r} 看起来像写工具（{verb}）"


# --------------------------------------------------------------------- ③ 行为

def test_MCP调用不产生任何业务表写入(client, auth, admin_user, book, db):
    """调用 MCP 工具**前后**，业务表的行数一个都不许变。

    业务表＝``voucher / entry / subject / period``。这是「只读」最直接的
    证明：不看代码，只看数。

    注意 ``mcp_token.last_used_at`` 与 ``audit_log`` **会**变 —— 那是令牌
    生命周期与读取审计，不是业务数据。审计是「谁在读」的留痕，与「读了
    什么数据」无关。
    """
    from tests.test_mcp_auth import issue, mcp_post

    token = issue(client, auth, admin_user.id)

    def counts() -> dict:
        with SessionLocal() as s:
            return {
                "voucher": s.scalar(select(func.count()).select_from(Voucher)
                                    .where(Voucher.book_id == book.id)) or 0,
                "entry": s.scalar(select(func.count()).select_from(Entry)
                                  .where(Entry.book_id == book.id)) or 0,
                "subject": s.scalar(select(func.count()).select_from(Subject)
                                    .where(Subject.book_id == book.id)) or 0,
                "period": s.scalar(select(func.count()).select_from(Period)
                                   .where(Period.book_id == book.id)) or 0,
            }

    before = counts()

    # 把每个工具都调一遍。调失败也无所谓 —— 这一条断言的是「无论工具
    # 成功还是失败，都没有写入」。
    calls = [
        ("tools/list", {}),
        ("tools/call", {"name": "list_books", "arguments": {}}),
        ("tools/call", {"name": "list_periods", "arguments": {"book_id": book.id}}),
        ("tools/call", {"name": "find_subjects",
                        "arguments": {"book_id": book.id, "keyword": "费"}}),
        ("tools/call", {"name": "search_vouchers",
                        "arguments": {"book_id": book.id}}),
        ("tools/call", {"name": "latest_payment",
                        "arguments": {"book_id": book.id, "subject_keyword": "租金"}}),
        ("tools/call", {"name": "check_period_payments",
                        "arguments": {"book_id": book.id, "period_code": "2026-01"}}),
        ("tools/call", {"name": "ledger_balance",
                        "arguments": {"book_id": book.id, "period_code": "2026-01"}}),
        ("tools/call", {"name": "income_expense_trend",
                        "arguments": {"book_id": book.id, "year": 2026}}),
        ("tools/call", {"name": "render_report",
                        "arguments": {"book_id": book.id, "template_code": "INCOME_SHEET",
                                      "period_code": "2026-01"}}),
        ("tools/call", {"name": "accounting_context",
                        "arguments": {"book_id": book.id}}),
    ]
    for i, (method, params) in enumerate(calls):
        mcp_post(client, token, method, params, request_id=i + 1)

    after = counts()
    assert after == before, f"MCP 调用前后业务表行数变了：{before} → {after}"


def test_MCP调用不写凭证(client, auth, admin_user, book, db, make_voucher):
    """更具体的一条：调用**明细类**工具后，凭证与分录的**内容**也不许变。

    上面那条比的是行数；这一条比的是**内容摘要**（张数、金额合计）。
    万一某条路径「删一张再插一张」（行数不变、内容变了），上面那条抓不住。
    """
    from tests.test_mcp_auth import issue, mcp_post

    make_voucher("2026-03-08",
                 dr=[("560207", "12800.00", "3月租金")],
                 cr=[("1002", "12800.00", "付租金")])
    token = issue(client, auth, admin_user.id)

    def snapshot() -> tuple:
        with SessionLocal() as s:
            rows = s.execute(
                select(Voucher.voucher_no, Voucher.status, Voucher.remark,
                       Voucher.period_id, Entry.subject_id, Entry.direction,
                       Entry.amount, Entry.summary)
                .join(Entry, Entry.voucher_id == Voucher.id)
                .where(Voucher.book_id == book.id)
                .order_by(Voucher.id, Entry.line_no)
            ).all()
            return tuple(tuple(r) for r in rows)

    before = snapshot()
    for i, (method, params) in enumerate([
        ("tools/call", {"name": "search_vouchers",
                        "arguments": {"book_id": book.id, "keyword": "租金"}}),
        ("tools/call", {"name": "latest_payment",
                        "arguments": {"book_id": book.id, "subject_keyword": "租金"}}),
        ("tools/call", {"name": "check_period_payments",
                        "arguments": {"book_id": book.id, "period_code": "2026-03",
                                      "subject_keyword": "租金"}}),
        ("tools/call", {"name": "ledger_balance",
                        "arguments": {"book_id": book.id, "period_code": "2026-03"}}),
    ]):
        mcp_post(client, token, method, params, request_id=i + 1)

    assert snapshot() == before, "MCP 调用改动了凭证或分录的内容"


# --------------------------------------------------------------------- ④ 结构

def test_MCP包不引用任何写路径():
    """MCP 包的源码里**不出现**任何写路径的函数名。

    这是结构层面的护栏：行为断言只能覆盖「已经写出来的调用」，这条覆盖
    「将来有人 import 了一个写函数进来」。
    """
    offenders = []
    for path in _mcp_source_files():
        text = path.read_text(encoding="utf-8")
        for symbol in FORBIDDEN_IMPORTS:
            # 注释里提到不算 —— 只看真正的代码引用。
            for match in re.finditer(rf"\b{re.escape(symbol)}\b", text):
                line_no = text[: match.start()].count("\n") + 1
                line = text.splitlines()[line_no - 1]
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"') \
                        or stripped.startswith("'''") or stripped.startswith('"'):
                    continue
                offenders.append(f"{path.name}:{line_no} {symbol}")
    assert not offenders, f"MCP 包引用了写路径：{offenders}"


def test_MCP包不写业务表():
    """MCP 包里不出现对**业务表**的写操作。

    判据分两层：

    * ``app/mcp/tools/``（工具实现层）**一个写入动作都不许有** ——
      工具只读业务数据，没有任何理由往业务表里写；
    * 整个 ``app/mcp/`` 里**不许出现业务模型的构造**（``Voucher(`` /
      ``Entry(`` / ``Subject(`` / ``Period(``）。

    ``app/mcp/auth.py`` 里有一处 ``db.add(row)`` —— 那是**令牌行**
    （``McpToken``），属于令牌生命周期，不是业务数据；读取审计写
    ``audit_log`` 同理。这两处是被允许的写入，面板上的数字（凭证、分录、
    科目、期间）一个都不许动。
    """
    tool_files = [p for p in _mcp_source_files()
                  if p.parent.name == "tools" and p.name != "base.py"]

    offenders = []
    for path in tool_files:
        text = path.read_text(encoding="utf-8")
        for pattern in (r"\bdb\.add\(", r"\bbulk_save", r"\.delete\(",
                        r"\bVoucher\(", r"\bEntry\(", r"\bSubject\(", r"\bPeriod\("):
            for match in re.finditer(pattern, text):
                line_no = text[: match.start()].count("\n") + 1
                line = text.splitlines()[line_no - 1].strip()
                if line.startswith("#"):
                    continue
                offenders.append(f"{path.name}:{line_no} {pattern}")
    assert not offenders, f"MCP 工具层疑似写业务表：{offenders}"

    # 整个 MCP 包里不许构造业务模型（鉴权层写令牌行不算业务表）
    for path in _mcp_source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in (r"\bVoucher\(", r"\bEntry\(", r"\bSubject\(", r"\bPeriod\("):
            for match in re.finditer(pattern, text):
                line_no = text[: match.start()].count("\n") + 1
                line = text.splitlines()[line_no - 1].strip()
                if line.startswith("#"):
                    continue
                offenders.append(f"{path.name}:{line_no} {pattern}")
    assert not offenders, f"MCP 包疑似构造业务模型：{offenders}"


def test_MCP包的写入只落在令牌与审计上():
    """MCP 包里的 ``db.add(`` 只允许出现在**令牌生命周期**上。

    读审计写 ``audit_log``、令牌签发写 ``mcp_token`` —— 这两处是允许的，
    除此之外的写入都要被这条钉住。
    """
    offenders = []
    for path in _mcp_source_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\bdb\.add\(", text):
            line_no = text[: match.start()].count("\n") + 1
            context = "\n".join(text.splitlines()[max(0, line_no - 12):line_no])
            if "McpToken(" not in context:
                offenders.append(f"{path.name}:{line_no}")
    assert not offenders, f"MCP 包里出现了非令牌行的写入：{offenders}"


# --------------------------------------------------------------------- 脱敏

def test_MCP错误信息不含库结构(client, auth, admin_user):
    """工具错误里**不出现**表名、列名、约束名。

    MCP 子应用是独立的一层 ASGI 栈，父应用的 ``DBAPIError`` 处理器**覆盖
    不到它** —— 所以脱敏必须由 MCP 自己保证（``app/mcp/errors.py``）。
    """
    from tests.test_mcp_auth import issue, mcp_post

    token = issue(client, auth, admin_user.id)

    # 故意给一堆会触发「查库失败 / 找不到」路径的入参。
    payloads = [
        ("tools/call", {"name": "list_periods", "arguments": {"book_id": 99999999}}),
        ("tools/call", {"name": "find_subjects",
                        "arguments": {"book_id": admin_user.id, "year": 9999}}),
        ("tools/call", {"name": "ledger_balance",
                        "arguments": {"book_id": admin_user.id,
                                      "period_code": "1900-01"}}),
        ("tools/call", {"name": "render_report",
                        "arguments": {"book_id": admin_user.id,
                                      "template_code": "不存在", "period_code": "1900-01"}}),
    ]

    forbidden = (
        "mcp_token", "sys_user", "sys_user_book", "report_template",
        "period_opening", "uq_", "fk_", "ck_", "trg_",
        "duplicate key", "violates", "not null", "null value",
        "relation ", "column ", "pg_catalog", "psycopg",
    )

    for i, (method, params) in enumerate(payloads):
        r = mcp_post(client, token, method, params, request_id=i + 1)
        text = r.text.lower()
        for word in forbidden:
            assert word not in text, (
                f"错误回包里出现了库结构片段 {word!r}：{r.text[:300]}")


def test_查询超时翻成可读提示而不是PG原文():
    """``statement_timeout`` 掐掉的查询 ⇒ ``query_timeout`` + 中文提示。

    计划 §5.3 的一格：「单次调用超时 ⇒ 库层 statement_timeout，超时转成
    可读错误」。PG 抛的是 ``57014``，原文是「canceling statement due to
    statement timeout」并带着具体时长 —— 那是 PG 的话（还泄了服务端配置），
    agent 该知道的是「太慢了，缩小区间再试」。
    """
    from app.mcp.errors import dbapi_to_mcp_error

    class _FakeOrig:
        sqlstate = "57014"

        class diag:  # noqa: N801 - 模拟 psycopg 的 diag
            message_primary = ("canceling statement due to statement timeout "
                               "with statement timeout of 15000ms")

    class _FakeDBAPI(Exception):
        orig = _FakeOrig()

    error = dbapi_to_mcp_error(_FakeDBAPI(), request_id="rid-1")
    assert error.code == "query_timeout", error.code
    assert "缩小查询区间" in error.message, error.message
    # PG 原文（含服务端配置时长）不许出去
    assert "canceling statement" not in error.message
    assert "15000ms" not in error.message


def test_约束违例按sqlstate分流且不泄库结构():
    """约束类 sqlstate 分成「我们写的中文」与「PG 自带的两套话」。

    判据与 ``main.py`` 相同：**文案里有没有汉字**。``db_init.sql`` 里
    ``RAISE EXCEPTION '已记账的凭证不允许删除'`` 是写给人看的提示；PG 自带的
    ``duplicate key value violates unique constraint "uq_..."`` 没有汉字 ——
    里面带的是**库结构**，不能回给 agent。
    """
    from app.mcp.errors import dbapi_to_mcp_error

    class _FakeOrig:
        def __init__(self, sqlstate, message):
            self.sqlstate = sqlstate
            self.diag = type("D", (), {"message_primary": message})()

    class _FakeDBAPI(Exception):
        def __init__(self, sqlstate, message):
            self.orig = _FakeOrig(sqlstate, message)

    # 我们自己写的中文护栏文案 ⇒ 原样回
    own = dbapi_to_mcp_error(
        _FakeDBAPI("23514", "已记账的凭证不允许删除"), request_id="r")
    assert own.code == "constraint_violation"
    assert "已记账的凭证不允许删除" in own.message

    # PG 自带的文案 ⇒ 换成脱敏替身
    pg = dbapi_to_mcp_error(
        _FakeDBAPI("23505", 'duplicate key value violates unique constraint "uq_x"'),
        request_id="r")
    assert pg.code == "constraint_violation"
    assert "uq_x" not in pg.message
    assert "violates" not in pg.message.lower()
    assert "unique" not in pg.message.lower()


def test_无权限时回的是403而不是500(client, auth, admin_user, book):
    """没有账套授权 ⇒ **结构化 403**，不是 500、不是栈。

    文案与 ``core/deps.py`` 的 ``_link_or_403`` **逐字一致** ——
    「没有该账套的访问权限」。同一件事在两个面上说两种话，是排查噩梦。
    """
    from tests.test_mcp_auth import issue, mcp_post

    # 给一个**没有**账套授权的用户签令牌
    staff = type(admin_user)(username="mcp_noauth", password_hash="x",
                             real_name="无授权用户", active=True, is_admin=False)
    with SessionLocal() as s:
        s.add(staff)
        s.commit()
        s.refresh(staff)
        staff_id = staff.id

    token = issue(client, auth, staff_id, name="无授权 agent")
    r = mcp_post(client, token, "tools/call",
                 {"name": "list_periods", "arguments": {"book_id": book.id}})
    assert "403" in r.text or "forbidden" in r.text
    assert "没有该账套的访问权限" in r.text
