"""**个人自用**的 MCP 密钥管理（「个人资料」页那一块）。

**它与管理面（``api/mcp_admin.py``）是两条路，互不代替。**

* 管理面（``/api/system/mcp-tokens``）：管理员给别人签发、吊销别人的令牌。
* 本文件（``/api/mcp-tokens``）：**本人**管自己名下的密钥。

两条路写的是**同一张表**、**同一套权限语义**（令牌绑用户、账套可见性
100% 来自 ``sys_user_book``），区别只在**谁能动哪些行**。

### 处置方式：**删掉记录**，不留管理痕迹

本人这一侧把「处置一枚密钥」做成**物理删除** —— 那一行从此不存在，
界面上也不再留一条「已吊销」的旧记录。

**这与管理员那一侧不是同一件事，两个动词别混用**：

| | 做什么 | 记录 | 审计 |
|---|---|---|---|
| 管理面「吊销」``/api/system/mcp-tokens/{id}`` | **标记**失效（``revoked_at``） | **留着** | 写 |
| 本人「删除」``/api/mcp-tokens/{id}`` | **物理删除**这一行 | **没了** | **不写** |

管理面**必须**是「标记」而不是「删除」：轮换（``rotate``）要把旧令牌放进
宽限期 —— 那需要「已吊销但还在宽限期内」这个状态存在。

**本人这一侧不留管理痕迹**（用户 2026-10-30 定的口径）：MCP 密钥是本人的
私人凭据，怎么处置是本人的事。于是自助的三个写操作（生成 / 删除一枚 /
全部删除）**一律不写** ``audit_log`` —— 记一笔「删了一行密钥」既没有对象
可指（那一行已经没了），也与「不留痕」的口径自相矛盾。

**但真正要紧的取证痕迹一条都没少**：

* **读取审计在**（``app/mcp/audit.py`` 的 ``mcp.read``）—— 每一次调用都记
  工具名、账套、结果行数。它记的是 ``user_id`` 与 ``token_id``（**字符串
  列**，不随令牌行消失），所以「谁读过账」照样查得到；
* **账号那条线也在** —— 令牌的有效性本来就跟着账号走（停用即作废、
  撤授权即挡住），那是 ``sys_user`` 那一侧的事，与令牌行无关。

丢掉的只有一条：**「这枚密钥曾经存在、现在被删了」这条管理痕迹**。这是
明面上的取舍，不是悄悄发生。

### 一条失败审计仍然保留

**「当前密码没通过」的失败审计还在**（``mcp.token.self_issue_denied``）。
它记的不是「密钥的生命周期」，而是「**有人在拿我的账号试**」—— 拿一枚
（可能是偷来的）会话令牌试着铸永久密钥，密码没过。这条恰恰是本人最该
知道的事，与「私人凭据不留痕」是两件事。

### 为什么需要自助这一条

MCP 令牌**永不过期**（§11.15），于是「处置」是它唯一的安全阀。而令牌
是**按 agent 分枚**的 —— 一个用户接三个外部 agent，就有三枚令牌，其中
任何一枚丢了或该换了，此前只有两个办法：找管理员（管理员不在就卡住），
或者**继续用着那一枚**（越拖越不敢换，最后不了了之）。自助这一条把
「换掉一枚密钥」这件事从「要排队等管理员」变成「自己三秒钟」。

### 权限跟着用户走 —— 这条语义**本来就有**，这里只是把它露出来

令牌**不携带自己的账套范围**：它绑一个真实用户，那个用户能访问哪些账套，
用这枚令牌的 agent 就只能访问哪些账套（可见性 100% 来自
``sys_user_book``，每次调用现查，不做跨请求缓存）。所以：

* 用户改了角色、撤了授权、停用了账号 ⇒ agent 下一次调用立刻同步；
* 用户新增了账套 ⇒ agent 立刻能读它。

**自助签发不改变这条语义**，只是让「本人」也能给自己铸一枚而已 ——
铸出来的那枚能读的东西，**和他自己在网页上能读的一模一样**，
不会更多。这正是「自助不削弱管理员那条」的同一道理（§9.8）。

### 签发为什么要验「当前密码」

MCP 令牌是**永不过期的永久读凭据**。会话令牌是可以被拿走的（XSS、共享
电脑、令牌落在别人手里），而令牌在手就能调这个接口 —— 没有「当前密码」
这道关，拿到令牌的人**顺手就替本人铸了一枚永久密钥**，而那枚密钥
**不随登出、不随改密失效**（§11.15 的口径），于是「改密」这条最常用的
处置对它无效。

这道关把「有令牌」降级成「有令牌**且**知道口令」—— 而知道口令的人本来
就已经是账号的主人了。**与同一页上的「改密码要填当前密码」是同一道关**，
共用同一套失败记账与锁定阶梯（``core.deps.verify_current_password``）。

### 「口令变更不自动作废 MCP 令牌」这条口径**没有改**

§11.15 写死了：改密**不**连带作废 MCP 令牌，把取舍摆在明面上，而不是让
它悄悄发生。所以这一页**多给了一条明面上的处置**：用户自己可以随时
**全部删除**（``DELETE /api/mcp-tokens``）—— 那就是文档里说的「改密后的
标准处置」。

### 管理动作不留痕，但读取审计一条不少

令牌永不过期 ⇒ 处置是唯一安全阀 ⇒ **读取**必须可查：每一次 MCP 调用都写
``mcp.read`` 审计（工具名、账套、结果行数），那才是取证要的东西。

而**管理动作**（生成 / 删除一枚 / 全部删除）**不写审计** —— 见文件头
「处置方式：删掉记录，不留管理痕迹」。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, verify_current_password
from app.models import McpToken, SysUser

from app.mcp import auth as mcp_auth

router = APIRouter(
    prefix="/api/mcp-tokens",
    tags=["个人资料"],
    # **没有管理员门禁** —— 这是本人管本人的那一半。
    # 路由级只挂「必须登录」：具体能动哪些行由每个端点自己判
    # （一律按 ``user_id`` 过滤，请求体里**没有「改谁」这个字段**）。
)


class CreateIn(BaseModel):
    """签发一枚属于自己的 MCP 密钥。"""

    #: 用途标识（如「Claude Desktop · 财务问答」）。**必填** —— 一个用户
    #: 可以有多个令牌（每个外部 agent 一个），名字是本人唯一能分辨
    #: 「哪个 agent 在用」的线索，也是按 agent 单独删除的前提。
    name: str = Field(min_length=1, max_length=64)

    #: **当前密码**。理由见文件头「签发为什么要验当前密码」。
    #: 少了它，一枚被偷走的会话令牌就能铸出一枚永不过期的读凭据。
    current_password: str = Field(min_length=1, max_length=256)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("密钥用途不能为空")
        return v


def _token_view(db: Session, row: McpToken) -> dict:
    """一枚令牌的出参形状（**永远不含明文**，明文不落库，回不出来的）。

    与管理面那份形状的差别只有一个：少了 ``token_hash``（本来也不该有）
    与 ``user_id``（这里永远是本人，不必回）。
    """
    state = mcp_auth.token_state(row)
    return {
        "id": row.id,
        "name": row.name,
        "status": state["status"],
        "enabled": state["enabled"],
        "in_grace": state["in_grace"],
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_used_at": state["last_used_at"].isoformat()
        if state["last_used_at"] else None,
        "revoked_at": state["revoked_at"].isoformat()
        if state["revoked_at"] else None,
        "grace_until": state["grace_until"].isoformat()
        if state["grace_until"] else None,
        # 从来没用过 ⇒ None；用过但很久没用 ⇒ 一个天数。
        "unused_days": state["unused_days"],
        # 这枚密钥能读哪些账套 —— **跟着用户走**，不跟着密钥走。
        "books": mcp_auth.books_of_user(db, row.user_id),
    }


def _my_token(db: Session, token_id: int, user: SysUser) -> McpToken:
    """取「本人的」那一枚令牌。

    **不是本人的 ⇒ 404，不是 403。** 403 会回答「这一枚存在，而且它不是
    你的」—— 那是一个**令牌存在性探测器**：拿别人的 id 一路试过去，
    能把某个用户名下有几枚密钥、id 是多少全试出来。404 不回答任何问题。
    """
    row = db.get(McpToken, token_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "密钥不存在")
    return row


@router.get("")
def list_my_tokens(db: Session = Depends(get_db),
                   user: SysUser = Depends(get_current_user)):
    """列出**本人**名下的全部 MCP 密钥。

    管理员签发给本人的那些**也在列表里** —— 否则本人看不到自己有哪些
    永久凭据在外面，那正是「处置是唯一安全阀」最需要的那一屏。

    **不返回明文。** 明文只在签发那一次响应里出现一次。
    """
    rows = list(db.scalars(
        select(McpToken)
        .where(McpToken.user_id == user.id)
        .order_by(McpToken.created_at.desc(), McpToken.id.desc())
    ).all())
    return [_token_view(db, row) for row in rows]


@router.post("", status_code=201)
def create_my_token(payload: CreateIn, db: Session = Depends(get_db),
                    user: SysUser = Depends(get_current_user)):
    """签发一枚属于自己的 MCP 密钥。

    **明文只在这次响应里返回一次**，之后谁也拿不回来（库里只有摘要）。

    响应同时回带这枚密钥能读的账套清单 —— 让本人当场确认 agent 将看到
    什么（签发的是 OWNER 的账号时，那就是**全部**账套）。

    **不写审计**（见文件头「处置方式：删掉记录，不留管理痕迹」）。
    """
    # ---- 1. 复核「当前密码」 -------------------------------------------
    # 闸门在 core（与登录、自助改密共用同一份规则与锁定阶梯）。
    gate = verify_current_password(db, user, payload.current_password)
    if not gate["ok"]:
        _audit_gate_failure(db, user, payload.name, gate)
        db.commit()
        _raise_gate_failure(gate)

    # ---- 2. 上限 ------------------------------------------------------
    # 令牌永不过期 ⇒ 「能铸出多少枚永久凭据」必须有界。否则一枚被偷走的
    # 会话令牌能铸出无穷多枚永久读凭据，而本人事后连「我到底铸了几枚」都
    # 看不见。宽限期内的那枚**也算还能用**，所以算进上限里。
    limit = max(0, int(settings.mcp_max_tokens_per_user))
    active = mcp_auth.active_token_count(db, user.id)
    if limit and active >= limit:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"你已经有 {active} 枚可用的 MCP 密钥（上限 {limit} 枚）。"
            "请先删掉不再用的那一枚再生成。")

    # ---- 3. 签发 ------------------------------------------------------
    row, plain = mcp_auth.issue_token(
        db, user=user, name=payload.name, created_by=user.username)
    db.commit()
    db.refresh(row)
    view = _token_view(db, row)
    # 明文只在这里出现。
    view["token"] = plain
    return view


@router.delete("/{token_id}")
def delete_my_token(token_id: int, db: Session = Depends(get_db),
                    user: SysUser = Depends(get_current_user)):
    """**删掉**本人名下的一枚密钥：那一行从此不存在。

    **删除 vs 标记失效**：这一侧是**物理删除**（管理面那一侧是「吊销」=
    标记失效、记录留着 —— 它需要那个状态来做轮换的宽限期）。

    **删了之后**：这枚密钥下一次调用立刻 401（校验链是「按摘要查库」，
    查不到就是查不到）。**已经发生过的读取痕迹仍在** —— ``mcp.read``
    审计记的是 ``user_id`` 与 ``token_id`` 字符串，不随这一行消失。

    **不挂再验证闸门**：密钥被盗时要能**立刻**处置，而那时人可能正处在
    「没有当前密码」的状态上。给安全阀加锁是把安全阀焊死。

    **不写审计**（见文件头「处置方式：删掉记录，不留管理痕迹」）。
    """
    row = _my_token(db, token_id, user)
    mcp_auth.delete_token(db, row)
    db.commit()
    return {"ok": True, "id": token_id, "deleted": 1}


@router.delete("")
def delete_all_my_tokens(db: Session = Depends(get_db),
                         user: SysUser = Depends(get_current_user)):
    """**一次性删掉本人名下全部密钥**（任何状态都删）。返回删了几枚。

    这是 §11.15 说的「改密后的标准处置」—— 文档写死了「口令变更**不**
    自动作废 MCP 令牌，把取舍摆在明面上」，那么「明面」就得有一个能点的
    地方：怀疑密钥泄露、换了密码、离职交接，都走这一条。

    **不写审计**（见文件头「处置方式：删掉记录，不留管理痕迹」）。
    """
    count = mcp_auth.delete_user_tokens(db, user.id)
    db.commit()
    return {"deleted": count}


# --------------------------------------------------------------------- 闸门

def _audit_gate_failure(db: Session, user: SysUser, name: str, gate: dict) -> None:
    """「当前密码」没过 —— 留痕。

    **为什么失败留痕、而成功不留**：这一条记的不是「密钥的生命周期」，
    而是「**有人在拿我的账号试**」—— 有人在拿一枚（可能是偷来的）会话
    令牌试着铸一枚永久密钥，密码没过。它是本人在「不留管理痕迹」之外
    唯一需要知道的事。

    成功的签发**不留痕**（那正是「处置方式：删掉记录，不留管理痕迹」
    那条口径要说的）。
    """
    log(db, action="mcp.token.self_issue_denied", entity="mcp_user",
        entity_id=user.id, user=user,
        detail=f"本人签发 MCP 密钥被拒（当前密码未通过）：{name}",
        after={"name": name, "reason": gate.get("reason", "")})


def _raise_gate_failure(gate: dict) -> None:
    """把闸门的结论翻成 HTTP 响应。

    **文案与状态码与「自助改密码」那一条逐字一致** —— 同一个账号、同一个
    错法，在两个界面上给出两套说法会让人以为自己撞上了另一个系统的规则。
    """
    reason = gate.get("reason")
    left = int(gate.get("left_seconds") or 0)
    if reason == "hard_locked":
        raise HTTPException(423, f"账号已锁定，请 {left // 60 + 1} 分钟后再试")
    if reason == "delayed":
        raise HTTPException(429, f"连续验证失败次数过多，请 {left} 秒后重试",
                            headers={"Retry-After": str(left)})
    locked = gate.get("locked") or {}
    if locked.get("hard"):
        raise HTTPException(
            423,
            f"当前密码不正确，且账号已被锁定 {locked['minutes']} 分钟"
            f"（第 {locked['level']} 轮）：请联系管理员解锁或稍后再试")
    if locked:
        raise HTTPException(429, f"当前密码不正确，请 {locked['seconds']} 秒后再试",
                            headers={"Retry-After": str(locked["seconds"])})
    raise HTTPException(400, "当前密码不正确")


__all__ = ["CreateIn", "router"]
