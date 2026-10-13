"""IP 维度的登录失败聚合状态（审计 P2-11 的另一半）

Revision ID: 0008_login_ip_throttle
Revises: 0007_subject_leaf_parent_guard
Create Date: 2026-10-13

**这张表为什么必须存在。**

P2-11 的前半段（登录时序侧信道）早已修掉：未知用户名与错误口令耗时
33.4ms vs 32.7ms，扫用户名不再免费。而后半段一直空着：``sys_user`` 上
的 ``failed_logins`` / ``locked_until`` **只按账号**。一个攻击源只要轮换
用户名，就能对上万个账号各试 5 次 —— 每次只锁一个账号，锁定机制几乎不生效，
而审计里留下的是成千上万条互不相干的 ``LOGIN_FAILURE``，事后没有任何一行
能说清「这些来自同一个源」。

这张表是那个「源」：一个来源 IP 一行，记滑动窗口内的失败总数。

**为什么没有重放 db_init.sql。**

0005 立的规矩是「改了 ``db_init.sql`` 就得让存量库重放一遍」，本条**没有改
那份文件**，所以不重放：这里交付的是一张**新表**，不是一条护栏。
``db_init.sql`` 里从头到尾只有触发器与约束（建表是 ``0001_baseline`` 冻结的
显式 DDL 的事），新表按 ``0002`` / ``0004`` / ``0006`` 的老路数由本条 revision
自己 ``create_table`` —— 与模型 ``models/system.py::LoginIpThrottle`` 逐列对齐。

顺带说明两件「本可以加、但没加」的事，都是为了不给这个工作线增加分歧面：

* **没有 CHECK 约束**（比如 ``failures >= 0``）。加了就必须在模型侧也声明一份，
  两份声明一旦对不上，``test_migration_guardrails.py::test_模型与库的外键零差异``
  那条就会红；而这个计数在应用侧由一条原子 upsert 写出，本来就不可能为负。
* **没有把本表加进 db_init.sql 第 7 段那张 ``trg_updated_at`` 表清单**。那张清单
  管的是「数据库自己改写行时也刷新 updated_at」（P2-09），而这张表**只由应用写**，
  且 ``auth.py`` 的 upsert 在语句里显式写了 ``updated_at = now()``（那条语句绕过了
  ORM 的 ``onupdate``，所以必须自己写）。为了一个已经覆盖到的语义去改清单、
  再让存量库重放整份 SQL，是把收益为零的分歧风险买了进来。

**幂等。** ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` +
``alter_column``（撤 server_default，本身可重复执行）：运维重跑 ``upgrade head``
不会撞 DuplicateTable，也不会在这张表上多出第二份结构。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_login_ip_throttle"
down_revision = "0007_subject_leaf_parent_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_ip_throttle",
        sa.Column("id", sa.Integer(), nullable=False),
        # 45 = IPv6 最长文本长度。与审计表里那个 String(45) 的 ip 同规格：
        # 全项目只用一种 IP 的文本表示，比较时不会踩「inet vs varchar」的坑。
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        # 三个时间列都是 naive UTC，与 sys_user.locked_until 同一口径
        # （Python 侧一律走 auth.py::_utcnow()）。混用 aware/naive 会让
        # 窗口在东八区部署时凭空差 8 小时。
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(), nullable=False),
        # 仅在显式开启 LOGIN_IP_HARD_LOCK 时才写；默认永远 NULL。
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 这条唯一约束是整张表并发正确性的全部基础：upsert 靠它把
        # 「读计数 + 加一」压成一条原子语句。少了它只能先 SELECT 再 UPDATE，
        # 两个并发请求各读到同一个旧值，失败数只涨 1 —— 攻击者并发打就废掉了。
        sa.UniqueConstraint("ip", name="uq_login_ip_throttle_ip"),
        if_not_exists=True,
    )
    # 清理入口按 last_failed_at 扫，而清理是在登录路径上顺带做的：
    # 没有索引就是每次清理一次全表扫。
    op.create_index("ix_login_ip_throttle_last_failed", "login_ip_throttle",
                    ["last_failed_at"], unique=False, if_not_exists=True)

    # server_default 只负责让这张刚建出来的表有值；应用侧是显式写的，
    # 这层默认撤掉，免得将来加默认值时忘了这一列（与 0002 同一条理由）。
    op.alter_column("login_ip_throttle", "failures", server_default=None)


def downgrade() -> None:
    # 只回滚本条新增的那张表。护栏、约束一概不动。
    op.drop_index("ix_login_ip_throttle_last_failed", table_name="login_ip_throttle",
                  if_exists=True)
    op.drop_table("login_ip_throttle", if_exists=True)
