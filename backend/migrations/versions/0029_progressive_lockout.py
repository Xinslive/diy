"""递进式登录锁定：给 sys_user 加一列 lock_level

Revision ID: 0029_progressive_lockout
Revises: 0028_scheduled_tasks
Create Date: 2026-10-19

**这一列解决的是什么问题。**

旧实现里「锁多久」是一个常数（``login_lock_minutes``，15 分钟）：连续失败
到阈值就锁 15 分钟，锁完再错再锁，还是 15 分钟。对**慢速爆破**（每隔
14 分钟试一次，每次只错一次，永远不触发阈值）完全无效；对**故意撞锁**
的人也没有任何代价 —— 攻击者可以精确地把每次尝试安排在锁定窗口之外，
让这个账号被无限期挡在门外。

递进式锁定把「已经被锁过几轮」记下来：第 N 轮锁 ``login_lock_minutes × N``
分钟（10 → 20 → 30 …，上限 ``login_lock_max_minutes``）。越撞越久，
慢速爆破的收益随之归零。

**为什么必须是新的一列，不能复用 failed_logins。**

``failed_logins`` 表达的是「离下一次锁定还差几次」—— 触发锁定时它被清零
（见 ``app/api/auth.py::_record_failure``）。也就是说，恰恰在**最需要知道
「已经锁过几轮」的那一刻，这个信息被清掉了**。复用它就等于让递进永远是
第一档。``lock_level`` 只增不减，只在两件事发生时清零：登录成功、管理员
提前解除锁定 —— 这两件事都意味着账号回到了正常状态。

**为什么不重放 db_init.sql。**

0005 立的规矩是「改了 db_init.sql 就得让存量库重放一遍」，本条**没有改
那份文件**：这里交付的是一张**已存在表上的新列**，由本 revision 自己
``add_column``，与模型 ``models/system.py::SysUser.lock_level`` 逐列对齐。
``db_init.sql`` 里从头到尾只有触发器与约束（建表是 0001_baseline 冻结的
显式 DDL 的事），新列不涉及那一层。

**幂等。** ``add_column`` 本身在重复执行时会撞 DuplicateColumn，因此这里
先用 ``IF NOT EXISTS`` 语义（PG 9.6+ 的 ``ADD COLUMN IF NOT EXISTS``）。
生产上的迁移由 ``alembic upgrade head`` 串行执行一次，幂等只是为了让
「运维手工重跑」也不会炸。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_progressive_lockout"
down_revision = "0028_scheduled_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 递进轮次。NOT NULL + server_default '0'：存量行必须有值，否则
    # 「读出来是 NULL」和「读出来是 0」两条路径都得写，而 NULL 那条
    # 迟早有人忘（与 0002 的 failed_logins 同一条理由）。
    op.execute("ALTER TABLE sys_user ADD COLUMN IF NOT EXISTS "
               "lock_level integer NOT NULL DEFAULT 0")

    # server_default 只负责让存量行有值；应用侧是显式写 0 的，
    # 所以这层默认可以撤掉，避免将来加默认值时忘了这一列。
    op.alter_column("sys_user", "lock_level", server_default=None)


def downgrade() -> None:
    op.drop_column("sys_user", "lock_level")
