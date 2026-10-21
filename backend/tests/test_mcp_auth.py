"""MCP 鉴权链的测试。

**这一组用例钉的是产品决定，不是实现细节。**

MCP 令牌的两条语义是用户 2026-10-21 拍板的：

1. **令牌绑定一个真实用户；该用户能访问哪些账套，用这个令牌的 agent 就只能
   访问哪些账套。** 令牌**不携带自己的账套范围**。
2. **令牌永不过期。**

第二条听起来很危险 —— 它意味着「泄漏窗口是无限的」。所以这一组用例里
有一半是在钉**补偿控制**：吊销立即生效、停用立即作废、用户被删令牌跟着
消失、可见性随用户同步。

其中两条是**防止日后有人「顺手加个到期逻辑」**：

* 「令牌不随时间失效」这条把「永不过期」钉成一条**显式断言**；
* 「签发只返回一次明文、库里只有摘要」这条钉住凭据形态。

改口径之前请先想清楚这两条为什么存在。
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import McpToken, SysUser, UserBook
from app.mcp import auth as mcp_auth


# --------------------------------------------------------------------- 助手

def mcp_post(client, token: str | None, method: str, params: dict | None = None,
             request_id: int = 1):
    """直接打 MCP 端点（Streamable HTTP 的 JSON-RPC 形态）。

    不用 SDK 客户端：这一组要验的是**HTTP 层的行为**（401 / 403 / 协议
    错误），走 SDK 会把「令牌不对」与「协议不对」混在一起。
    """
    body: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/api/mcp", json=body, headers=headers)


def rpc_payload(response) -> dict:
    """从 Streamable HTTP 的回包里取出 JSON-RPC 响应体。

    服务端用 SSE 回包（``text/event-stream``），所以正文是
    ``event: message\\ndata: {...}``。这里把 ``data:`` 那一行解出来。
    """
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"回包里没有 data 行：{text[:400]}")


def issue(client, auth, user_id: int, name: str = "测试 agent") -> str:
    """走管理面签发一个令牌，返回明文。"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": name, "user_id": user_id}, headers=auth)
    assert r.status_code == 201, f"签发失败：{r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture
def mcp_user(db, admin_user):
    """一个被授权了账套的普通用户。"""
    return admin_user


# --------------------------------------------------------------------- ① ②

def test_无令牌一律401(client):
    """没有 Authorization 头 ⇒ 401。

    注意这是**端点级**的拦截：连 ``tools/list`` 都拿不到 ——
    否则任何人都能拉到工具清单与参数 schema，那是一份「这个系统有哪些
    入口」的地图。
    """
    r = mcp_post(client, None, "tools/list")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def test_错令牌401(client):
    # 令牌放在 HTTP 头里，头值必须是 ASCII —— 所以用「格式对但不存在」的
    # 令牌，而不是一段中文。
    r = mcp_post(client, "mcp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                 "tools/list")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def test_登录JWT不能当MCP令牌用(client, auth):
    """登录用的 JWT 传进来 ⇒ 401。

    传进来的是一个**格式合法**的 JWT，但它不是 MCP 令牌。挡掉它靠的是
    前缀检查 —— 明文必须以 ``mcp_`` 开头。
    """
    jwt = auth["Authorization"].split(" ", 1)[1]
    r = mcp_post(client, jwt, "tools/list")
    assert r.status_code == 401


# --------------------------------------------------------------------- ③

def test_吊销后立即401(client, auth, admin_user):
    """吊销 ⇒ **下一次调用**就 401，不等宽限期、不重启、不换令牌。"""
    token = issue(client, auth, admin_user.id)
    assert mcp_post(client, token, "tools/list").status_code == 200

    r = client.delete(f"/api/system/mcp-tokens/{_token_id(admin_user)}", headers=auth)
    assert r.status_code == 200, r.text

    assert mcp_post(client, token, "tools/list").status_code == 401


# --------------------------------------------------------------------- ④

def test_轮换后旧令牌在宽限期内仍可用(client, auth, admin_user, db):
    """轮换 ⇒ 旧令牌进入宽限期，**宽限期内仍然可用**。

    为什么要有宽限期：agent 换 token 时会断线。宽限期是让「换」这件事
    不成为一次故障。
    """
    token = issue(client, auth, admin_user.id)
    row = _token_row(admin_user)

    r = client.post(f"/api/system/mcp-tokens/{row.id}/rotate", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["token"] != token

    # 宽限期未过 ⇒ 旧令牌仍可用
    assert mcp_post(client, token, "tools/list").status_code == 200
    # 新令牌可用
    assert mcp_post(client, r.json()["token"], "tools/list").status_code == 200


def test_宽限期过了旧令牌401(client, auth, admin_user, db):
    """宽限期过了 ⇒ 旧令牌自动失效（不需要定时任务去清理）。"""
    token = issue(client, auth, admin_user.id)
    row = _token_row(admin_user)
    client.post(f"/api/system/mcp-tokens/{row.id}/rotate", headers=auth)

    # 把宽限期推到过去 —— 时间旅行由测试来做，生产没有这个按钮。
    with SessionLocal() as db:
        db.execute(
            select(McpToken).where(McpToken.id == row.id)
        )
        fresh = db.get(McpToken, row.id)
        fresh.grace_until = mcp_auth.now_utc() - timedelta(seconds=1)
        db.commit()

    assert mcp_post(client, token, "tools/list").status_code == 401


# --------------------------------------------------------------------- ⑤

def test_用户停用立即作废其全部令牌(client, auth, admin_user, db):
    """**用户停用立即作废其全部令牌。**

    这是「令牌永不过期」前提下最不能少的一条：令牌自己没有到期时间，
    那么「人走了、账号停了」这件事就必须由用户状态来表达。
    """
    token = issue(client, auth, admin_user.id)
    assert mcp_post(client, token, "tools/list").status_code == 200

    with SessionLocal() as db:
        user = db.get(SysUser, admin_user.id)
        user.active = False
        db.commit()

    assert mcp_post(client, token, "tools/list").status_code == 401


# --------------------------------------------------------------------- ⑥

def test_用户被删令牌随CASCADE消失(client, auth, admin_user, db):
    """用户被删 ⇒ 令牌行随外键 CASCADE 一起消失，不留僵尸令牌。"""
    token = issue(client, auth, admin_user.id)
    user_id = admin_user.id

    with SessionLocal() as db:
        db.delete(db.get(SysUser, user_id))
        db.commit()
        left = db.scalar(
            select(McpToken).where(McpToken.user_id == user_id)
        )
        assert left is None, "用户被删后令牌行应当随 CASCADE 消失"

    assert mcp_post(client, token, "tools/list").status_code == 401


# --------------------------------------------------------------------- ⑦ ⑧

def test_账套可见性随用户授权同步(client, auth, admin_user, db, book):
    """**可见性 100% 来自 UserBook，且不重启、不换令牌就同步。**

    ① 用户**新增**账套授权 ⇒ agent 立刻能看见；
    ② 用户**撤销**账套授权 ⇒ agent 立刻被 403 挡住。
    """
    from app.mcp import auth as auth_mod

    # 建一个自己的用户与令牌，并让它**没有**这个账套的授权。
    staff = SysUser(username=f"mcp_staff_{book.id}", password_hash="x",
                    real_name="外部 agent 用户", active=True, is_admin=False)
    with SessionLocal() as db:
        db.add(staff)
        db.commit()
        db.refresh(staff)
        staff_id = staff.id

    token = issue(client, auth, staff_id, name="外部 agent")

    with SessionLocal() as db:
        principal = mcp_auth.McpPrincipal(user_id=staff_id, username=staff.username,
                                          token_id=_token_row_by_user(staff_id).id,
                                          token_name="外部 agent")
        # 此刻没有授权 ⇒ 403
        with pytest.raises(Exception) as exc:
            auth_mod.require_book_access(db, principal, book.id)
        assert "没有该账套的访问权限" in str(exc.value)

        # 新增授权 ⇒ 立刻可见
        db.add(UserBook(user_id=staff_id, book_id=book.id, role="VIEWER"))
        db.commit()
        link = auth_mod.require_book_access(db, principal, book.id)
        assert link.role == "VIEWER"

        # 撤销授权 ⇒ 立刻 403
        db.delete(link)
        db.commit()
        with pytest.raises(Exception) as exc:
            auth_mod.require_book_access(db, principal, book.id)
        assert "没有该账套的访问权限" in str(exc.value)


# --------------------------------------------------------------------- ⑨

def test_签发只返回一次明文库里只有摘要(client, auth, admin_user, db):
    """明文只在签发响应里出现一次；库里永远只有 ``sha256`` 摘要。"""
    token = issue(client, auth, admin_user.id)

    row = _token_row(admin_user)
    assert row.token_hash == mcp_auth.token_digest(token)
    assert token not in row.token_hash
    # 库里没有明文
    assert len(row.token_hash) == 64  # sha256 十六进制
    assert not row.token_hash.startswith("mcp_")

    # 列表接口不回明文
    r = client.get("/api/system/mcp-tokens", headers=auth)
    assert r.status_code == 200
    assert token not in r.text


# --------------------------------------------------------------------- ⑩

def test_批量吊销某用户全部令牌(client, auth, admin_user):
    """批量吊销 ⇒ 该用户的全部令牌立即 401。

    这是改密、离职、疑似泄漏时的标准处置。
    """
    t1 = issue(client, auth, admin_user.id, name="agent A")
    t2 = issue(client, auth, admin_user.id, name="agent B")
    assert mcp_post(client, t1, "tools/list").status_code == 200
    assert mcp_post(client, t2, "tools/list").status_code == 200

    r = client.delete(f"/api/system/mcp-tokens?user_id={admin_user.id}", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] >= 2

    assert mcp_post(client, t1, "tools/list").status_code == 401
    assert mcp_post(client, t2, "tools/list").status_code == 401


# --------------------------------------------------------------------- ⑪

def test_管理接口需要管理员(client, plain_auth):
    """非管理员访问管理面 ⇒ 403。

    ``mcp_admin`` 的路由挂了 ``dependencies=[Depends(require_admin)]`` ——
    门禁进依赖图之后，FastAPI 在**校验请求体之前**就先求依赖，所以哪怕
    请求体填得不对，非管理员也拿不到 200。
    """
    r = client.get("/api/system/mcp-tokens", headers=plain_auth)
    assert r.status_code == 403
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "x", "user_id": 1}, headers=plain_auth)
    assert r.status_code == 403


# --------------------------------------------------------------------- ⑫

def test_令牌不随时间失效(client, auth, admin_user, db):
    """**令牌永不过期** —— 这条产品决定钉成一条显式断言。

    没有它，日后有人看到「永不过期」觉得不安全、顺手加一个到期列，
    常驻的外部 agent 会在某一天突然全部 401 —— 而那次改动看起来只是
    「加了个安全措施」。

    做法：把令牌的创建时间推到很久以前，**不改任何别的东西**，它仍然可用。
    """
    token = issue(client, auth, admin_user.id)
    row = _token_row(admin_user)

    with SessionLocal() as db:
        fresh = db.get(McpToken, row.id)
        fresh.created_at = mcp_auth.now_utc() - timedelta(days=3650)
        db.commit()

    # 十年前签发的令牌，今天仍然可用
    assert mcp_post(client, token, "tools/list").status_code == 200

    # 而且表里**没有**到期列 —— 留一个恒为空的列只会让人误以为令牌会过期
    assert not hasattr(fresh, "expires_at")


# --------------------------------------------------------------------- 其它

def test_列表回带账套清单与角色(client, auth, admin_user, book):
    """列表与签发响应都回带该用户可访问的账套与角色。

    绑定一个 OWNER 的令牌意味着 agent 能读该用户**所有**账套 ——
    签发人必须当场知情。
    """
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "带账套清单", "user_id": admin_user.id},
                    headers=auth)
    assert r.status_code == 201, r.text
    books = r.json()["books"]
    assert any(b["id"] == book.id for b in books)
    row = next(b for b in books if b["id"] == book.id)
    assert row["role"] == "OWNER"


def test_签发必须给名字(client, auth, admin_user):
    """``name`` 必填。

    一个用户可以有多个令牌（每个外部 agent 一个），名字是管理员唯一能
    分辨「哪个 agent 在用」的线索，也是按 agent 单独吊销的前提。
    """
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "", "user_id": admin_user.id}, headers=auth)
    assert r.status_code in (400, 422)


def test_给停用用户签发被拒(client, auth, admin_user, db):
    """给一个**已停用**的用户签发令牌 ⇒ 400。

    签发人是另一个（仍然启用的）管理员 —— 停用的是**目标用户**。
    """
    target = SysUser(username="mcp_target_off", password_hash="x",
                     real_name="已停用的目标用户", active=False, is_admin=False)
    with SessionLocal() as db:
        db.add(target)
        db.commit()
        db.refresh(target)
        target_id = target.id

    r = client.post("/api/system/mcp-tokens",
                    json={"name": "给停用用户", "user_id": target_id},
                    headers=auth)
    assert r.status_code == 400, r.text


def test_停用用户的令牌立即失效且不需要换令牌(client, auth, admin_user, db):
    """停用 ⇒ 立即 401（不是「等令牌自然过期」）。"""
    token = issue(client, auth, admin_user.id)
    with SessionLocal() as db:
        db.get(SysUser, admin_user.id).active = False
        db.commit()
    assert mcp_post(client, token, "tools/list").status_code == 401


def test_单令牌每小时调用次数有上限(client, auth, admin_user, monkeypatch):
    """限流是「令牌永不过期」这条产品决定的**事前**兜底。

    没有到期时间 ⇒ 泄漏窗口无限 ⇒ 吊销是唯一的事后手段。限流是唯一能
    压住「被拿去批量导出数据」这一形态的**事前**手段。

    计数是**每进程**一份（生产 2 worker ⇒ 实际上限是配置值 × worker 数）。
    这是有意的取舍：为了一个近似精确的计数而给每次调用加一次写放大，
    代价更大；要看「谁在用、查了什么」，靠的是**读取审计**（落库的、
    跨进程完整的）。
    """
    from app.mcp import auth as auth_mod

    auth_mod.reset_rate_limits()
    monkeypatch.setattr(auth_mod.settings, "mcp_max_calls_per_hour", 3)

    token = issue(client, auth, admin_user.id)
    for _ in range(3):
        assert mcp_post(client, token, "tools/list").status_code == 200

    # 第 4 次撞上限
    r = mcp_post(client, token, "tools/list")
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"

    auth_mod.reset_rate_limits()


def test_限流只影响超限的那个令牌(client, auth, admin_user, monkeypatch):
    """限流按**令牌**计数，不是按用户 —— 否则一个忙令牌会连累同用户的
    另一个 agent。"""
    from app.mcp import auth as auth_mod

    auth_mod.reset_rate_limits()
    monkeypatch.setattr(auth_mod.settings, "mcp_max_calls_per_hour", 2)

    busy = issue(client, auth, admin_user.id, name="忙令牌")
    other = issue(client, auth, admin_user.id, name="闲令牌")

    for _ in range(2):
        assert mcp_post(client, busy, "tools/list").status_code == 200
    assert mcp_post(client, busy, "tools/list").status_code == 429
    assert mcp_post(client, other, "tools/list").status_code == 200

    auth_mod.reset_rate_limits()


# --------------------------------------------------------------------- 私有

def _token_row(user) -> McpToken:
    """这个用户的**第一个**令牌行（签发后库里就有）。"""
    with SessionLocal() as db:
        row = db.scalar(
            select(McpToken).where(McpToken.user_id == user.id)
            .order_by(McpToken.id.desc())
        )
        assert row is not None, "库里没有令牌行"
        db.expunge(row)
        return row


def _token_row_by_user(user_id: int) -> McpToken:
    with SessionLocal() as db:
        row = db.scalar(
            select(McpToken).where(McpToken.user_id == user_id)
            .order_by(McpToken.id.desc())
        )
        assert row is not None
        db.expunge(row)
        return row


def _token_id(user) -> int:
    return _token_row(user).id
