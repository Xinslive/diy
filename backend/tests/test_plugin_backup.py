import copy
import gzip
import json
from pathlib import Path
import pytest
from sqlalchemy import text, select, tuple_
from app.models.base import Base
from app.backup.export import export_scope, read_snapshot, restore, ExportRefused, SCOPE_BOOK
from app.core.db import SessionLocal, engine
from app.plugins.installer import install
from app.plugins.manifest import table_prefix
from app.plugins.models import PluginRegistry
from tests.plugin_fixtures import plugin_env


def snapshot(plugin_env, admin_user, book, tmp_path):
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    install(raw, admin_user)
    name = table_prefix(manifest['id']) + 'items'
    with engine.begin() as conn:
        conn.execute(text(f'INSERT INTO "{name}" (book_id,label) VALUES (:book,:label)'), {'book': book.id, 'label': '保留的插件账套数据'})
    path = tmp_path / 'book.json.gz'
    with SessionLocal() as db:
        export_scope(db, scope=SCOPE_BOOK, book_id=book.id, book_label=book.name, out_path=path)
    return path, manifest, name


def test_v2_real_roundtrip_and_live_metadata_drift(plugin_env, admin_user, book, tmp_path):
    path, manifest, name = snapshot(plugin_env, admin_user, book, tmp_path)
    data = read_snapshot(path)
    assert data['header']['version'] == 2
    assert data['header']['plugins'][0]['id'] == manifest['id']
    assert data['tables'][name]['rows'][0]['label'] == '保留的插件账套数据'
    # 删除本账套归档中的全部行，再从完整 v2 快照恢复（其他账套保持不变）。
    with SessionLocal.begin() as db:
        db.execute(text(f'DELETE FROM "{name}"'))
        for table in reversed(Base.metadata.sorted_tables):
            payload = data["tables"].get(table.name)
            if not payload or not payload["rows"]:
                continue
            pk = list(table.primary_key.columns)
            values = [tuple(r[c.name] for c in pk) for r in payload["rows"]]
            db.execute(table.delete().where(tuple_(*pk).in_(values)))
    with SessionLocal.begin() as db:
        counts = restore(db, path)
        assert counts[name] == 1
    with engine.connect() as conn:
        assert conn.scalar(text(f'SELECT label FROM "{name}"')) == '保留的插件账套数据'
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{name}" ADD COLUMN drift TEXT'))
    with SessionLocal() as db, pytest.raises(ExportRefused):
        export_scope(db, scope=SCOPE_BOOK, book_id=book.id, book_label=book.name, out_path=tmp_path / 'drift.gz')


@pytest.mark.parametrize('mutation', ['digest', 'version', 'migration_head', 'unknown', 'metadata', 'scope', 'missing_table', 'v1'])
def test_restore_rejects_incompatible_plugins_before_any_write(plugin_env, admin_user, book, tmp_path, mutation):
    path, manifest, name = snapshot(plugin_env, admin_user, book, tmp_path)
    data = read_snapshot(path)
    if mutation in {'digest', 'version', 'migration_head'}:
        data['header']['plugins'][0][mutation] = 'wrong'
    elif mutation == 'unknown':
        data['header']['plugins'][0]['id'] = 'com.unknown.plugin'
    elif mutation == 'metadata':
        data['header']['plugins'][0]['tables'][0]['columns'][0]['nullable'] = True
    elif mutation == 'scope':
        data['tables'][name]['rows'][0]['book_id'] += 1
    elif mutation == 'missing_table':
        del data['tables'][name]
    else:
        data['header']['version'] = 1
    broken = tmp_path / 'broken.gz'
    with gzip.open(broken, 'wt', encoding='utf-8') as out:
        json.dump(data, out)
    with SessionLocal() as db, pytest.raises(ExportRefused):
        restore(db, broken)
    with engine.connect() as conn:
        assert conn.scalar(text(f'SELECT count(*) FROM "{name}"')) == 1


@pytest.mark.parametrize("missing", ["code", "installation"])
def test_restore_requires_matching_installed_code(plugin_env, admin_user, book, tmp_path, missing):
    from app.core.config import settings
    path, manifest, name = snapshot(plugin_env, admin_user, book, tmp_path)
    with SessionLocal.begin() as db:
        p = db.get(PluginRegistry, manifest["id"])
        if missing == "installation":
            from datetime import datetime, timezone
            p.deleted_at = datetime.now(timezone.utc)
        else:
            (Path(settings.plugin_root) / p.plugin_id / p.plugin_digest / "backend/main.py").unlink()
    with SessionLocal() as db, pytest.raises(ExportRefused, match="插件"):
        restore(db, path)
    with engine.connect() as conn:
        assert conn.scalar(text(f'SELECT count(*) FROM "{name}"')) == 1


def test_retained_backup_validation_never_allows_writes(plugin_env, admin_user, book, tmp_path):
    path, _, _ = snapshot(plugin_env, admin_user, book, tmp_path)
    with SessionLocal() as db, pytest.raises(ValueError, match="不能用于写入"):
        restore(db, path, _retained_backup_validation=True)


def test_global_reference_and_composite_book_fk_closure(plugin_env, admin_user, book, tmp_path):
    from app.plugins.security import verify_archive
    build, _ = plugin_env
    raw, manifest, _, _ = build(tables=True)
    pid, prefix = manifest['id'], table_prefix(manifest['id'])
    files = dict(verify_archive(raw).files)
    category, parent, child = [prefix + name for name in ['category', 'items', 'child']]
    files['migrations/versions/0001_items.py'] = f'''from alembic import op
import sqlalchemy as sa
revision = "0001_items"
down_revision = None
def upgrade():
    op.create_table("{category}", sa.Column("id", sa.Integer, primary_key=True), sa.Column("label", sa.String(32), nullable=False))
    op.create_table("{parent}", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), nullable=False),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("{category}.id"), nullable=False),
        sa.UniqueConstraint("book_id", "id"))
    op.create_table("{child}", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), nullable=False),
        sa.Column("parent_id", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["book_id", "parent_id"], ["{parent}.book_id", "{parent}.id"]))
'''.encode()
    def metadata(doc):
        doc['permissions'] = ['plugin_data:rw']
        doc['tables'] = [{'name': category, 'scope': 'global', 'purpose': 'reference'},
                         {'name': parent, 'scope': 'book'}, {'name': child, 'scope': 'book'}]
    signed, _, _, _ = build(plugin_id=pid, files=files, edit=metadata)
    install(signed, admin_user)
    with engine.begin() as conn:
        conn.execute(text(f'INSERT INTO "{category}" VALUES (1, :label)'), {'label': '参考配置'})
        conn.execute(text(f'INSERT INTO "{parent}" VALUES (1, :id, 1)'), {'id': book.id})
        conn.execute(text(f'INSERT INTO "{child}" VALUES (1, :id, 1)'), {'id': book.id})
    out = tmp_path / 'closure.gz'
    with SessionLocal() as db:
        export_scope(db, scope=SCOPE_BOOK, book_id=book.id, book_label=book.name, out_path=out)
        assert restore(db, out, dry_run=True)[category] == 1
    original = read_snapshot(out)
    for missing in [category, parent]:
        broken = copy.deepcopy(original)
        broken['tables'][missing]['rows'] = []
        bad = tmp_path / f'missing-{missing}.gz'
        with gzip.open(bad, 'wt', encoding='utf-8') as handle:
            json.dump(broken, handle)
        with SessionLocal() as db, pytest.raises(ExportRefused, match='外键缺少父行'):
            restore(db, bad)
    with engine.connect() as conn:
        assert conn.scalar(text(f'SELECT count(*) FROM "{child}"')) == 1
