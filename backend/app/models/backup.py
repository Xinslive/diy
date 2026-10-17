"""数据安全：备份运行记录。

这张表存在的理由是**「备份看起来在跑」不等于「备份真的产出了东西」**。
本仓库的 ``scripts/db_backup.py`` 为这件事已经吃过一次教训：早期只有 dump、
没有 restore，也没有产物校验，于是「备份能用」被当成了「出事能救回来」。
OSS 备份要更小心 —— 它跑在一台**别的机器**上，写的是**别人的存储**，
而这两件事都不在本地日志的视野里：

* 一次上传「成功」可能只意味着写进了请求队列，而对象根本没落盘；
* 清理策略写错会**把永久保留的月度备份一起删掉**。

所以每次运行都落一行：成功与否、对象键、字节数、SHA256、行数、错误全文。
界面上「数据安全」页看到的就是这张表 —— 运维判断退路在不在，依据是它，
而不是 systemd 那个永远显示 active 的 timer。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (BigInteger, DateTime, Index, Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# 三级保留级别，同时也是 OSS 对象键的**第一层目录**。
#
# 分级语义（用户口径）：每天一次、每周一次、每月一次；
# 月度永久保存、周保存一年、天保存 30 天。
# 分级判重见 ``app/backup/tier.py::classify``。
TIER_DAILY = "daily"
TIER_WEEKLY = "weekly"
TIER_MONTHLY = "monthly"
TIERS = (TIER_DAILY, TIER_WEEKLY, TIER_MONTHLY)

# 保留天数。0 = 永不删除。
#
# **月度为什么是 0 而不是一个大数字。** 「永久」不是一个能靠清理天数实现的
# 性质 —— 写成一个有限的天数，意味着过了那天它照样被删，而且没有任何代码
# 提醒你「你已经把月度备份也清掉了」。这里让月度这一档**根本不存在删除分支**
# （``prune_prefix`` 对 tier 为月度的前缀直接返回），于是「月度永久」是结构上
# 成立的，不是一个约定。
KEEP_DAYS = {TIER_DAILY: 30, TIER_WEEKLY: 365, TIER_MONTHLY: 0}

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

SCOPE_BOOK = "BOOK"
SCOPE_SYSTEM = "SYSTEM"


class BackupRun(Base, TimestampMixin):
    """一次备份尝试（一行 = 一次尝试，失败也留痕）。"""

    __tablename__ = "sys_backup_run"
    __table_args__ = (
        # 分级判重要问的是「这个账套这个月/这周**成功过没有**」，最常见的查询是
        # ``WHERE book_id=? AND tier=? AND status='success' AND created_at >= ?``。
        # 没有这份复合索引时，那条查询在记录攒够之后就是全表扫 —— 而它每跑一次
        # 备份都要对每个账套问一遍。
        Index("ix_backup_run_scope_tier_time", "scope", "book_id", "tier", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # 账套 id。系统级快照（用户、授权、参数）没有账套，为 NULL。
    # 故意**不建外键**：账套可以被删，而备份记录必须活过它的账套 ——
    # 出了事正要靠它找回数据，那时账套没了是常态而不是意外。
    book_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ``BOOK`` / ``SYSTEM``。与 book_id 一起构成备份对象的唯一身份。
    scope: Mapped[str] = mapped_column(String(16), default=SCOPE_BOOK, nullable=False)

    # 冗余可读标签。账套改了名或被删了，这条记录仍要能一眼看懂它备份的是什么。
    book_label: Mapped[str] = mapped_column(String(256), default="", nullable=False)

    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # 产物的 OSS 对象键。失败时为空串 —— 失败不该留下一个指向对象的键，
    # 否则「有键就说明有对象」这条推断会骗人。
    oss_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    # 压缩后的字节数（就是 OSS 上真正占的字节）与压缩前的行数。
    # 两个都记：字节数回答「占了多少钱」，行数回答「备份到了东西没有」。
    # 一份 0 行但非零字节的备份是有可能的（账套刚建、全是科目表头），
    # 反过来一份 0 字节的绝不可能 —— 那层拦截在 ``export.py`` 里。
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 产物的 SHA256（对**压缩后**的字节算，与 OSS 上存的东西逐字节对应）。
    # 用途是事后发现「备份文件本身坏了」—— OSS 不会告诉你它悄悄损坏了什么，
    # 而损坏的备份只有在你需要它的那天才会被发现。
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # 失败时的完整原因。**要全文，不要摘要** —— 排障时「上传失败」四个字
    # 等于没写，而重新触发一次上传再猜一次要贵得多。
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
