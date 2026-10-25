"""「近期管理员二次验证」必须盖住**建号**与**装插件**（审查 §3.2）。

**契约原文**（``app/core/config.py`` 里的 ``admin_mfa_grace_minutes`` 注释）：

    管理员验过二次因子之后，这个窗口内做高危动作（**改他人口令 / 装插件** /
    读 OSS 凭据 / 轮换调度密钥 / 删用户**）不再重复要求走邮箱。

改前，``grep -rn require_recent_admin_mfa app/`` 的命中只有
``app/api/system.py``（7 处）与 ``app/core/deps.py`` —— **``app/plugins/``
全域零出现**；而 ``POST /api/system/users``（建号）同样没挂这道依赖。

**为什么这不是「多一道手续」的问题。** 这道闸门的全部意义是把危险动作与
「令牌刚被偷」解耦：一枚**没有 ``mfa_at``** 的管理员令牌（MFA 开关打开之前
签发的，最长还有 4 小时有效）本来只能做「当次请求里的事」。

* **建号**能**铸出一个比令牌活得久的新凭据** —— 新账号登录不走二次验证，
  于是那次偷来的令牌在 4 小时窗口之后**照样能进系统**。而它授的账套角色
  与 ``PUT /users/{id}/books``（**已被管控**）是**同一份权限**。
* **装插件**更直接：插件代码跑在 **API 宿主进程内**，一份装上去的插件
  能改库、能提权。它是这条链上唯一能把临时凭据变成**永久后门**的动作。

所以下面钉住的是：**没有近期二次验证 ⇒ 写操作一律 403；读操作照旧。**
"""
from __future__ import annotations

import time

import pytest

from app.core import system_settings
from app.core.security import create_access_token
from app.models import SysUser
from app.notify import mfa as mfa_service
from tests.plugin_fixtures import plugin_env  # noqa: F401  （插件夹具不是 conftest）


@pytest.fixture
def mfa_switch(db):
    """把二次验证的开关打开（**只改库里那一位开关**）。

    只改 ``sys_setting``、不动 env —— 「库里优先、env 兜底」是这套约定
    本身要验的东西。
    """
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()


@pytest.fixture
def token_without_mfa(client, admin_user, db):
    """一枚**没有 ``mfa_at``** 的管理员令牌。

    复现的是真实的攻击窗口：令牌在 MFA 开关打开**之前**签发，之后开关
    被打开 —— 令牌本身还没过期（管理员令牌 4 小时），但它**没有** ``mfa_at``。
    """
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, r.text
    assert r.json().get("access_token"), r.text
    token = r.json()["access_token"]
    # 闸门随后打开（模拟运维在某个时刻打开了二次验证）。
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token_with_mfa(admin_user, db):
    """一枚**带** ``mfa_at`` 的管理员令牌（刚做完二次验证）。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()
    token, _ = create_access_token(
        admin_user.id, admin_user.username, is_admin=True,
        token_version=admin_user.token_version or 0,
        mfa_at=int(time.time()))
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 建号


def test_没有近期二次验证时建不了号(client, token_without_mfa):
    """**这是 §3.2 里最要紧的一条**：建号能铸出比令牌活得久的新凭据。"""
    r = client.post("/api/system/users",
                    json={"username": "backdoor01", "password": "backdoor123"},
                    headers=token_without_mfa)
    assert r.status_code == 403, r.text
    assert "二次验证" in r.text, r.text


def test_建号被拒时一个用户都不许落地(client, db, token_without_mfa):
    """403 必须真的**没建号** —— 只回一句提示却把号建出来是最坏的形态。"""
    client.post("/api/system/users",
                json={"username": "backdoor01", "password": "backdoor123"},
                headers=token_without_mfa)
    db.expire_all()
    assert db.query(SysUser).filter(SysUser.username == "backdoor01").count() == 0


def test_带近期二次验证的令牌可以建号(client, db, token_with_mfa):
    """闸门不是「一律拒绝」：验过二次验证就放行。"""
    r = client.post("/api/system/users",
                    json={"username": "normal_user01", "password": "normal12345"},
                    headers=token_with_mfa)
    assert r.status_code == 201, r.text
    db.expire_all()
    assert db.query(SysUser).filter(SysUser.username == "normal_user01").count() == 1


def test_二次验证关着时建号照旧(client, auth):
    """开关关着时这道依赖**不拦人** —— 挂着它不影响现有部署。"""
    r = client.post("/api/system/users",
                    json={"username": "no_mfa_user01", "password": "plain12345"},
                    headers=auth)
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------- 插件


def test_没有近期二次验证时装不了插件(plugin_env, client, token_without_mfa):
    """装插件是**在 API 宿主进程里装代码** —— 它必须过这道闸门。"""
    build, _ = plugin_env
    raw, _manifest, _, _ = build()
    r = client.post("/api/system/plugins/install",
                    files={"file": ("test.cwp", raw)}, headers=token_without_mfa)
    assert r.status_code == 403, r.text
    assert "二次验证" in r.text, r.text


def test_插件的读接口不受这道闸门影响(plugin_env, client, token_without_mfa):
    """**读**不该被拦：管理员在没做二次验证时也该能看见插件列表。"""
    r = client.get("/api/system/plugins", headers=token_without_mfa)
    assert r.status_code == 200, r.text
    assert "plugins" in r.json()


@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/api/system/plugins/reconcile", {}),
    ("post", "/api/system/plugins/revoke-key", {"json": {"key_id": "test"}}),
    ("post", "/api/system/plugins/maintenance/prune", {"json": {}}),
    ("post", "/api/system/plugins/some-plugin/enable", {}),
    ("post", "/api/system/plugins/some-plugin/disable", {}),
    ("post", "/api/system/plugins/some-plugin/upgrade", {}),
    ("delete", "/api/system/plugins/some-plugin", {}),
    ("put", "/api/system/plugins/some-plugin/users", {"json": {"user_ids": []}}),
    ("post", "/api/system/plugins/some-plugin/users/1", {}),
    ("delete", "/api/system/plugins/some-plugin/users/1", {}),
    ("post", "/api/system/plugins/some-plugin/tables/cleanup",
     {"json": {"confirmation": "some-plugin"}}),
])
def test_插件的所有写接口都要近期二次验证(client, token_without_mfa,
                                            method, path, kwargs):
    """**整个写面都要盖住**，不是只盖 install 一条。

    闸门在依赖里解析，所以它**先于**路由体执行 —— 于是不管插件存不存在、
    参数对不对，回的都是同一个 403（不会顺带泄漏「这个插件 id 存不存在」）。
    """
    r = getattr(client, method)(path, headers=token_without_mfa, **kwargs)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert "二次验证" in r.text, (path, r.text)


def test_带近期二次验证的令牌可以装插件(plugin_env, client, token_with_mfa):
    """闸门不是「一律拒绝」：验过二次验证就放行。"""
    build, _ = plugin_env
    raw, _manifest, _, _ = build()
    r = client.post("/api/system/plugins/install",
                    files={"file": ("test.cwp", raw)}, headers=token_with_mfa)
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------- 顺序


def test_不是管理员的提示说的是管理员而不是二次验证(client, plain_auth):
    """**管理员判定必须先于二次验证判定。**

    否则一个普通用户会看到「你需要二次验证」—— 那是在教他「再过一次就能
    装插件」，而真相是「这里跟你没关系」。
    """
    r = client.post("/api/system/plugins/reconcile", headers=plain_auth)
    assert r.status_code == 403, r.text
    assert "管理员" in r.text, r.text
    assert "二次验证" not in r.text, r.text
