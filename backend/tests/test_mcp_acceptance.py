"""端到端验收：外部 agent 用 MCP 答出计划里的三个示例问题。

计划 §4 给了三个问题，这一组把它们变成**可执行**的断言：

* **Q1** 「最近一笔租金是什么时候付出去的？」
* **Q2** 「有没有付过这个月的电费？」
* **Q3** 「今年收入/费用变化」

另外两条是计划 §6.2 的验收项：

* **可见性随用户同步**：撤销账套授权后，**不重启、不换令牌**，agent 下一次
  调用必须被 403 挡住；
* **多 worker 下会话不错乱**：连续 20 次调用都正常（stateless 模式）。

**为什么这些问题值得单独一组用例。**
它们是「这套东西到底有没有用」的判据 —— 工具能不能列出来不重要，agent
能不能**答对**才重要。而「答对」的判据是**数字与系统界面一致**，所以每条
断言都对着凭证的原始金额写死，而不是对着工具自己的输出写。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import SysUser, UserBook
from tests.mcp_helpers import call_tool, call_tool_expect_error, make_principal


# --------------------------------------------------------------------- 造数

@pytest.fixture
def principal(admin_user):
    """默认主体：管理员本人（``book`` 夹具已把他授权成 OWNER）。"""
    return make_principal(user_id=admin_user.id, username=admin_user.username)


@pytest.fixture
def rent_voucher(make_voucher):
    """一笔**真付出去**的租金：借 房屋租赁费 / 贷 银行存款。"""
    return make_voucher("2026-09-05",
                        dr=[("560207", "12800.00", "9月房屋租金")],
                        cr=[("1002", "12800.00", "付9月租金")],
                        posted=True, remark="2026年9月租金")


@pytest.fixture
def older_rent_voucher(make_voucher):
    """更早的一笔，用来验「最近一笔」而不是「唯一一笔」。"""
    return make_voucher("2026-06-05",
                        dr=[("560207", "12800.00", "6月房屋租金")],
                        cr=[("1002", "12800.00", "付6月租金")],
                        posted=True, remark="2026年6月租金")


@pytest.fixture
def utility_voucher(make_voucher):
    """一笔 10 月的电费：借 水电费 / 贷 银行存款。"""
    return make_voucher("2026-10-08",
                        dr=[("560206", "3260.00", "10月电费")],
                        cr=[("1002", "3260.00", "付10月电费")],
                        posted=True, remark="2026年10月电费")


# --------------------------------------------------------------------- Q1

def test_Q1_最近一笔租金是什么时候付出去的(db, book, admin_user, principal,
                                            older_rent_voucher, rent_voucher):
    """Q1：agent 先找科目，再问最近一次付款 —— 两步都能答。

    **注意「租金」这个词。** 科目名是「房屋租赁费」，**「租金」不是它的
    子串** —— 内置科目表里没有叫「租金」的科目。所以 agent 的真实路径是
    先试「租金」（无命中）、再试「租赁」（命中）。这里把两步都钉住：
    试错的那个要**明确返回没找到**（而不是静默返回空列表让 agent 以为
    「这个月没付过租金」），试对的那个要给出完整答案。

    **注意两个夹具的顺序。** 夹具 ``make_voucher(posted=True)`` 会把该凭证
    **之前**的所有期间标成已结账（业务规矩：上一期结账了本期才能记账）。
    所以「早的那笔」必须先建 —— 否则 9 月那笔一记账，6 月就被封了，
    再想补一张 6 月的凭证就被守卫挡下，报的还是「已结账，不能新增凭证」。
    """
    # 第一步：试「租金」—— 内置科目表里没有这个名字
    miss = call_tool("find_subjects", principal, book_id=book.id,
                     keyword="租金", year=2026)
    assert miss["total"] == 0, "内置科目表里不该有叫「租金」的科目"

    # 第二步：试「租赁」—— 命中「房屋租赁费」
    hit = call_tool("find_subjects", principal, book_id=book.id,
                    keyword="租赁", year=2026)
    assert hit["total"] >= 1
    subject = hit["items"][0]
    assert subject["code"] == "560207"
    assert subject["name"] == "房屋租赁费"
    assert subject["category"] == "PROFIT"

    # 第三步：问最近一次付款
    payment = call_tool("latest_payment", principal, book_id=book.id,
                        subject_id=subject["id"], year=2026)
    assert payment["found"] is True
    assert payment["payment_date"] == "2026-09-05"
    assert payment["amount"] == "12800.00"
    assert payment["status"] == "POSTED"
    assert payment["period_code"] == "2026-09"
    assert payment["cash_flow_kind"] == "PAYMENT"
    assert payment["counterparty"]["code"] == "1002"
    # 更早那笔在 alternatives 里 —— agent 能看到「上一笔是什么时候」
    assert any(a["date"] == "2026-06-05" for a in payment["alternatives"])


def test_Q1_计提了但没付款要能分得开(db, book, admin_user, principal,
                                     make_voucher):
    """「计提了」与「付了」是两回事 —— 混起来会给出错误答案。

    对方是应付账款（2202）⇒ 确认了费用但钱没出去。
    """
    make_voucher("2026-09-20",
                 dr=[("560207", "5000.00", "9月租金计提")],
                 cr=[("2202", "5000.00", "欠付租金")],
                 posted=True, remark="计提未付")

    subjects = call_tool("find_subjects", principal, book_id=book.id,
                         keyword="租赁", year=2026)["items"]
    subject_id = subjects[0]["id"]

    payment = call_tool("latest_payment", principal, book_id=book.id,
                        subject_id=subject_id, year=2026)
    # 计提的那笔**不是**付款：对方不是现金类科目
    assert payment["found"] is False, \
        "计提未付的租金被当成了付出去的钱"
    assert payment["payment_date"] is None
    # 出参要**说清楚**是「仅计提未付」，而不是让 agent 自己猜
    assert "仅计提未付" in payment["note"]
    assert payment["note"].find("2026-09-20") >= 0, \
        "要把最近一条计提记录指出来"


# --------------------------------------------------------------------- Q2

def test_Q2_有没有付过这个月的电费(db, book, admin_user, principal,
                                   utility_voucher):
    """Q2：agent 先问「现在到哪个月了」，再核对该月的电费付没付。"""
    context = call_tool("accounting_context", principal, book_id=book.id)
    assert context["current_accounting_year"] == 2026
    assert context["current_period"]["code"] == "2026-10"

    result = call_tool("check_period_payments", principal, book_id=book.id,
                       period_code="2026-10", subject_keyword="电费")
    assert result["paid"] is True
    assert result["paid_amount"] == "3260.00"
    assert result["paid_dates"] == ["2026-10-08"]
    assert result["accrued_only"] is False
    assert result["unposted_only"] is False
    assert result["period"] == "2026-10"
    assert result["records"], "已付就必须给出那张凭证"


def test_Q2_本月没记录与仅计提未付要分得开(db, book, admin_user, principal,
                                          make_voucher):
    """四种状态必须分得开：**已付 / 仅计提未付 / 有凭证但未记账 / 本月无记录**。

    这一条是整个 MCP 面最容易答错的地方（计划 §8.2 R9）：「计提了」与
    「付了」混起来，agent 会告诉用户「电费付过了」—— 而钱根本没出去。
    """
    subjects = call_tool("find_subjects", principal, book_id=book.id,
                         keyword="电费", year=2026)["items"]
    subject_id = subjects[0]["id"]

    # ① 本月无记录
    empty = call_tool("check_period_payments", principal, book_id=book.id,
                      period_code="2026-03", subject_id=subject_id)
    assert empty["paid"] is False
    assert empty["paid_amount"] == "0.00"
    assert empty["records"] == []

    # ② 仅计提未付：对方是应付账款
    make_voucher("2026-04-10",
                 dr=[("560206", "800.00", "4月电费计提")],
                 cr=[("2202", "800.00", "欠付电费")],
                 posted=True, remark="计提")
    accrued = call_tool("check_period_payments", principal, book_id=book.id,
                        period_code="2026-04", subject_id=subject_id)
    assert accrued["paid"] is False, "计提不等于付款"
    assert accrued["accrued_only"] is True
    assert accrued["paid_amount"] == "0.00"

    # ③ 有凭证但未记账
    make_voucher("2026-05-10",
                 dr=[("560206", "600.00", "5月电费")],
                 cr=[("1002", "600.00", "付5月电费")],
                 posted=False, remark="未记账")
    unposted = call_tool("check_period_payments", principal, book_id=book.id,
                         period_code="2026-05", subject_id=subject_id)
    assert unposted["paid"] is False, "未记账的凭证不算已付"
    assert unposted["unposted_only"] is True


# --------------------------------------------------------------------- Q3

def test_Q3_今年收入费用变化(db, book, admin_user, principal, make_voucher):
    """Q3：agent 问今年的收支变化 —— 数字要与凭证对得上。

    造数：1 月收入 82000；3 月收入 50000 + 费用 12000。
    """
    make_voucher("2026-01-10", dr=[("1002", "82000.00", "1月收入")],
                 cr=[("5001", "82000.00", "营业收入")], posted=True)
    make_voucher("2026-03-10", dr=[("1002", "50000.00", "3月收入")],
                 cr=[("5001", "50000.00", "营业收入")], posted=True)
    make_voucher("2026-03-15", dr=[("560207", "12000.00", "3月租金")],
                 cr=[("1002", "12000.00", "付3月租金")], posted=True)

    trend = call_tool("income_expense_trend", principal, book_id=book.id,
                      year=2026)
    assert trend["caliber"] == "report"
    assert trend["carry_forward_excluded"] is True

    by_period = {row["period"]: row for row in trend["series"]}
    assert by_period["2026-01"]["income"] == "82000.00"
    assert by_period["2026-01"]["expense"] == "0.00"
    assert by_period["2026-03"]["income"] == "50000.00"
    assert by_period["2026-03"]["expense"] == "12000.00"
    assert by_period["2026-03"]["net"] == "38000.00"

    total_income = sum(float(row["income"]) for row in trend["series"])
    total_expense = sum(float(row["expense"]) for row in trend["series"])
    assert total_income == 132000.0
    assert total_expense == 12000.0

    # 口径说明必须在 —— agent 要知道这是「报表口径」（排除了结转凭证）
    assert any("结转" in note for note in trend["notes"]), \
        "趋势出参必须说明这是报表口径（已排除结转凭证）"


# --------------------------------------------------------------------- 可见性同步

def test_撤销账套授权后agent立刻被挡住(db, book, admin_user, principal):
    """可见性随用户同步：**不重启、不换令牌**，下一次调用就被 403 挡住。

    这是「令牌 = 绑定用户权限的只读镜像」这条语义的落点。
    """
    from app.mcp import auth as auth_mod

    staff = SysUser(username="mcp_acceptance_user", password_hash="x",
                    real_name="外部 agent 用户", active=True, is_admin=False)
    db.add(staff)
    db.flush()
    link = UserBook(user_id=staff.id, book_id=book.id, role="VIEWER")
    db.add(link)
    db.commit()

    who = make_principal(user_id=staff.id, username=staff.username)
    # 授权在 ⇒ 读得到
    assert call_tool("list_periods", who, book_id=book.id)["items"]

    # 撤销授权 ⇒ 立刻 403
    db.delete(link)
    db.commit()

    failure = call_tool_expect_error("list_periods", who, book_id=book.id)
    assert failure.code == "forbidden"
    assert failure.message == "没有该账套的访问权限"


def test_新增账套授权后agent立刻看得到(db, admin_user, principal):
    """反过来也一样：新增授权 ⇒ agent 立刻能看见。

    凭据的是 ``UserBook`` 那张表，不做任何跨请求缓存。
    """
    from app.models import Book

    staff = SysUser(username="mcp_acceptance_new", password_hash="x",
                    real_name="新增授权用户", active=True, is_admin=False)
    db.add(staff)
    db.flush()
    book = Book(company_name="临时公司", name="临时账套",
                start_year=2026, start_month=1)
    db.add(book)
    db.flush()
    db.commit()

    who = make_principal(user_id=staff.id, username=staff.username)
    failure = call_tool_expect_error("list_periods", who, book_id=book.id)
    assert failure.code == "forbidden"

    db.add(UserBook(user_id=staff.id, book_id=book.id, role="VIEWER"))
    db.commit()

    assert call_tool("list_periods", who, book_id=book.id)["book_id"] == book.id


# --------------------------------------------------------------------- 多 worker

def test_连续二十次调用都正常(client, auth, admin_user, book):
    """stateless 模式：连续 20 次调用不出现会话错乱。

    生产是 2 worker（``scripts/start_server.py``）—— 有状态的 MCP session
    会把 ``initialize`` 与后续调用分散到不同 worker 上，于是「刚初始化完
    就报 session not found」。stateless 模式下每个请求独立建传输。
    """
    from tests.test_mcp_auth import issue, mcp_post

    token = issue(client, auth, admin_user.id)
    for i in range(20):
        r = mcp_post(client, token, "tools/call",
                     {"name": "list_periods", "arguments": {"book_id": book.id}},
                     request_id=i + 1)
        assert r.status_code == 200, f"第 {i + 1} 次调用失败：{r.status_code} {r.text[:200]}"
        payload = json.loads(
            [line for line in r.text.splitlines() if line.startswith("data:")][0][5:])
        assert "result" in payload, f"第 {i + 1} 次调用没有回结果：{payload}"


# --------------------------------------------------------------------- 工具清单

def test_agent能拿到完整的工具清单(client, auth, admin_user):
    """``tools/list`` 要给得出完整的 10 个工具与它们的参数 schema。

    agent 靠这份清单决定「先调哪个、传什么参数」—— 清单不全就等于有工具
    调不到。
    """
    from tests.test_mcp_auth import issue, mcp_post

    token = issue(client, auth, admin_user.id)
    r = mcp_post(client, token, "tools/list")
    assert r.status_code == 200
    payload = json.loads(
        [line for line in r.text.splitlines() if line.startswith("data:")][0][5:])
    tools = payload["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "list_books", "list_periods", "find_subjects", "search_vouchers",
        "latest_payment", "check_period_payments", "ledger_balance",
        "income_expense_trend", "render_report", "accounting_context",
    }
    for tool in tools:
        assert tool.get("description"), f"工具 {tool['name']} 没有说明"
