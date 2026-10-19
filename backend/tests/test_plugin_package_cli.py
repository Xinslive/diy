import importlib.util
import json
import shutil
from pathlib import Path
from app.plugins.security import verify_archive

spec = importlib.util.spec_from_file_location('cwq_plugin_packager', Path(__file__).parents[1] / 'scripts/plugin_package.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def test_documented_example_builds_with_full_signature(tmp_path):
    private = tmp_path / 'temporary.pem'
    keys = cli.generate_key(private, 'example')
    assert private.stat().st_mode & 0o777 == 0o600
    out = tmp_path / 'example.cwp'
    source = tmp_path / 'source'
    shutil.copytree(Path(__file__).parents[2] / 'docs/plugin-example', source)
    (source / '.DS_Store').write_bytes(b'Finder metadata')
    (source / 'frontend/.DS_Store').write_bytes(b'Finder metadata')
    result = cli.build(source, private, 'example', out)
    verified = verify_archive(out.read_bytes(), keys=keys)
    assert verified.digest == result['plugin_digest']
    assert verified.manifest['access']['default'] == 'none'
    assert any(p.endswith('.css') for p in verified.files)
    assert not any('pem' in p for p in verified.files)
    assert not any(Path(p).name == '.DS_Store' for p in verified.files)
