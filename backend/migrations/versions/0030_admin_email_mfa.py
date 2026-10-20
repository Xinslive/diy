"""管理员二次验证（邮箱链接）：新增 admin_mfa_challenge 表

Revision ID: 0030_admin_email_mfa
Revises: 0029_progressive_lockout
Create Date: 2026-10-20

**这张表解决什么问题。**

审查报告 P0-1：「管理员账号无任何二次验证」。管理员口令一旦泄露，持码者立刻
获得「开号 / 改口令 / 装插件 / 读 OSS 凭据 / 轮换调度密钥」的全套权限 ——
这些权限恰好是普通会计永远拿不到的。二次验证把「拿到口令」与「拿到会话」
拆成两件事。

**形态是邮箱链接而不是 TOTP。** 本项目已经有一套跑通的 SMTP 通知通道
（``app/notify/mailer.py``，纯标准库），复用它等于零新增依赖、零新增运维面。
TOTP 要密钥保管、恢复码、时钟漂移容差与装机引导 —— 那是另一个产品。

**表里为什么有两份额外的东西。**

一次邮箱链接式登录有两个参与者：发起登录的浏览器（要知道「有人点了吗」）与
收信的邮箱（点开即确认）。邮件链接会被网关扫描、被手机锁屏预览、被 IT 留档
—— 所以**它绝不能直接就是会话令牌**。于是：

* ``email_token_hash``：邮件链接里那串东西的散列。点链接只把挑战标成
  ``VERIFIED``，拿不到任何会话；
* ``client_secret_hash``：只发给**发起登录的那个客户端**的散列。客户端拿
  ``challenge_id + client_secret`` 轮询，被验证后才换到真正的访问令牌。

这样「邮件链接被第三方点开」的最坏后果只是浪费掉一次挑战，而**不是**第三方
拿到了管理员会话。

**幂等。** 建表与建索引都用 ``IF NOT EXISTS``，运维手工重跑不会炸。

**不重放 db_init.sql。** 按 0005 立的规矩，``db_init.sql`` 只承担「触发器与
约束」那一层；新表由本 revision 自己建，与模型
``app/models/mfa.py::AdminMfaChallenge`` 逐列对齐。
"""
from __future__ import annotations

from alembic import op

revision = "0030_admin_email_mfa"
down_revision = "0029_progressive_lockout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS admin_mfa_challenge (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES sys_user(id) ON DELETE CASCADE,
            email_token_hash VARCHAR(64) NOT NULL,
            client_secret_hash VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            expires_at TIMESTAMPTZ NOT NULL,
            last_sent_at TIMESTAMPTZ,
            verified_at TIMESTAMPTZ,
            consumed_at TIMESTAMPTZ,
            ip VARCHAR(45) NOT NULL DEFAULT '',
            user_agent VARCHAR(256) NOT NULL DEFAULT '',
            reason VARCHAR(32) NOT NULL DEFAULT 'login',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_admin_mfa_challenge_email_token UNIQUE (email_token_hash),
            CONSTRAINT ck_admin_mfa_challenge_status
                CHECK (status IN ('PENDING', 'VERIFIED', 'CONSUMED', 'EXPIRED'))
        )
    """)
    # 清理入口按「未完成且已过期」扫。没有索引就是每次清理一次全表扫，
    # 而清理会挂在登录路径上顺带做。
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_mfa_challenge_pending "
               "ON admin_mfa_challenge (status, expires_at)")
    # 「这个账号当前有没有未完成的挑战」—— 发起新挑战时要把旧的作废。
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_mfa_challenge_user "
               "ON admin_mfa_challenge (user_id)")
    # 与 0028 的 scheduled_task 一样挂一个「UPDATE 时刷新 updated_at」的触发器，
    # 让库侧改写的行与 ORM 改写的行时间戳语义一致（见 app/models/base.py）。
    op.execute("DROP TRIGGER IF EXISTS trg_admin_mfa_challenge_touch "
               "ON admin_mfa_challenge")
    op.execute("CREATE TRIGGER trg_admin_mfa_challenge_touch "
               "BEFORE UPDATE ON admin_mfa_challenge "
               "FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_admin_mfa_challenge_touch "
               "ON admin_mfa_challenge")
    op.execute("DROP INDEX IF EXISTS ix_admin_mfa_challenge_user")
    op.execute("DROP INDEX IF EXISTS ix_admin_mfa_challenge_pending")
    op.execute("DROP TABLE IF EXISTS admin_mfa_challenge")
