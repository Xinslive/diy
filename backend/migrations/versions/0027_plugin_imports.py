"""宿主插件源文件、预览批次与凭证来源。"""
from alembic import op

revision = "0027_plugin_imports"
down_revision = "0026_plugin_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_import_batch (
        id VARCHAR(32) PRIMARY KEY,
        plugin_id VARCHAR(96) NOT NULL REFERENCES plugin_registry(plugin_id) ON DELETE CASCADE,
        book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES sys_user(id) ON DELETE SET NULL, username VARCHAR(128) NOT NULL,
        plugin_version VARCHAR(64) NOT NULL, plugin_digest VARCHAR(64) NOT NULL,
        fingerprint VARCHAR(64) NOT NULL, status VARCHAR(24) NOT NULL DEFAULT 'UPLOADED',
        revision INTEGER NOT NULL DEFAULT 1, original_preview JSONB, preview JSONB,
        preview_digest VARCHAR(64) NOT NULL DEFAULT '', configuration JSONB NOT NULL DEFAULT '{}',
        configuration_digest VARCHAR(64) NOT NULL DEFAULT '', validation_errors JSONB NOT NULL DEFAULT '[]',
        confirmed_at TIMESTAMPTZ, submitted_at TIMESTAMPTZ, expires_at TIMESTAMPTZ NOT NULL,
        result JSONB NOT NULL DEFAULT '[]', request_id VARCHAR(128) NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_plugin_import_status CHECK (status IN ('UPLOADED','PREVIEW_READY','EDITING','CONFIRMED','SUBMITTING','SUBMITTED','FAILED','CANCELLED')),
        CONSTRAINT ck_plugin_import_revision CHECK (revision > 0))""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_plugin_import_fingerprint ON plugin_import_batch(plugin_id,book_id,fingerprint) WHERE status <> 'CANCELLED'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_plugin_import_owner ON plugin_import_batch(plugin_id,user_id,book_id,created_at)")
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_import_file (
        id VARCHAR(32) PRIMARY KEY, batch_id VARCHAR(32) NOT NULL REFERENCES plugin_import_batch(id) ON DELETE CASCADE,
        filename VARCHAR(256) NOT NULL, content_type VARCHAR(128) NOT NULL, size INTEGER NOT NULL,
        sha256 VARCHAR(64) NOT NULL, content BYTEA, expires_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT ck_plugin_import_file_size CHECK(size > 0))""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_plugin_import_file_batch_id ON plugin_import_file(batch_id)")
    op.execute("""CREATE TABLE IF NOT EXISTS plugin_voucher_origin (
        id SERIAL PRIMARY KEY, batch_id VARCHAR(32) NOT NULL REFERENCES plugin_import_batch(id) ON DELETE CASCADE,
        book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
        voucher_id INTEGER UNIQUE REFERENCES voucher(id) ON DELETE SET NULL,
        original_voucher_id INTEGER NOT NULL, voucher_no VARCHAR(64) NOT NULL, position INTEGER NOT NULL,
        CONSTRAINT uq_plugin_origin_position UNIQUE(batch_id,position))""")
    # 与其他宿主表保持相同的数据库更新时间戳语义。
    op.execute("DROP TRIGGER IF EXISTS trg_plugin_import_touch ON plugin_import_batch")
    op.execute("CREATE TRIGGER trg_plugin_import_touch BEFORE UPDATE ON plugin_import_batch FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()")


def downgrade():
    for name in ("plugin_voucher_origin", "plugin_import_file", "plugin_import_batch"):
        op.drop_table(name)
