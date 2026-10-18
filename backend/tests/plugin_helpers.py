"""临时签名包；测试密钥与生产配置隔离。"""
import base64
import hashlib
import io
import json
import stat
import uuid
import zipfile
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.plugins.manifest import canonical_payload, table_prefix, validate_manifest


def package(*, plugin_id=None, version="1.0.0", files=None, tables=False, private=None, edit=None):
    pid = plugin_id or "com.test.p" + uuid.uuid4().hex[:12]
    private = private or Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    prefix = table_prefix(pid)
    files = dict(files or {"backend/main.py": b'def register(registrar, ctx):\n    async def hello():\n        return {"hello": "plugin"}\n    registrar.get("/hello", hello)\n',
                           "frontend/index.js": b'export default (host) => ({render: () => host.h("div", "Signed plugin")});\n'})
    if tables:
        files["migrations/versions/0001_items.py"] = f'''from alembic import op
import sqlalchemy as sa
revision = "0001_items"
down_revision = None

def upgrade():
    op.create_table("{prefix}items", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False))

def downgrade():
    op.drop_table("{prefix}items")
'''.encode()
    manifest = {"format_version": 1, "id": pid, "name": "测试扩展", "version": version,
                "api_version": ">=1.0.0 <2.0.0", "permissions": ["plugin_data:rw"] if tables else [],
                "access": {"assignment": "admin_users", "default": "none"},
                "contributes": {"backend_entry": "backend/main.py", "frontend_entry": "frontend/index.js",
                                "menu": {"module": "extensions", "title": "测试扩展", "icon": "Grid", "route": f"/plugin/{pid}"}},
                "tables": [{"name": prefix + "items", "scope": "book"}] if tables else [],
                "files": [{"path": p, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for p, data in files.items()],
                "signature": {"algo": "ed25519", "key_id": "test", "value": ""}}
    if edit:
        edit(manifest)
    manifest = validate_manifest(manifest)
    manifest["signature"]["value"] = base64.b64encode(private.sign(canonical_payload(manifest))).decode()
    raw = archive(manifest, files)
    return raw, manifest, {"test": public}, private


def archive(manifest, files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for path, content in {"plugin.json": json.dumps(manifest).encode(), **files}.items():
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            out.writestr(info, content)
    return buffer.getvalue()
