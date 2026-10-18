import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('cwq_plugin_supervisor', Path(__file__).parents[1] / 'scripts/start_server.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_supervisor_creates_one_fresh_shared_generation(monkeypatch):
    monkeypatch.setenv('CWQ_PLUGIN_GENERATION', 'inherited-old-value')
    first = launcher.restart_environment(2)
    second = launcher.restart_environment(2)
    assert len(first['CWQ_PLUGIN_GENERATION']) == 64
    assert first['CWQ_PLUGIN_GENERATION'] != second['CWQ_PLUGIN_GENERATION']
    assert int(second['CWQ_PLUGIN_STARTED_AT']) > int(first['CWQ_PLUGIN_STARTED_AT'])
    assert first['CWQ_PLUGIN_EXPECTED_WORKERS'] == '2'
    assert '--workers' in launcher.launch_command('uvicorn', host='127.0.0.1', port=8000, workers=2)
    assert 'gunicorn' in launcher.launch_command('gunicorn', host='0.0.0.0', port=8000, workers=2)
    for count in (0, -1, 65):
        with pytest.raises(ValueError):
            launcher.launch_command('uvicorn', host='localhost', port=8000, workers=count)
