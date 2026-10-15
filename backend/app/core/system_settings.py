"""系统级配置的读取与写入（第三方服务凭据）。

数据在 ``sys_setting`` 表里，key 是纯文本、value 是字符串。**只有这里**允许
直接把 value 当凭据用；往外发的地方一律走 :func:`mask_secret`。

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


def mask_secret(value: str) -> str:
    """给审计日志用：只留末 4 位。

    审计表是**给所有人看的**（操作日志页普通管理员都能翻），所以进日志的必须是
    掩码而不是明文 —— 否则「谁改了 token」这条日志本身就成了一次泄露。
    """
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) >= 4 else "…"
