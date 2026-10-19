"""应用级计划任务与持久运行记录。"""
from alembic import op

revision = "0028_scheduled_tasks"
down_revision = "0027_plugin_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS sys_scheduled_task (
        task_key VARCHAR(200) PRIMARY KEY, owner_type VARCHAR(16) NOT NULL, plugin_id VARCHAR(96),
        task_name VARCHAR(64) NOT NULL, plugin_digest VARCHAR(64) NOT NULL DEFAULT '', schedule JSONB NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        next_run_at TIMESTAMPTZ NOT NULL, last_run_at TIMESTAMPTZ, lease_token VARCHAR(32) NOT NULL DEFAULT '',
        lease_until TIMESTAMPTZ, current_run_id VARCHAR(32), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_scheduled_task_owner CHECK (owner_type IN ('SYSTEM', 'PLUGIN'))
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_scheduled_task_due ON sys_scheduled_task (enabled, next_run_at, lease_until)")
    op.execute("DROP TRIGGER IF EXISTS trg_scheduled_task_touch ON sys_scheduled_task")
    op.execute("CREATE TRIGGER trg_scheduled_task_touch BEFORE UPDATE ON sys_scheduled_task FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()")
    op.execute("""CREATE TABLE IF NOT EXISTS sys_scheduled_task_run (
        run_id VARCHAR(32) PRIMARY KEY, task_key VARCHAR(200) NOT NULL, owner_type VARCHAR(16) NOT NULL,
        plugin_id VARCHAR(96), task_name VARCHAR(64) NOT NULL, plugin_digest VARCHAR(64) NOT NULL DEFAULT '',
        scheduled_for TIMESTAMPTZ NOT NULL, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
        status VARCHAR(16) NOT NULL, claim_count INTEGER NOT NULL DEFAULT 1, error_code VARCHAR(64) NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '', user_results JSONB NOT NULL DEFAULT '[]',
        CONSTRAINT ck_scheduled_task_run_owner CHECK (owner_type IN ('SYSTEM', 'PLUGIN')),
        CONSTRAINT ck_scheduled_task_run_status CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED')),
        CONSTRAINT uq_scheduled_task_run_occurrence UNIQUE (task_key, scheduled_for)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_scheduled_task_run_recent ON sys_scheduled_task_run (task_key, scheduled_for)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_scheduled_task_run_finished ON sys_scheduled_task_run (finished_at)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS sys_scheduled_task_run")
    op.execute("DROP TRIGGER IF EXISTS trg_scheduled_task_touch ON sys_scheduled_task")
    op.execute("DROP TABLE IF EXISTS sys_scheduled_task")
