"""插件宿主注册表；不执行插件迁移，不修改已有主迁移。"""
from alembic import op
import sqlalchemy as sa

revision = "0026_plugin_registry"
down_revision = "0025_backup_run"
branch_labels = None
depends_on = None


def upgrade():
    # 显式冻结本次迁移结构，不能 import 随应用演进的 ORM 模型。
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_registry (
        plugin_id VARCHAR(96) PRIMARY KEY, version VARCHAR(64) NOT NULL,
        plugin_digest VARCHAR(64) NOT NULL, manifest_version INTEGER NOT NULL DEFAULT 1,
        desired_enabled BOOLEAN NOT NULL DEFAULT true, desired_revision VARCHAR(32) NOT NULL,
        access_default VARCHAR(16) NOT NULL DEFAULT 'none' CHECK (access_default = 'none'),
        runtime_status VARCHAR(32) NOT NULL DEFAULT 'PENDING_RESTART', generation VARCHAR(64) NOT NULL DEFAULT '',
        error_code VARCHAR(64) NOT NULL DEFAULT '', error_message VARCHAR(256) NOT NULL DEFAULT '',
        installed_at TIMESTAMPTZ NOT NULL DEFAULT now(), installed_by INTEGER REFERENCES sys_user(id) ON DELETE SET NULL,
        migration_head VARCHAR(64) NOT NULL DEFAULT '', tables JSONB NOT NULL DEFAULT '[]',
        manifest_metadata JSONB NOT NULL DEFAULT '{}', archive_digest VARCHAR(64) NOT NULL DEFAULT '',
        pending_attempt_id VARCHAR(32), backup_path TEXT NOT NULL DEFAULT '', deleted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_install_attempt (
        attempt_id VARCHAR(32) PRIMARY KEY, plugin_id VARCHAR(96) NOT NULL, operation VARCHAR(16) NOT NULL,
        old_digest VARCHAR(64) NOT NULL DEFAULT '', new_digest VARCHAR(64) NOT NULL,
        staging_path TEXT NOT NULL, active_path TEXT NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'PREPARED',
        error_code VARCHAR(64) NOT NULL DEFAULT '', migration_head VARCHAR(64) NOT NULL DEFAULT '',
        manifest_metadata JSONB NOT NULL, tables JSONB NOT NULL DEFAULT '[]', archive_digest VARCHAR(64) NOT NULL DEFAULT '',
        actor_id INTEGER REFERENCES sys_user(id) ON DELETE SET NULL, backup_path TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_plugin_install_attempt_plugin_id ON plugin_install_attempt(plugin_id)")
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_user_access (
        plugin_id VARCHAR(96) REFERENCES plugin_registry(plugin_id), user_id INTEGER REFERENCES sys_user(id) ON DELETE CASCADE,
        grant_id VARCHAR(32) NOT NULL UNIQUE, granted_by INTEGER REFERENCES sys_user(id) ON DELETE SET NULL,
        granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), revoked_at TIMESTAMPTZ, PRIMARY KEY(plugin_id,user_id))""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_plugin_access_user ON plugin_user_access(user_id,plugin_id,revoked_at)")
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_cohort (
        id INTEGER PRIMARY KEY CHECK(id=1), generation VARCHAR(64) NOT NULL,
        started_at BIGINT NOT NULL, expected_workers INTEGER NOT NULL CHECK(expected_workers BETWEEN 1 AND 64))""")
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_worker_runtime (
        generation VARCHAR(64) NOT NULL, worker_id VARCHAR(64) NOT NULL, plugin_id VARCHAR(96) NOT NULL,
        plugin_digest VARCHAR(64) NOT NULL DEFAULT '', desired_revision VARCHAR(32) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL, error_code VARCHAR(64) NOT NULL DEFAULT '', lease_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(generation,worker_id,plugin_id))""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_plugin_worker_lease ON plugin_worker_runtime(plugin_id,lease_at)")


def downgrade():
    for name in ("plugin_worker_runtime", "plugin_cohort", "plugin_user_access", "plugin_install_attempt", "plugin_registry"):
        op.drop_table(name)
