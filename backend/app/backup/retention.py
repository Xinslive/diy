"""保留策略：月度永久、周 1 年、天 30 天。

**「月度永久」是结构上成立的，不是一个写死的 99999 天。**
本模块对 tier=monthly 压根没有删除分支（见下面的早退）。如果把它写成
「保留 99999 天」，那么每份月度备份都会带着一条「再过 273 年过期」的元数据，
而将来有人把「99999」改小、或者写一个从配置读天数的循环 —— 月度备份就被清空了。
而那恰恰是唯一一份「出事时能救回来」的东西。

**按对象键的前缀删，而不是按「备份记录」删。** 记录表 ``sys_backup_run`` 只用来
判断分级与展示历史，不参与清理 —— 因为它是数据库里的东西，而**数据库正是可能
已经坏掉的那一份**。用它决定要删掉谁，等于让退路自己决定自己的生死。真出事时
那条查询本身就跑不出来。

**按对象的修改时间判龄，不用键名里的日期。** 键名是「生成时刻」，
LastModified 是「OSS 上真正写下的时刻」。两者在重传、跨区复制、生命周期规则
搬过之后会不一致；而清理动作真正该依据的是后者 —— 删的是 OSS 上那个对象，
判龄就该看 OSS 说它是什么时候写的。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from app.backup import tier as tier_mod
from app.backup.oss_client import OssError, delete_object, iter_objects
from app.models import TIER_MONTHLY, TIER_WEEKLY

# 只删我们自己格式的对象。
#
# 清理是**唯一**一个会对远端做不可逆删除的动作，而它跑在一个别人也在用的
# Bucket 上。按扩展名收窄，等于「即使前缀算错了，也删不到别人的东西」。
# （前缀算错已经不可能发生 —— tier 是键的第一层目录，见 tier.object_key ——
# 但多一道收窄的成本几乎为零。）
_OUR_SUFFIX = ".json.gz"


@dataclass(frozen=True)
class Deleted:
    key: str
    tier: str
    age_days: float


def _age_days(obj_last_modified: Optional[datetime], now: datetime) -> float:
    """对象存了多久（天）。

    **``last_modified`` 缺失时按「刚写下」处理**，返回 0 —— 于是这个对象这一轮
    不会被删。宁可多留一份（下次清理还会再看它），也不要因为远端没返回这个
    字段就把一份备份当成陈旧品清掉。
    """
    if obj_last_modified is None:
        return 0.0
    lm = obj_last_modified
    if lm.tzinfo is None:
        # oss2 返回 naive 时按 UTC 解（它就是从 HTTP 头取的，HTTP 时间是 UTC）。
        lm = lm.replace(tzinfo=timezone.utc)
    return max(0.0, (now - lm).total_seconds() / 86400.0)


def plan_deletions(objects: Iterable, tier: str, keep_days: int,
                   now: datetime) -> list[tuple[str, float]]:
    """从已列出的对象里挑出该删的。纯函数，好测。

    **月度在这里也早退**，而不仅仅是在 :func:`apply_retention` 里被跳过。
    两处都挡是刻意的：``apply_retention`` 那一处保证「不会去列月度目录」，
    这一处保证「就算有人将来新写一条直接调 ``plan_deletions`` 的清理路径，
    也删不掉月度档」。一个月度备份被删掉的代价，是把唯一那份「出事能救回来」
    的东西删掉，而触发它的可能只是一次重构里多写的一行循环。
    """
    if tier == TIER_MONTHLY or keep_days <= 0:
        return []
    out = []
    for obj in objects:
        if not obj.key.endswith(_OUR_SUFFIX):
            continue
        age = _age_days(obj.last_modified, now)
        if age > keep_days:
            out.append((obj.key, age))
    return out


def apply_retention(bucket, prefix: str, keep_days: dict[str, int],
                    now: Optional[datetime] = None,
                    dry_run: bool = False) -> list[Deleted]:
    """按档位清理过期产物，返回实际（dry_run 时为将）删掉的对象。"""
    now = now or datetime.now(timezone.utc)
    deleted: list[Deleted] = []

    # 月度明确列出、明确跳过：与其靠「KEEP_DAYS 里恰好是 0」这种间接约定，
    # 不如在这里写一句人话 —— 将来有人把 KEEP_DAYS 改成 30，行为也不变。
    tiers = [t for t in (tier_mod.TIER_DAILY, TIER_WEEKLY, TIER_MONTHLY)]
    for t in tiers:
        if t == TIER_MONTHLY:
            continue
        keep = keep_days.get(t, 0)
        if keep <= 0:
            continue
        p = tier_mod.tier_prefix(prefix, t)
        objects = list(iter_objects(bucket, p))
        for key, age in plan_deletions(objects, t, keep, now):
            if not dry_run:
                delete_object(bucket, key)
            deleted.append(Deleted(key=key, tier=t, age_days=age))
    return deleted
