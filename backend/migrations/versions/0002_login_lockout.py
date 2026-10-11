"""登录失败锁定：给 sys_user 加两列

Revision ID: 0002_login_lockout
Revises: 0001_baseline
Create Date: 2026-10-12

**这条 revision 本身就是 P3-01 的论证材料。**

它做的事是「给一张**已经存在**的表加两列」。旧实现里这件事写在应用启动的
``create_all()`` 里，而 ``create_all`` 只建缺失的表 —— 已存在的表它一眼都不看，
于是这个变更在生产上会**静默不生效**：服务照常启动，用户照常登录（因为代码读的是
不存在的列……或者更糟，读到的是默认值），问题要到运行时才暴露。
现在它是一次显式的、版本化的、会被 review 到的迁移。

两条约束的取舍：
  * ``failed_logins`` 给 NOT NULL + server_default '0'。存量行必须有值，
    否则「读出来是 NULL」和「读出来是 0」两条路径都得写，而 NULL 那条
    迟早有人忘。
  * ``locked_until`` 允许 NULL —— 没锁住和锁到某个时刻是两件事，
    用 NULL 表示「没锁」比另设一个布尔列干净。
    时间存 naive UTC，和 ``app/api/auth.py`` 里的 ``_utcnow()`` 对齐。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_login_lockout"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sys_user", sa.Column(
        "failed_logins", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sys_user", sa.Column("locked_until", sa.DateTime(), nullable=True))
    # server_default 只负责让存量行有值；应用侧是显式写 0/None 的，
    # 所以这层默认可以撤掉，避免将来加默认值时忘了这一列。
    op.alter_column("sys_user", "failed_logins", server_default=None)


def downgrade() -> None:
    op.drop_column("sys_user", "locked_until")
    op.drop_column("sys_user", "failed_logins")
