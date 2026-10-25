"""「近期二次验证」闸门与前端的**契约**（审计 §3.2 的另一半）。

**这个文件钉的是什么。**

闸门本身（拒绝 / 放行两头）已经由 ``test_audit_fix_admin_write_mfa_gates.py``
钉死了。这里钉的是另一件同样要紧、却**没人钉**的事：

    **「撞到闸门之后，管理员在界面上有下一步可走。」**

闸门扩到 13 条受管控写路由之后，管理员的令牌一旦超过宽限期（默认 5 分钟），
碰这些动作就会收到一条 403。修复之前前端**没有任何下文** ——
``frontend/src/api/auth.js`` 里的 ``mfaReauth()`` 全仓**没有调用方**，
于是管理员看到的就是一句 403：撞死路，而且没有任何提示告诉他该做什么。

修法是「403 → 弹引导 → 重新验证 → 拿到新令牌 → **自动重试**原动作」。
这条链要成立，**前后端必须认同一个错误码**：

* 后端回 ``{"detail": {"code": "MFA_REAUTH_REQUIRED", "message": "…"}}``；
* 前端按 ``detail.code === 'MFA_REAUTH_REQUIRED'`` 认它。

**跨语言的那一环最容易悄悄断掉**：改了后端的码而忘了前端，前端就退回到
「只认文案」或者干脆不认 —— 界面上又回到撞死路，而且**没有任何报错**
（403 还是那个 403，只是没人接了）。所以本文件**跨语言核对**：
后端用例直接去读前端源码，确认两边是同一个字符串。

**为什么后端要回结构化 detail 而不是一句纯文案**：前端要判断
「这个 403 是不是『该去重新验证』」。拿中文文案去匹配（``detail.includes
("二次验证")``）是**拿协议去猜协议** —— 文案一改就静默失效。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core.deps import MFA_REAUTH_REQUIRED
from app.core import system_settings
from app.core.security import create_access_token
from app.notify import mfa as mfa_service

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend" / "src"


# --------------------------------------------------------------------- 夹具


@pytest.fixture
def mfa_switch_on(db):
    """把二次验证的开关打开（只改库里那一位）。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()


@pytest.fixture
def stale_admin_token(client, admin_user, db):
    """一枚**没有** ``mfa_at`` 的管理员令牌（MFA 开关已开）。

    复现真实的攻击窗口：令牌在开关打开**之前**签发 —— 令牌本身还没过期，
    但它没有 ``mfa_at``，于是被闸门拦下。
    """
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "0")
    db.commit()
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 用例


def test_闸门回的是结构化的403_带机器可读的错误码(client, admin_user,
                                                       stale_admin_token):
    """403 的 detail 必须是 ``{code, message}``，不能是一句纯文案。

    **纯文案的话前端没法判断**「这个 403 是不是该去重新验证」——
    它只能靠匹配中文，而文案一改就静默失效。
    """
    r = client.post("/api/system/users",
                    json={"username": "someone", "password": "Sup3rSecret!"},
                    headers=stale_admin_token)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict), f"detail 不是结构化的：{detail!r}"
    assert detail["code"] == MFA_REAUTH_REQUIRED, detail
    assert "二次验证" in detail["message"], detail
    # 用户看得懂的那句话**必须还在** —— 结构化不是把提示删掉。
    assert detail["message"].strip(), detail


def test_错误码在前后端是同一个字符串(client, admin_user, stale_admin_token):
    """**跨语言核对**：前端必须认后端回的这个码。

    改了一边忘了另一边，界面上就回到「撞死路」，而且**没有任何报错**
    （403 还是那个 403，只是没人接了）—— 所以这里直接去读前端源码。
    """
    # 前端那一侧的权威实现：client.js 里认错误码的那一行。
    client_js = (FRONTEND / "api" / "client.js").read_text(encoding="utf-8")
    assert MFA_REAUTH_REQUIRED in client_js, (
        "前端源码里找不到这个错误码 —— 前端与后端对不上，"
        "界面上撞到闸门就没有「去重新验证」的引导了")

    # 而且前端认的是**同一个**码（不是另一个长得像的）。
    m = re.search(r"MFA_REAUTH_REQUIRED\s*=\s*'([^']+)'", client_js)
    assert m, "前端没有把错误码定义成常量"
    assert m.group(1) == MFA_REAUTH_REQUIRED, (m.group(1), MFA_REAUTH_REQUIRED)

    # 前端**按 code 判**，不是按文案判。
    assert re.search(r"detail\?\.code\s*===\s*MFA_REAUTH_REQUIRED", client_js), (
        "前端没有按 detail.code 判 —— 靠文案匹配的话，文案一改就静默失效")


def test_前端认了这个码之后有下文而不是只弹一句403():
    """前端不能只是「认出这个码」，还得**接得住**。

    钉三件事：
      1. 有个处理函数把「重新验证」这套流程接上（``onMfaReauth``）；
      2. 它被**装上了**（main.js 里真的调用了 ``onMfaReauth(...)``）；
      3. 界面上有个地方能看见它（对话框组件挂进了 MainLayout）。

    少任何一件，管理员看到的都还是一句没有下文的 403。
    """
    main_js = (FRONTEND / "main.js").read_text(encoding="utf-8")
    assert "onMfaReauth" in main_js, "main.js 里没有装重新验证的处理器"
    assert re.search(r"onMfaReauth\(", main_js), "onMfaReauth 只是 import 了，没被调用"

    layout = (FRONTEND / "layouts" / "MainLayout.vue").read_text(encoding="utf-8")
    assert "MfaReauthDialog" in layout, "引导对话框没有挂到界面上"

    dialog = (FRONTEND / "components" / "MfaReauthDialog.vue").read_text(encoding="utf-8")
    # 对话框要能**真的发起**重新验证（调 store 的动作），而不只是画个框。
    assert "startMfaReauth" in dialog, "对话框没有发起重新验证"
    assert "finishMfaReauth" in dialog, "对话框没有把「验证通过」交回去（动作不会被重试）"
    assert "cancelMfaReauth" in dialog, "对话框没有取消路径 —— 管理员会卡在框里出不来"


def test_重新验证换到的令牌带新鲜的验证时刻(client, admin_user, db,
                                              mfa_switch_on):
    """「自动重试」能成立的前提：新令牌必须带 ``mfa_at``。

    没有 ``mfa_at`` 的令牌重试过去，**还是被同一个闸门拦下** ——
    自动重试会变成「再撞一次墙」。
    """
    token, _ = create_access_token(admin_user.id, admin_user.username,
                                   is_admin=True, mfa_at=int(1e9))
    r = client.post("/api/system/users",
                    json={"username": "someone", "password": "Sup3rSecret!"},
                    headers={"Authorization": f"Bearer {token}"})
    # mfa_at 是 2001 年 —— 早于宽限期 → 必须被拦。
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == MFA_REAUTH_REQUIRED


def test_结构化detail不会漏成空_带近期验证的放行那一头(client, admin_user,
                                                          db, mfa_switch_on):
    """**放行的那一半也要钉**：闸门不是「一律拒绝」。

    一枚 ``mfa_at`` 是**现在**的令牌必须能照常做事。
    """
    token, _ = create_access_token(admin_user.id, admin_user.username,
                                   is_admin=True, mfa_at=9999999999)
    r = client.post("/api/system/users",
                    json={"username": "someone", "password": "Sup3rSecret!"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    # 放行的时候**不该**再带那个错误码（那是「撞闸门」的信号）。
    body = json.dumps(r.json(), ensure_ascii=False)
    assert MFA_REAUTH_REQUIRED not in body, body
