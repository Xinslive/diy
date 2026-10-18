import pytest
from sqlalchemy import inspect, text
from app.core.db import engine
from app.plugins.errors import PluginError
from app.plugins.manifest import version_table, table_prefix
from app.plugins.migrations import migrate, read_head, read_version, validate_tables
from app.plugins.security import verify_archive
from tests.plugin_helpers import package
from tests.plugin_fixtures import plugin_env


def test_independent_migration_and_live_metadata(tmp_path, plugin_env):
    raw, manifest, keys, _ = package(tables=True)
    path = tmp_path / "staging"
    verify_archive(raw, keys=keys).extract(path)
    with engine.connect() as conn:
        before = conn.scalar(text("SELECT version_num FROM alembic_version"))
    head, metadata = migrate(engine, path, manifest)
    assert head == "0001_items"
    assert metadata[0]["columns"][1]["name"] == "book_id"
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == before
        assert read_version(conn, manifest["id"]) == head
        assert validate_tables(conn, manifest, expected=metadata) == metadata
        assert inspect(conn).has_table(version_table(manifest["id"]))
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table_prefix(manifest["id"])}items" ADD COLUMN drift TEXT'))
    with engine.connect() as conn, pytest.raises(PluginError, match="漂移"):
        validate_tables(conn, manifest, expected=metadata)


def test_migration_failure_rolls_back_and_host_schema_unchanged(tmp_path, plugin_env):
    raw, manifest, keys, _ = package(tables=True)
    path = tmp_path / "staging"
    verified = verify_archive(raw, keys=keys)
    key = "migrations/versions/0001_items.py"
    verified.files[key] = verified.files[key].replace(b"def downgrade():", b'    raise ValueError("injected failure")\n\ndef downgrade():')
    verified.extract(path)
    with pytest.raises(ValueError, match="injected"):
        migrate(engine, path, manifest)
    with engine.connect() as conn:
        assert not inspect(conn).has_table(table_prefix(manifest["id"]) + "items")
        assert not inspect(conn).has_table(version_table(manifest["id"]))


@pytest.mark.parametrize('extra', [
    'op.add_column("book", sa.Column("injected", sa.Integer))',
    'op.create_table("outside_plugin", sa.Column("id", sa.Integer, primary_key=True))',
    'op.execute("CREATE SCHEMA injected_plugin")',
    'op.execute("CREATE VIEW injected_view AS SELECT 1 AS x")',
])
def test_migration_scope_violation_rolls_back(tmp_path, plugin_env, extra):
    raw, manifest, keys, _ = package(tables=True)
    verified = verify_archive(raw, keys=keys)
    script = 'migrations/versions/0001_items.py'
    verified.files[script] = verified.files[script].replace(b'def downgrade():', ('    '+extra+'\n\ndef downgrade():').encode())
    folder = tmp_path / 'scope'
    verified.extract(folder)
    with pytest.raises(PluginError, match='迁移修改了'):
        migrate(engine, folder, manifest)
    with engine.connect() as conn:
        assert not inspect(conn).has_table(table_prefix(manifest['id'])+'items')
        assert 'injected' not in {c['name'] for c in inspect(conn).get_columns('book')}


def test_book_scope_nullable_and_host_fk_are_rejected(tmp_path, plugin_env):
    raw, manifest, keys, _ = package(tables=True)
    verified = verify_archive(raw, keys=keys)
    path = tmp_path / 'bad_scope'
    script = 'migrations/versions/0001_items.py'
    verified.files[script] = verified.files[script].replace(b'sa.ForeignKey("book.id"), nullable=False', b'sa.ForeignKey("book.id"), nullable=True')
    verified.extract(path)
    with pytest.raises(PluginError, match='非空整数'):
        migrate(engine, path, manifest)
