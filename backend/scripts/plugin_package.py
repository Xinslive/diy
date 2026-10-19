#!/usr/bin/env python3
"""离线生成 Ed25519 密钥、签名 .cwp 或校验包；私钥不进入插件包。"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.plugins.manifest import canonical_payload, safe_path, validate_manifest
from app.plugins.security import verify_archive


def generate_key(path: Path, key_id: str):
    private = Ed25519PrivateKey.generate()
    encoded = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(encoded)
    public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return {key_id: public}


def build(source: Path, private_path: Path, key_id: str, output: Path):
    source = source.resolve()
    private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError('私钥必须为 Ed25519')
    files = {}
    for path in sorted(source.rglob('*')):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError('源目录不允许链接或特殊文件')
        # Finder metadata is not a plugin asset; keep it outside the signed file set.
        if path.is_file() and path.name == '.DS_Store':
            continue
        if path.is_file() and path.name != 'plugin.json':
            if path.resolve() in {private_path.resolve(), output.resolve()}:
                raise ValueError('私钥/输出包不能位于源目录中')
            files[safe_path(path.relative_to(source).as_posix())] = path.read_bytes()
    manifest = json.loads((source / 'plugin.json').read_text(encoding='utf-8'))
    manifest['files'] = [{'path': p, 'size': len(v), 'sha256': hashlib.sha256(v).hexdigest()} for p, v in files.items()]
    manifest['signature'] = {'algo': 'ed25519', 'key_id': key_id, 'value': ''}
    manifest = validate_manifest(manifest)
    manifest['signature']['value'] = base64.b64encode(private.sign(canonical_payload(manifest))).decode()
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in {'plugin.json': json.dumps(manifest, ensure_ascii=False).encode(), **files}.items():
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    keys = {key_id: base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()}
    verified = verify_archive(buffer.getvalue(), keys=keys)
    with output.open('xb') as handle:
        handle.write(buffer.getvalue())
    output.chmod(0o600)
    return {'id': manifest['id'], 'version': manifest['version'], 'plugin_digest': verified.digest, 'files': len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    key = sub.add_parser('keygen')
    key.add_argument('--private', required=True, type=Path)
    key.add_argument('--key-id', required=True)
    pack = sub.add_parser('build')
    pack.add_argument('source', type=Path)
    pack.add_argument('--private', required=True, type=Path)
    pack.add_argument('--key-id', required=True)
    pack.add_argument('--output', required=True, type=Path)
    check = sub.add_parser('verify')
    check.add_argument('package', type=Path)
    check.add_argument('--public-keys', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'keygen':
        result = generate_key(args.private, args.key_id)
    elif args.command == 'build':
        result = build(args.source, args.private, args.key_id, args.output)
    else:
        verified = verify_archive(args.package.read_bytes(), keys=json.loads(args.public_keys.read_text()))
        result = {'id': verified.manifest['id'], 'plugin_digest': verified.digest, 'files': len(verified.files)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
