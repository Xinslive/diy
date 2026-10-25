"""备份配置：OSS 参数与保留策略，存 ``sys_setting``，库里优先、env 兜底。

**为什么凭据从 .env 搬进库。** 与 OCR 那套完全同一个理由（见
``app/core/system_settings.py``）：改一个 AccessKey 不该要求重启后端、
改部署机上的文件、还要有人会 ssh 上去。凭据是会过期、要轮换的东西，
让它只能靠重启来改，结果就是没人改 —— 于是备份在某天悄悄失效，
而配置「明明配过」。

**为什么 AccessKey 两项都只写不读。** 它们是一个凭据对，单独一个 AK id 没有
用处，但**分开授权是 OSS 的常态**（一个 AK 管只读一个管上传）。读接口若把
AK id 原样回给前端，它就会出现在浏览器历史、网络面板和一次误开的调试窗口里 ——
那正是当初把 OCR token 做成只写的原因，所以这里照抄，不因为「id 不敏感」就开口子。
界面能回答的只有「配没配」和末 4 位，这已经够用了。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core import system_settings
from app.models import KEEP_DAYS, TIER_DAILY, TIER_MONTHLY, TIER_WEEKLY

logger = logging.getLogger(__name__)

# 存储 key -> (返回给调用方的名字, app.core.config.Settings 上的字段名)。
# 与 system_settings.OCR_SPEC 同一个写法与同一个理由：这三处名字（库里存什么、
# 函数返回什么、env 里叫什么）分成三份时，一定会有一处对不上。
OSS_SPEC = {
    "oss_endpoint": ("endpoint", "oss_endpoint"),
    "oss_access_key_id": ("access_key_id", "oss_access_key_id"),
    "oss_access_key_secret": ("access_key_secret", "oss_access_key_secret"),
    "oss_bucket": ("bucket", "oss_bucket"),
    "oss_prefix": ("prefix", "oss_prefix"),
}

# 可以原样回给前端的键。两个 AccessKey 项都不在其中 —— 只有写、没有读。
OSS_PUBLIC_KEYS = ("endpoint", "bucket", "prefix")

# 对象键的默认顶层前缀。**不是**空串：空串意味着备份对象直接铺在 Bucket 根目录，
# 而这个 Bucket 可能还放着别的东西。放进一个命名空间，将来清理、迁移、
# 配生命周期规则都只在自己的一亩地里。
DEFAULT_PREFIX = "acct-backup"


def _settings():
    from app.core.config import settings
    return settings


def oss_config(db: Session) -> dict[str, Any]:
    """OSS 的**生效配置**：库里有的优先，其余退回 env。"""
    values: dict[str, Any] = {}
    for storage_key, (name, env_name) in OSS_SPEC.items():
        override = system_settings.get_value(db, storage_key, "")
        values[name] = override if override else getattr(_settings(), env_name, "")
    return values


def oss_config_public(db: Session) -> dict[str, Any]:
    """给界面看的形态：**两个 AccessKey 都没有明文**。"""
    cfg = oss_config(db)
    ak_id = cfg["access_key_id"] or ""
    ak_secret = cfg["access_key_secret"] or ""
    return {
        **{key: cfg[key] for key in OSS_PUBLIC_KEYS},
        "prefix": (cfg["prefix"] or DEFAULT_PREFIX).strip("/"),
        "access_key_id_set": bool(ak_id),
        "access_key_id_hint": system_settings.mask_secret(ak_id),
        "access_key_secret_set": bool(ak_secret),
        "access_key_secret_hint": system_settings.mask_secret(ak_secret),
    }


def _int_setting(db: Session, key: str, env_name: str, default: int) -> int:
    raw = system_settings.get_value(db, key, "")
    if not raw:
        raw = getattr(_settings(), env_name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        # 存进来一个手滑的值不该让备份整个瘫掉 —— 退回默认档继续跑，
        # 页面上显示的是实际生效的那个天数，运维看得见自己填错了。
        return default


def retention_days(db: Session) -> dict[str, int]:
    """各级保留天数。月度恒为 0（永不删），见 models/backup.py 的 KEEP_DAYS。"""
    return {
        TIER_DAILY: max(1, _int_setting(db, "backup_keep_daily_days",
                                        "backup_keep_daily_days", KEEP_DAYS[TIER_DAILY])),
        TIER_WEEKLY: max(1, _int_setting(db, "backup_keep_weekly_days",
                                         "backup_keep_weekly_days", KEEP_DAYS[TIER_WEEKLY])),
        # 月度**不看配置**：保留策略是产品口径，不是运维可调项。
        # 做成可配就意味着某天有人把「0」填成「30」，然后月度备份被清空，
        # 而那恰恰是唯一一份「出事时能救回来」的东西。
        TIER_MONTHLY: 0,
    }


def is_enabled(db: Session) -> bool:
    """定时备份总开关。关掉之后脚本仍可被手动触发（见 service.run_backup）。"""
    raw = system_settings.get_value(db, "backup_enabled", "")
    if not raw:
        return bool(getattr(_settings(), "backup_enabled", True))
    return raw.strip().lower() in ("1", "true", "yes", "on")


def include_system(db: Session) -> bool:
    """是否额外备份系统级数据（用户、授权、参数、全局基线模板）。

    默认**要**。用户与账套授权不在任何一个账套里，只备账套就意味着
    「全员账号与权限」在任何一份备份里都不存在 —— 出事要恢复时得重建全部账号，
    而这件事没法靠回忆做。数据量与账套备份完全不在一个量级。
    """
    raw = system_settings.get_value(db, "backup_include_system", "")
    if not raw:
        return bool(getattr(_settings(), "backup_include_system", True))
    return raw.strip().lower() not in ("0", "false", "no", "off")


class ConfigIncomplete(Exception):
    """OSS 参数没配全。消息必须自带「缺哪个」和「去哪儿填」。"""


def require_ready(db: Session) -> dict[str, Any]:
    """取出生效配置，缺项直接抛 :class:`ConfigIncomplete`。

    **不返回半份配置让调用方自己判断。** 缺 AccessKey 时 oss2 会在几层之后
    抛一个签名相关的错，而缺 Bucket 时它抛的是 ``NoSuchBucket`` —— 两者都不
    指「你没配」。这里把缺项一次性说清楚，是为了让「数据安全」页上那个红字
    直接就是可执行的下一步。
    """
    cfg = oss_config(db)
    missing = [label for label, key in (
        ("Endpoint", "endpoint"),
        ("AccessKey ID", "access_key_id"),
        ("AccessKey Secret", "access_key_secret"),
        ("Bucket", "bucket"),
    ) if not (cfg.get(key) or "").strip()]
    if missing:
        raise ConfigIncomplete(
            f"OSS 参数未配置完整，缺少：{'、'.join(missing)}。"
            "请到「系统管理 → 数据安全」填写后重试。")
    cfg["prefix"] = (cfg["prefix"] or DEFAULT_PREFIX).strip("/")
    return cfg


class EncryptionKeyMissing(ConfigIncomplete):
    """OSS 备份配好了，但**备份加密密钥**没配 —— 制品会以**明文 gzip** 上传。

    **继承** :class:`ConfigIncomplete` 是有意的：调用方（``scripts/backup_to_oss.py``、
    ``api/system.py``）已经在 catch 那个异常，而「备份没配好」正是这里发生的事。
    """


def encryption_ready() -> bool:
    """备份加密密钥配好了没有（**不抛异常**）。

    问的是「制品会不会是密文」，不是「OSS 配了没有」——
    给「测试连接」那个按钮用：**连通性归连通性、密文归密文**，两件事分开报。
    正在配 OSS 的人不该被「先配一把密钥」拦住才能测连通性；
    但「连通但备份跑不起来」这件事必须当场说出来。

    判据仍然是**生产者本人**（``export.encryption_key``）：
    它抛 ``ExportRefused`` = 配了但不合格 → 那更不能算「ready」。
    """
    from app.backup import export as export_mod

    try:
        return export_mod.encryption_key() is not None
    except export_mod.ExportRefused:
        return False


def require_encrypted_ready(db: Session) -> dict[str, Any]:
    """取出生效配置，**并且**要求备份制品一定是密文。

    **为什么启动期的闸门不够（审计报告 §3.8 的复核结论，2026-10-25 补）。**

    启动闸门（``core/config.py::validate_production_settings``）看的是
    **env 层**的 OSS 参数 —— 启动期没有库连接，看不到**界面上填的那一套**。
    而备份管线真正用的是**生效配置**（:func:`oss_config`：库里优先、env 兜底）。

    于是「在界面上配 OSS」这条**产品自己指给运维的路**
    （``require_ready`` 的报错原文就是「请到「系统管理 → 数据安全」填写后重试」）
    上，启动闸门**不响**，每晚的备份就以**明文 gzip** 躺在对象存储上 ——
    审计 §3.8 写的那条失败场景，在这条路径上**原样可达**。

    所以这里把同一道闸搬到**运行期**、按**生效配置**判 ——
    一处实现同时覆盖 env 与界面两条来源。

    **判据用生产者本人**（``export.encryption_key``）：
    它返回 None = 没配（不加密），它抛 ``ExportRefused`` = 配了但不合格。
    不另写一份「算不算配了」的判据 —— 启动闸门说「合格」而导出说「拒绝」，
    那这道闸就是骗人的（而且是最坏的那种骗法：它让人以为备份是加密的）。

    **豁免口**与启动闸门同一个：``BACKUP_ENCRYPTION_KEY_WARN_ONLY=true``
    （登记在 ``app/core/escape_hatches.py``）。
    """
    cfg = require_ready(db)

    from app.backup import export as export_mod

    try:
        key = export_mod.encryption_key()
    except export_mod.ExportRefused:
        # 配了但不合格（不是合法 base64 / 不是 32 字节）—— 让生产者自己说为什么，
        # 它的话最准（还带着「怎么生成」的命令）。
        raise
    if key is not None:
        return cfg

    if _settings().backup_encryption_key_warn_only:
        logger.warning(
            "OSS 备份未加密：没有配置 BACKUP_ENCRYPTION_KEY —— 已按 "
            "BACKUP_ENCRYPTION_KEY_WARN_ONLY=true 放行，"
            "备份制品将以明文 gzip 存放在 OSS 上。")
        return cfg

    raise EncryptionKeyMissing(
        "OSS 备份已配置，但没有配置备份加密密钥（BACKUP_ENCRYPTION_KEY）—— "
        "**备份制品会以明文 gzip 上传到对象存储**。"
        "备份管线是 gzip → SHA256 → OSS，而 SHA256 是**校验和不是加密**："
        "桶的读权限一旦泄露（AK 泄露 / 桶策略过宽 / 云侧账号被盗），"
        "全部账套的凭证与金额就一起出去了。"
        "生成一把：python -c \"import base64,os;"
        "print(base64.b64encode(os.urandom(32)).decode())\"；"
        "若确实要先用明文跑，设 BACKUP_ENCRYPTION_KEY_WARN_ONLY=true。")

