"""系统级配置的读取与写入（第三方服务凭据、自助注册开关）。

数据在 ``sys_setting`` 表里，key 是纯文本、value 是字符串。**凭据类的 key**
才允许直接把 value 当凭据用，且往外发的地方一律走 :func:`mask_secret`；
非凭据类（本文件末尾的自助注册开关）返回的是布尔，天然没有明文问题。

为什么库里没有就退回环境变量：容器部署仍会用 ``.env`` 注入，凭据不必搬进库
就能跑。两侧同时存在时**库优先** —— 界面是运维真正会用的那个入口，让它说了算，
否则会出现「界面上改了却没生效，而没人知道为什么」。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SystemSetting

# 存储 key -> (返回给调用方的名字, app.core.config.Settings 上的字段名)。
#
# 写成一张表而不是三份散开的列表：这几个名字过去在「库里存什么」「函数返回什么」
# 「env 里叫什么」之间各用各的，于是 ocr_config_public 按错的一套取值，直接
# KeyError。少一个「键」的映射，就少一处能对不上的地方。
#
# 返回名去掉了 ``ocr_`` 前缀 —— 既然它已经是「一份 OCR 配置」，调用方拿到
# ``api_url`` 就够，不必每次都写 ``ocr_api_url``。
OCR_SPEC = {
    "ocr_api_url": ("api_url", "invoice_ocr_api_url"),
    "ocr_api_token": ("token", "invoice_ocr_api_token"),
    "ocr_model": ("model", "invoice_ocr_model"),
    "ocr_timeout_seconds": ("timeout_seconds", "invoice_ocr_timeout_seconds"),
    "ocr_poll_seconds": ("poll_seconds", "invoice_ocr_poll_seconds"),
    "ocr_optional_payload": ("optional_payload", "invoice_ocr_optional_payload"),
}

# 哪几个返回键带类型转换 —— 界面上的输入框要数字与小数，存进去的是文本。
OCR_NUMERIC = {"timeout_seconds": int, "poll_seconds": float}

# 可以原样回给前端的键。token 不在其中 —— 它只有写、没有读。
OCR_PUBLIC_KEYS = ("api_url", "model", "timeout_seconds", "poll_seconds", "optional_payload")


def get_value(db: Session, key: str, default: str = "") -> str:
    row = db.get(SystemSetting, key)
    return row.value if row is not None and row.value != "" else default


def set_value(db: Session, key: str, value: str, *, updated_by: str | None = None) -> None:
    row = db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key)
        db.add(row)
    row.value = value
    if updated_by is not None:
        row.updated_by = updated_by


def ocr_config(db: Session) -> dict[str, Any]:
    """OCR 的**生效配置**：库里有的优先，其余退回 env。"""
    from app.core.config import settings

    values: dict[str, Any] = {}
    for storage_key, (name, env_name) in OCR_SPEC.items():
        override = get_value(db, storage_key, "")
        raw = override if override else getattr(settings, env_name)
        values[name] = OCR_NUMERIC[name](raw) if name in OCR_NUMERIC else raw
    return values


def ocr_config_public(db: Session) -> dict[str, Any]:
    """给界面看的形态：**没有 token 明文**，只有「配没配」与末几位。

    ``token_set`` 是这个接口存在的主要理由：管理员要能回答「到底配没配上」，
    但不需要（也不应该）能把 token 再读出来贴到别处。
    """
    config = ocr_config(db)
    token = config["token"] or ""
    return {
        **{key: config[key] for key in OCR_PUBLIC_KEYS},
        "token_set": bool(token),
        "token_hint": f"…{token[-4:]}" if len(token) >= 4 else ("已设置" if token else ""),
    }


# --------------------------------------------------------------- 自助注册开关
#
# 与 OCR / 备份同一个约定：**库里优先，env 兜底**。原来这个开关只在 env 里
# （``REGISTRATION_ENABLED``），于是「关掉自助注册」在生产上意味着：登进服务器、
# 改 .env、重启后端 —— 一件需要服务器权限的动作。实际部署里它常年关着，
# 于是这个开关「有用」这件事没人说得清，而登录页上的「注册」页签一直在
# （前端不知道这个开关存在，见 frontend/src/views/LoginView.vue）。
#
# 放进库里之后，管理员在「用户与授权」页上点一下就能改，改完立即生效。
#
# **布尔值的解析与 backup/config.py 保持同一套写法**（1/true/yes/on 为真）：
# 这张表的值全是文本，同一个 key 用两套解析方式，迟早出现「界面上显示关着、
# 接口却开着」这种自己都解释不了的错位。

#: 库里存这个开关的 key。与 env 的名字一致，省掉一次映射。
REGISTRATION_KEY = "registration_enabled"

_TRUTHY = ("1", "true", "yes", "on")


def registration_enabled(db: Session) -> bool:
    """自助注册当前是否开放。**库里优先，env 兜底。**

    库里没有这一行时读 ``settings.registration_enabled``（默认 False），
    所以升级到这一版之后，服务器上原有的 ``REGISTRATION_ENABLED=false``
    立刻继续生效 —— 不需要谁去库里补一行，注册也不会在升级过程中被打开。
    """
    from app.core.config import settings

    raw = get_value(db, REGISTRATION_KEY, "")
    if not raw:
        return bool(settings.registration_enabled)
    return raw.strip().lower() in _TRUTHY


def set_registration_enabled(db: Session, value: bool, *, updated_by: str | None = None) -> None:
    """写这个开关。只存文本，与同表其它配置一致。"""
    set_value(db, REGISTRATION_KEY, "1" if value else "0", updated_by=updated_by)


def mask_secret(value: str) -> str:
    """给审计日志用：只留末 4 位。

    审计表**只对系统管理员可见**（两个读取接口都要求管理员，见
    ``api/system.py``），但它仍然是**全员可读的管理员面** —— 也就是说
    任何一个拿到管理员权限的人（以及任何能读到审计表的运维、备份、
    日志归档）都能看到这些值。所以进日志的必须是**掩码**而不是明文 ——
    否则「谁改了 token」这条日志本身就成了一次泄露。
    """
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) >= 4 else "…"
