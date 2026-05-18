import json
from pathlib import Path

from fastapi.testclient import TestClient

from qapilot.api.main import app


def _prepare_index(tmp_path: Path) -> None:
    index_dir = tmp_path / '.qapilot' / 'codebase-index'
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / 'manifest.json').write_text(
        json.dumps({
            'scan_timestamp': '2026-05-15T12:34:56Z',
            'file_count': 12,
            'endpoint_count': 3,
        }),
        encoding='utf-8',
    )
    (index_dir / 'endpoints.json').write_text(
        json.dumps([
            {'path': '/health', 'method': 'GET', 'handler': 'health', 'file': 'app.py'},
            {'path': '/login', 'method': 'POST', 'handler': 'login', 'file': 'auth.py'},
            {'path': '/users', 'method': 'GET', 'handler': 'list_users', 'file': 'users.py'},
        ]),
        encoding='utf-8',
    )
    (index_dir / 'models.json').write_text(json.dumps([{'name': 'User'}]), encoding='utf-8')
    (index_dir / 'callgraph.json').write_text(json.dumps({}), encoding='utf-8')


def test_register_and_get_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _prepare_index(tmp_path)

    client = TestClient(app)
    payload = {
        'project_slug': 'sample-app',
        'display_name': 'Sample App',
        'local_path': str(tmp_path),
        'config_path': str(tmp_path / 'qapilot.config.yaml'),
        'index_path': str(tmp_path / '.qapilot' / 'codebase-index'),
        'framework': 'fastapi',
        'language': 'python',
    }

    register = client.post('/api/projects/register', json=payload)
    assert register.status_code == 200
    assert register.json()['project_slug'] == 'sample-app'
    assert register.json()['dashboard_url'].endswith('/sample-app')
    assert register.json()['status'] == 'created'

    registry_file = tmp_path / '.qapilot' / 'projects' / 'registry.json'
    assert registry_file.exists()

    detail = client.get('/api/projects/sample-app')
    assert detail.status_code == 200
    body = detail.json()
    assert body['project']['project_slug'] == 'sample-app'
    assert body['project']['framework'] == 'fastapi'
    assert body['summary']['file_count'] == 12
    assert body['summary']['endpoint_count'] == 3
    assert body['summary']['model_count'] == 1
    assert body['summary']['endpoints'][0]['path'] == '/health'


def test_register_project_overwrites_existing_slug(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)
    payload = {
        'project_slug': 'sample-app',
        'display_name': 'Sample App',
        'local_path': str(tmp_path),
        'config_path': str(tmp_path / 'qapilot.config.yaml'),
        'index_path': str(tmp_path / '.qapilot' / 'codebase-index'),
        'framework': 'fastapi',
        'language': 'python',
    }

    first = client.post('/api/projects/register', json=payload)
    second = client.post('/api/projects/register', json={**payload, 'display_name': 'Renamed App'})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()['status'] == 'updated'
