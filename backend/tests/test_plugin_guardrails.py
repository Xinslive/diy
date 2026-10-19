import pytest
from sqlalchemy import text
from app.core.db import engine
from app.plugins.errors import PluginError
from app.plugins.manifest import table_prefix
from app.plugins.migrations import migrate, validate_tables
from app.plugins.security import verify_archive
from tests.plugin_fixtures import plugin_env
from tests.plugin_helpers import package


def test_constraints_and_trigger_reach_database_and_drift_refused(tmp_path, plugin_env, book):
    raw, manifest, keys, _ = package(tables=True)
    verified = verify_archive(raw, keys=keys)
    table = table_prefix(manifest['id'])+'items'
    function = table_prefix(manifest['id'])+'normalize'
    extra = f'''    op.create_check_constraint("label_nonempty", "{table}", "length(label) > 0")
    op.execute("CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.label := upper(NEW.label); RETURN NEW; END; $$")
    op.execute("CREATE TRIGGER normalize BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()")

'''
    key = 'migrations/versions/0001_items.py'
    verified.files[key] = verified.files[key].replace(b'def downgrade():', extra.encode()+b'def downgrade():')
    directory = tmp_path / 'guard'
    verified.extract(directory)
    _, metadata = migrate(engine, directory, manifest)
    assert metadata[0]['checks'] and metadata[0]['triggers']
    with engine.begin() as conn:
        assert conn.scalar(text(f'INSERT INTO "{table}" (book_id,label) VALUES (:book, :label) RETURNING label'), {'book':book.id,'label':'test'}) == 'TEST'
    with engine.begin() as conn:
        conn.execute(text(f'DROP TRIGGER normalize ON "{table}"'))
        conn.execute(text(f'DROP FUNCTION {function}()'))
    with engine.connect() as conn, pytest.raises(PluginError, match='漂移'):
        validate_tables(conn, manifest, expected=metadata)
