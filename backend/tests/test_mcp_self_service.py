"""个人自用的 MCP 密钥管理（「个人资料」页那一块）的用例。

**这一组钉的是产品决定，不是实现细节。**

自助这一条要成立，靠三件事同时成立：

1. **权限跟着用户走** —— 密钥绑的是本人，本人能读哪些账套，密钥就能读哪些；
   本人改了授权，密钥立刻跟着变（不重启、不换密钥）。
2. **明文只出现一次** —— 库里只有摘要，谁都拿不回来。
3. **处置（删除）是唯一的安全阀** —— 它必须**立刻**生效，而且**本人**就能按。

外加两条「不该发生的」：

* **别人的密钥**看不见、删不掉（回 **404** 而不是 403 —— 403 会回答
  「这一枚存在，而且它不是你的」，那是一个令牌存在性探测器）；
* **没有当前密码就铸不出永久凭据** —— 会话令牌是可以被偷走的，而 MCP 密钥
  永不过期、不随登出与改密失效。

还有一条**刻意不做**的：口令变更**不**连带处置 MCP 密钥（§11.15 写死的
取舍 —— 「把取舍摆在明面上，而不是让它悄悄发生」）。这一组把它钉住，
免得日后有人「顺手补上」：那条处置由**本人显式地**「全部删除」来承担。

### 这一组的处置语义：**删掉记录，不留管理痕迹**

本人这一侧把「处置一枚密钥」做成**物理删除**，而且**自助的写操作一条审计
都不写**（生成 / 删除一枚 / 全部删除）。

这一组把那条口径钉住：

* 删掉之后**那一行真的没了**（不是标记失效）；
* 成功的三个写操作**不产生任何 ``audit_log`` 行**；
* **但读取审计仍在** —— ``mcp.read`` 记的是 ``user_id`` 与 ``token_id``
  （字符串列，不随令牌行消失），所以「谁读过账」照样查得到；
* **失败的签发仍然留痕**（``mcp.token.self_issue_denied``）—— 那记的是
  「有人在拿我的账号试」，不是密钥的生命周期。
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app.core.config import settings
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
    """从 Streamable HTTP 的回包里取出 JSON-RPC 响应体。"""
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"回包里没有 data 行：{text[:400]}")


def call_tool(client, token: str, name: str, arguments: dict | None = None):
    """调一个 MCP 工具，返回 ``(状态码, structuredContent)``。

    工具出错时服务端**不抛异常**，而是回 ``isError: true`` 的结构化回执
    （HTTP 层仍是 200/403/401 那些）。这里两种形态都摊平，让用例只关心
    「拿到了什么」。
    """
    r = mcp_post(client, token, "tools/call",
                 {"name": name, "arguments": arguments or {}})
    if r.status_code != 200:
        return r.status_code, None
    result = rpc_payload(r).get("result", {})
    content = result.get("structuredContent")
    if content is None:
        # 回包只有 text（错误回执的形态）：把正文解出来当结构化内容看。
        text = (result.get("content") or [{}])[0].get("text", "")
        try:
            content = json.loads(text)
        except (TypeError, ValueError):
            content = {"text": text}
    return r.status_code, content


def issue_self(client, headers, name: str = "我的 agent",
               password: str = "staff123") -> dict:
    """走自助接口签发一枚密钥，返回响应体（含明文）。"""
    r = client.post("/api/mcp-tokens",
                    json={"name": name, "current_password": password},
                    headers=headers)
    assert r.status_code == 201, f"签发失败：{r.status_code} {r.text}"
    return r.json()


def tokens_of(user_id: int) -> list[McpToken]:
    with SessionLocal() as db:
        return list(db.scalars(
            select(McpToken).where(McpToken.user_id == user_id)
        ).all())


def grant_book(db, user_id: int, book_id: int, role: str = "VIEWER") -> None:
    db.add(UserBook(user_id=user_id, book_id=book_id, role=role))
    db.commit()


# --------------------------------------------------------------------- 生成

def test_普通用户能在个人资料里生成自己的密钥(client, plain_user, plain_auth, db):
    """**自助这条路是开的**：不需要管理员，任何登录用户都能给自己签一枚。"""
    r = client.post("/api/mcp-tokens",
                    json={"name": "Claude Desktop", "current_password": "staff123"},
                    headers=plain_auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Claude Desktop"
    assert body["token"].startswith("mcp_"), "明文的前缀不对"
    assert body["status"] == "active"

    row = tokens_of(plain_user.id)[0]
    assert row.user_id == plain_user.id, "密钥没绑在这个人身上"
    assert row.name == "Claude Desktop"


def test_生成必须验当前密码(client, plain_user, plain_auth, db):
    """**没有当前密码就铸不出永久凭据。**

    会话令牌是可以被拿走的（XSS、共享电脑），而 MCP 密钥**永不过期**、
    不随登出与改密失效 —— 只凭一枚被偷走的令牌就能铸出一枚永久读凭据，
    那等于把「偷到令牌」升级成「永久读到账套」。
    """
    r = client.post("/api/mcp-tokens",
                    json={"name": "偷来的", "current_password": "不是我的密码"},
                    headers=plain_auth)
    assert r.status_code == 400, r.text
    assert "当前密码不正确" in r.json()["detail"]
    assert tokens_of(plain_user.id) == [], "密码没过却铸出了密钥"


def test_当前密码算错要计入那套锁定(client, plain_user, plain_auth, db):
    """**算错要计入登录那套锁定** —— 不另设一套。

    这条接口验证的也是口令，而它是**持着有效令牌**就能调的接口 ——
    登录那边的账号锁定与 IP 节流**都不覆盖它**。不另设一套限速，就是白送
    一个不限速的密码猜测机。
    """
    for _ in range(2):
        r = client.post("/api/mcp-tokens",
                        json={"name": "试", "current_password": "错的"},
                        headers=plain_auth)
        assert r.status_code in (400, 429), r.text
    db.expire_all()
    u = db.get(SysUser, plain_user.id)
    assert (u.failed_logins or 0) > 0 or (u.lock_level or 0) > 0, \
        "算错了却没计入失败计数 —— 这条接口成了不限速的密码猜测机"


def test_明文只返回一次_库里只有摘要(client, plain_user, plain_auth, db):
    """明文只在签发响应里出现一次；库里永远只有 ``sha256`` 摘要。"""
    body = issue_self(client, plain_auth)
    plain = body["token"]

    row = tokens_of(plain_user.id)[0]
    assert row.token_hash == mcp_auth.token_digest(plain)
    assert plain not in row.token_hash
    assert len(row.token_hash) == 64
    assert not row.token_hash.startswith("mcp_")

    # 列表接口不回明文
    r = client.get("/api/mcp-tokens", headers=plain_auth)
    assert r.status_code == 200
    assert plain not in r.text


def test_不传当前密码是422(client, plain_user, plain_auth, db):
    """漏了当前密码 ⇒ 422（不是「铸出来一把空口令的密钥」）。"""
    r = client.post("/api/mcp-tokens", json={"name": "漏了密码"},
                    headers=plain_auth)
    assert r.status_code == 422, r.text
    assert tokens_of(plain_user.id) == []


# --------------------------------------------------------------------- 可见性

def test_密钥能读的账套跟着本人走(client, plain_user, plain_auth, db, book):
    """**权限跟着用户走**：本人被授权了哪个账套，密钥就能读哪个。

    这是这条功能的核心语义 —— 密钥**不携带自己的账套范围**，可见性 100%
    来自 ``sys_user_book``。
    """
    grant_book(db, plain_user.id, book.id, role="VIEWER")
    body = issue_self(client, plain_auth)

    # 签发响应就回带「这枚密钥能看到哪些账套」
    assert [b["id"] for b in body["books"]] == [book.id]

    # 真的能读到
    status, content = call_tool(client, body["token"], "list_books")
    assert status == 200, content
    assert [b["id"] for b in content["items"]] == [book.id], \
        f"密钥看到的账套不对：{content}"


def test_本人改了授权_密钥立刻跟着变(client, plain_user, plain_auth, db, book):
    """撤了授权 ⇒ 密钥下一次调用就看不到这本账（不重启、不换密钥）。

    两种形态都要验：**清单里不再有它**（``list_books`` 回空列表），
    **直接点名去读它**则被挡下（403）。只验后者的话，清单那条路漏了也不知道。
    """
    grant_book(db, plain_user.id, book.id, role="VIEWER")
    body = issue_self(client, plain_auth)
    token = body["token"]

    status, content = call_tool(client, token, "list_books")
    assert status == 200
    assert [b["id"] for b in content["items"]] == [book.id]
    assert call_tool(client, token, "list_periods",
                     {"book_id": book.id})[0] == 200

    # 撤销授权
    link = db.scalar(select(UserBook).where(
        UserBook.user_id == plain_user.id, UserBook.book_id == book.id))
    db.delete(link)
    db.commit()

    status, content = call_tool(client, token, "list_books")
    assert status == 200
    assert content["items"] == [], f"撤了授权还看得见：{content}"

    # 直接点名去读它 ⇒ forbidden
    #
    # **注意形态**：MCP 的工具级错误**不是 HTTP 状态码** —— 它回 200 +
    # ``isError: true`` 的结构化回执（`error.code` 才是给机器分支用的）。
    # 401（令牌本身失效）才是 HTTP 状态码那一层的。
    status, content = call_tool(client, token, "list_periods", {"book_id": book.id})
    assert status == 200, content
    assert content["error"]["code"] == "forbidden", content
    assert "没有该账套的访问权限" in content["error"]["message"]


# --------------------------------------------------------------------- 列表

def test_列表只看得到自己的密钥(client, plain_user, plain_auth, admin_user,
                               auth, db):
    """**别人的密钥看不见** —— 这一条是自助的边界所在。"""
    issue_self(client, plain_auth, name="员工的")
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "管理员给的", "user_id": admin_user.id},
                    headers=auth)
    assert r.status_code == 201, r.text
    admin_token_id = r.json()["id"]

    mine = client.get("/api/mcp-tokens", headers=plain_auth)
    assert mine.status_code == 200
    assert [t["name"] for t in mine.json()] == ["员工的"]

    # 管理员签发给本人的那枚，**本人也要看得见** ——
    # 否则本人不知道自己有哪些永久凭据在外面，而那正是「处置是唯一安全阀」
    # 最需要的一屏。
    admin_view = client.get("/api/system/mcp-tokens", headers=auth)
    assert admin_view.status_code == 200
    assert any(t["id"] == admin_token_id for t in admin_view.json())


def test_管理员签发给本人的密钥本人也能删掉(client, plain_user, plain_auth,
                                         admin_user, auth, db):
    """**管理员给的密钥，本人也能自己删掉。**"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "管理员给的", "user_id": plain_user.id},
                    headers=auth)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]

    mine = client.get("/api/mcp-tokens", headers=plain_auth)
    assert any(t["id"] == token_id for t in mine.json())

    assert client.delete(f"/api/mcp-tokens/{token_id}",
                         headers=plain_auth).status_code == 200
    db.expire_all()
    assert db.get(McpToken, token_id) is None, "没真的删掉 —— 这一行还在库里"


# --------------------------------------------------------------------- 删除

def test_删除后密钥立刻失效(client, plain_user, plain_auth, db):
    """**处置是唯一的安全阀**，所以它必须**立刻**生效。"""
    body = issue_self(client, plain_auth)
    token = body["token"]
    token_id = body["id"]

    assert mcp_post(client, token, "tools/call",
                    {"name": "list_books", "arguments": {}}).status_code == 200

    r = client.delete(f"/api/mcp-tokens/{token_id}", headers=plain_auth)
    assert r.status_code == 200, r.text

    after = mcp_post(client, token, "tools/call",
                     {"name": "list_books", "arguments": {}})
    assert after.status_code == 401, f"删除后还能用：{after.status_code}"


def test_删除是物理删除_那一行从此不存在(client, plain_user, plain_auth, db):
    """**删掉记录，不是标记失效。**

    这是本人这一侧与**管理面**（「吊销」= 标记失效、记录留着）刻意分开的地方：
    管理面需要「已吊销但还在宽限期内」这个状态存在（轮换靠它），本人这一侧
    不需要 —— 私人凭据怎么处置是本人的事。
    """
    body = issue_self(client, plain_auth)
    token_id = body["id"]

    client.delete(f"/api/mcp-tokens/{token_id}", headers=plain_auth)

    db.expire_all()
    assert db.get(McpToken, token_id) is None, "行还在 —— 那是标记失效，不是删除"
    assert tokens_of(plain_user.id) == []


def test_删除不需要当前密码_安全阀不能焊死(client, plain_user, plain_auth, db):
    """**处置动作不挂再验证闸门。**

    密钥被盗时，人可能正处在「没有当前密码」的状态上（密码也一起被改了）。
    给安全阀加锁 = 把安全阀焊死 —— 那正是最需要它的那一刻它不动。
    """
    body = issue_self(client, plain_auth)
    r = client.delete(f"/api/mcp-tokens/{body['id']}", headers=plain_auth)
    assert r.status_code == 200, r.text
    assert tokens_of(plain_user.id) == []


def test_删别人的密钥回404_不是403(client, plain_user, plain_auth,
                                      admin_user, auth, db):
    """**404，不是 403。**

    403 会回答「这一枚存在，而且它不是你的」—— 那是一个令牌存在性探测器：
    拿别人的 id 一路试过去，能把某个用户名下有几枚密钥、id 是多少全试出来。
    404 不回答任何问题。
    """
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "管理员给的", "user_id": admin_user.id},
                    headers=auth)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]

    resp = client.delete(f"/api/mcp-tokens/{token_id}", headers=plain_auth)
    assert resp.status_code == 404, f"回的是 {resp.status_code}，不是 404"
    db.expire_all()
    assert db.get(McpToken, token_id) is not None, "不该动别人的密钥"


def test_一次全部删除(client, plain_user, plain_auth, db):
    """**本人可以一次性删掉自己名下全部密钥** —— 这是文档里「改密后的标准处置」。"""
    issue_self(client, plain_auth, name="一号")
    issue_self(client, plain_auth, name="二号")

    r = client.delete("/api/mcp-tokens", headers=plain_auth)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2
    assert tokens_of(plain_user.id) == [], "还有行留着 —— 没删干净"


def test_全部删除连管理员吊销过的那几枚也一起删(client, plain_user, plain_auth,
                                                 admin_user, auth, db):
    """**任何状态的行都删** —— 包括管理员吊销过的那几枚。

    那一行对本人已经没有用处（管理员的吊销动作留在管理员那条审计里），
    而本人这一侧的口径就是「不留管理痕迹」：留着它只是让列表里挂着一条
    永远处理不掉的旧记录。
    """
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "管理员给的", "user_id": plain_user.id},
                    headers=auth)
    assert r.status_code == 201, r.text
    admin_token_id = r.json()["id"]
    # 管理员把它吊销了（记录留着，只是标记失效）
    assert client.delete(f"/api/system/mcp-tokens/{admin_token_id}",
                         headers=auth).status_code == 200
    issue_self(client, plain_auth, name="自己的")

    resp = client.delete("/api/mcp-tokens", headers=plain_auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 2
    db.expire_all()
    assert tokens_of(plain_user.id) == []


def test_改密码不连带删除密钥_但本人可以自己全删(client, plain_user,
                                                   plain_auth, db):
    """**口令变更不自动作废 MCP 令牌**（§11.15 写死的取舍）。

    「把取舍摆在明面上，而不是让它悄悄发生」—— 所以这一条钉的是：
    改完密码密钥**还在**（不会悄悄发生），而「全部删除」是**本人显式点**的
    那一下（摆在明面上）。
    """
    body = issue_self(client, plain_auth)
    token = body["token"]

    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123",
                          "new_password": "staff456"},
                    headers=plain_auth)
    assert r.status_code == 200, r.text

    # 密钥**没有**被改密悄悄作废
    still = mcp_post(client, token, "tools/call",
                     {"name": "list_books", "arguments": {}})
    assert still.status_code == 200, "改密把密钥悄悄作废了 —— 那是悄悄发生"

    # 但本人可以自己一次性删掉
    again = client.post("/api/auth/login",
                        json={"username": plain_user.username,
                              "password": "staff456"})
    assert again.status_code == 200, again.text
    headers = {"Authorization": f"Bearer {again.json()['access_token']}"}
    assert client.delete("/api/mcp-tokens", headers=headers).json()["deleted"] == 1


# --------------------------------------------------------------------- 上限

def test_超过上限被拒(client, plain_user, plain_auth, db, monkeypatch):
    """**一个人能铸出多少枚永久凭据必须有界。**

    令牌永不过期 ⇒ 泄漏窗口是无限的。不设上限的话，一枚被偷走的会话令牌
    能铸出无穷多枚永久读凭据，而审计里只留下一堆看不出所以然的记录。
    """
    monkeypatch.setattr(settings, "mcp_max_tokens_per_user", 2)
    issue_self(client, plain_auth, name="一号")
    issue_self(client, plain_auth, name="二号")

    r = client.post("/api/mcp-tokens",
                    json={"name": "三号", "current_password": "staff123"},
                    headers=plain_auth)
    assert r.status_code == 400, r.text
    assert "上限" in r.json()["detail"]
    assert len(tokens_of(plain_user.id)) == 2


def test_上限0表示不限(client, plain_user, plain_auth, db, monkeypatch):
    """**0 = 不限** —— 哨兵值不许被误当成「上限 0 枚」。"""
    monkeypatch.setattr(settings, "mcp_max_tokens_per_user", 0)
    for i in range(3):
        issue_self(client, plain_auth, name=f"第{i}枚")
    assert len(tokens_of(plain_user.id)) == 3


# --------------------------------------------------------------------- 审计

def test_自助的三个写操作一条审计都不写(client, plain_user, plain_auth, db):
    """**生成 / 删除一枚 / 全部删除，一条 ``audit_log`` 都不写。**

    这是用户 2026-10-30 定的口径：**MCP 密钥是本人的私人凭据，怎么处置是
    本人的事**。记一笔「删了一行密钥」既没有对象可指（那一行已经没了），
    也与「不留痕」的口径自相矛盾。

    判据是「**一条 ``mcp.*`` 审计都没有**」，而不是「某几个动作名没有」——
    后者会让日后新增的动作名悄悄绕过这条断言。
    """
    from app.models import AuditLog

    body = issue_self(client, plain_auth, name="不留痕用")
    client.delete(f"/api/mcp-tokens/{body['id']}", headers=plain_auth)
    client.delete("/api/mcp-tokens", headers=plain_auth)

    db.expire_all()
    actions = {a.action for a in db.scalars(
        select(AuditLog).where(AuditLog.user_id == plain_user.id)).all()}
    assert not [a for a in actions if a.startswith("mcp.")], \
        f"自助的写操作留痕了：{actions}"


def test_删掉之后读取审计仍然查得到(client, plain_user, plain_auth, db, book):
    """**读取审计不随令牌行一起消失。**

    ``mcp.read`` 记的是 ``user_id`` 与 ``token_id``（``audit_log.entity_id``
    是**字符串列**，没有到 ``mcp_token`` 的外键），所以那一行被物理删除之后，
    「谁读过账」照样查得到 —— 丢掉的只是「这枚密钥曾经存在、现在被删了」
    这条管理痕迹。
    """
    from app.models import AuditLog

    grant_book(db, plain_user.id, book.id, role="VIEWER")
    body = issue_self(client, plain_auth)
    status, _content = call_tool(client, body["token"], "list_books")
    assert status == 200

    client.delete(f"/api/mcp-tokens/{body['id']}", headers=plain_auth)

    db.expire_all()
    reads = db.scalars(select(AuditLog).where(
        AuditLog.action == "mcp.read",
        AuditLog.user_id == plain_user.id)).all()
    assert reads, "读取审计随令牌行一起没了 —— 那才是真正丢掉的取证线索"
    assert any(str(body["id"]) in (r.entity_id or "") for r in reads), \
        "读取审计里没有 token_id —— 删掉之后就对不上是哪一枚密钥了"


def test_密码没过的签发也要留痕(client, plain_user, plain_auth, db):
    """**失败的签发更要留痕** —— 那才是「有人在试」的证据。

    这一条与「成功不留痕」不矛盾：它记的不是「密钥的生命周期」，而是
    「**有人在拿我的账号试**」。
    """
    from app.models import AuditLog

    client.post("/api/mcp-tokens",
                json={"name": "试一把", "current_password": "错的"},
                headers=plain_auth)

    db.expire_all()
    rows = db.scalars(select(AuditLog).where(
        AuditLog.action == "mcp.token.self_issue_denied")).all()
    assert rows, "密码没过却没留痕 —— 有人拿（偷来的）令牌在试，事后查不到"


# --------------------------------------------------------------------- 门禁

def test_没有令牌就是401(client):
    """没登录 ⇒ 401（不是 403 —— 那会回答「这条路存在但要管理员」）。"""
    assert client.get("/api/mcp-tokens").status_code == 401
    assert client.post("/api/mcp-tokens",
                       json={"name": "x", "current_password": "y"}).status_code == 401
    assert client.delete("/api/mcp-tokens/1").status_code == 401


def test_列表里带得出账套与角色(client, plain_user, plain_auth, db, book):
    """**界面上要能一眼看出「这枚密钥能读什么」**。"""
    grant_book(db, plain_user.id, book.id, role="VIEWER")
    body = issue_self(client, plain_auth)
    assert body["books"], "签发响应没有回带账套清单"
    b = body["books"][0]
    assert b["id"] == book.id
    assert b["role"] == "VIEWER"
    assert b["company_name"]


def test_状态字段分得清宽限期与已吊销(client, plain_user, plain_auth, db):
    """**「宽限期内」与「已吊销」是两件事**，界面上要能分开看。"""
    body = issue_self(client, plain_auth)
    token_id = body["id"]

    # 手工把这一枚置成「宽限期内」（轮换的形态：已吊销但还没到期）
    row = db.get(McpToken, token_id)
    row.revoked_at = mcp_auth.now_utc()
    row.grace_until = mcp_auth.now_utc() + timedelta(days=1)
    db.commit()

    r = client.get("/api/mcp-tokens", headers=plain_auth)
    item = r.json()[0]
    assert item["status"] == "grace", item
    assert item["in_grace"] is True

    # 宽限期过了 ⇒ 已吊销
    row.grace_until = mcp_auth.now_utc() - timedelta(seconds=1)
    db.commit()
    item = client.get("/api/mcp-tokens", headers=plain_auth).json()[0]
    assert item["status"] == "revoked", item
    assert item["in_grace"] is False


def test_只读账号也能签_但它只看得到自己该看得到的(client, plain_user,
                                              plain_auth, db, book):
    """**只读账号铸出来的密钥，不会多看到一分。**

    这是「自助不削弱管理员那条」在权限维度上的具体形态：密钥能读什么
    100% 由 ``sys_user_book`` 决定 —— 只读账号铸出来的密钥，读到的仍然
    只有它本来就能读的那本账。

    （它之所以不属于「账套写接口」，是因为它写的是**本人名下的凭据行**，
    不碰任何一本账的数据 —— 见 ``tests/test_viewer_matrix.py`` 的豁免说明。）
    """
    grant_book(db, plain_user.id, book.id, role="VIEWER")
    body = issue_self(client, plain_auth)

    assert [b["id"] for b in body["books"]] == [book.id]
    assert body["books"][0]["role"] == "VIEWER"

    status, content = call_tool(client, body["token"], "list_books")
    assert status == 200
    assert [b["id"] for b in content["items"]] == [book.id]


def test_只读账号看不到别人的密钥(client, plain_user, plain_auth, admin_user,
                                auth, db):
    """**只读也是「别人」。** 权限再低也不能动别人的密钥。"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "管理员给的", "user_id": admin_user.id},
                    headers=auth)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]

    assert client.get("/api/mcp-tokens", headers=plain_auth).json() == []
    assert client.delete(f"/api/mcp-tokens/{token_id}",
                         headers=plain_auth).status_code == 404
    db.expire_all()
    assert db.get(McpToken, token_id) is not None, "只读账号把别人的密钥删了"
