"""邮件通知：SMTP 配置的读取与写入。

与 ``core/system_settings.py`` 里 OCR / 备份那套**完全同一个约定**：

* 值存在 ``sys_setting`` 表，**库里优先、env 兜底**（容器首次启动时还没
  建过库，那时界面上什么都填不了，总得有一条路先把凭据放进去）；
* 凭据类字段**只写不读** —— GET 回来的是「配没配」与末几位，明文永远
  不在任何响应里；
* 布尔值用同一套解析（1/true/yes/on 为真），不另起一套。

**为什么 SMTP 密码的存储方式与 OSS AccessKey 一样（明文入库）。**

这是刻意的**一致性选择**，不是没意识到问题：审计 P0-1 已经指出
``sys_setting`` 明文存凭据（OSS AccessKey、OCR token）会让「拿到库 =
拿到凭据」，而备份制品里又装着同一批凭据。这条要修，但它是一次**跨全表
的存储方案变更**（信封加密 / KMS / 角色化临时凭证），牵涉密钥保管与
轮换流程，不是新增一个 SMTP 设置能顺手带出来的。

所以这里的取舍是：**沿用现有约定，把这条债明确标出来**，而不是在一个新
功能里偷偷引入一套不同的加密方案（那会让运维困惑「为什么 OCR 是明文、
SMTP 不是」，且密钥轮换语义不一致）。真要修，按 P0-1 的整改方案统一做。

**口令不出现在任何响应里**，这是现在就能做到的、且不引入新债的部分。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.system_settings import get_value, set_value

# 存储 key -> (返回给调用方的名字, app.core.config.Settings 上的字段名)。
#
# 写成一张表而不是几份散开的列表：这几个名字过去在「库里存什么」「函数返回
# 什么」「env 里叫什么」之间各用各的，于是 ocr_config_public 按错的一套取值，
# 直接 KeyError。少一个「键」的映射，就少一处能对不上的地方。
SMTP_SPEC: dict[str, tuple[str, str]] = {
    "smtp_host": ("host", "smtp_host"),
    "smtp_port": ("port", "smtp_port"),
    "smtp_username": ("username", "smtp_username"),
    "smtp_password": ("password", "smtp_password"),
    "smtp_from": ("from_address", "smtp_from"),
    "smtp_use_ssl": ("use_ssl", "smtp_use_ssl"),
    "smtp_use_tls": ("use_tls", "smtp_use_tls"),
    "smtp_timeout_seconds": ("timeout_seconds", "smtp_timeout_seconds"),
}

# 哪几个返回键带类型转换 —— 界面上的输入框要数字与开关，存进去的是文本。
SMTP_NUMERIC: dict[str, type] = {"port": int, "timeout_seconds": int}
SMTP_BOOL: dict[str, type] = {"use_ssl": bool, "use_tls": bool}

# 可以原样回给前端的键。password 不在其中 —— 它只有写、没有读。
SMTP_PUBLIC_KEYS = ("host", "port", "username", "from_address",
                    "use_ssl", "use_tls", "timeout_seconds")

#: 收件人列表的存储 key（逗号分隔的一串）。
RECIPIENTS_KEY = "notify_recipients"
#: 通知总开关的存储 key。
ENABLED_KEY = "notify_enabled"

_TRUTHY = ("1", "true", "yes", "on")

#: 收件人地址的粗校验。
#:
#: 只挡明显不是地址的东西（空串、缺 @、有换行）。**不**做完整的 RFC 5322
#: 校验 —— 那既难写对，拒掉一个能收到的地址也比放进一个坏地址更糟。
#: 真正拦住坏地址的是 SMTP 服务端，它会回一个明确的错误。
def valid_recipient(value: str) -> bool:
    value = (value or "").strip()
    if not value or len(value) > 254:
        return False
    if "@" not in value:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False
    # 换行会让邮件头注入 —— 收件人地址是管理员填的，但注入的后果是
    # 「一封信发到别处去」，不能靠「管理员不会这么填」来防。
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        return False
    return True


def parse_recipients(raw: str) -> list[str]:
    """把逗号分隔的一串拆成地址列表，去掉空项与不合法的项。

    去重时保持原顺序 —— 列表顺序就是管理员排的优先级顺序。
    """
    out: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip()
        if not value or not valid_recipient(value):
            continue
        if value not in out:
            out.append(value)
    return out


def smtp_config(db: Session) -> dict[str, Any]:
    """SMTP 的**生效配置**：库里有的优先，其余退回 env。"""
    from app.core.config import settings

    values: dict[str, Any] = {}
    for storage_key, (name, env_name) in SMTP_SPEC.items():
        override = get_value(db, storage_key, "")
        raw = override if override else getattr(settings, env_name, "")
        if name in SMTP_NUMERIC:
            try:
                values[name] = SMTP_NUMERIC[name](raw)
            except (TypeError, ValueError):
                values[name] = getattr(settings, env_name)
        elif name in SMTP_BOOL:
            values[name] = _as_bool(raw, default=bool(getattr(settings, env_name, False)))
        else:
            values[name] = raw
    return values


def _as_bool(raw: Any, *, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if not text:
        return default
    return text in _TRUTHY


def recipients(db: Session) -> list[str]:
    """收件人列表：库里优先，env 兜底。"""
    from app.core.config import settings

    raw = get_value(db, RECIPIENTS_KEY, "")
    if not raw:
        raw = getattr(settings, "notify_recipients", "")
    return parse_recipients(raw)


def recipients_raw(db: Session) -> str:
    """收件人列表的**原始文本**（界面回显用，保留管理员的排版）。"""
    from app.core.config import settings

    raw = get_value(db, RECIPIENTS_KEY, "")
    return raw if raw else getattr(settings, "notify_recipients", "")


def set_recipients(db: Session, value: str, *, updated_by: str | None = None) -> None:
    """写收件人列表。**存原始文本**而不是解析后的列表 ——

    界面回显要的是管理员自己排的顺序与换行，解析成列表再存回去会把
    「重复的地址」「空项」这些人的痕迹抹掉，让人以为自己写错了。
    真正发信时才解析（见 :func:`parse_recipients`）。
    """
    set_value(db, RECIPIENTS_KEY, (value or "").strip(), updated_by=updated_by)


def notify_enabled(db: Session) -> bool:
    """通知总开关。**库里优先，env 兜底**（与自助注册开关同一套语义）。"""
    from app.core.config import settings

    raw = get_value(db, ENABLED_KEY, "")
    if not raw:
        return bool(getattr(settings, "notify_enabled", True))
    return _as_bool(raw, default=True)


def set_notify_enabled(db: Session, value: bool, *, updated_by: str | None = None) -> None:
    set_value(db, ENABLED_KEY, "1" if value else "0", updated_by=updated_by)


def smtp_config_public(db: Session) -> dict[str, Any]:
    """给界面看的形态：**没有口令明文**，只有「配没配」与末几位。

    管理员要能回答「到底配没配上」，但不需要（也不应该）能把口令再读出来
    贴到别处 —— 能读出来就意味着它会出现在浏览器历史、网络面板和一次
    误开的调试窗口里。这与 OCR token / OSS AccessKey 是同一条口径。
    """
    config = smtp_config(db)
    password = config.get("password") or ""
    return {
        **{key: config[key] for key in SMTP_PUBLIC_KEYS},
        "password_set": bool(password),
        "password_hint": f"…{password[-4:]}" if len(password) >= 4 else ("已设置" if password else ""),
        "recipients": recipients_raw(db),
        "recipient_count": len(recipients(db)),
        "enabled": notify_enabled(db),
    }
