import json
from pathlib import Path

from qapilot.shared.project_registry import ProjectRecord, ProjectRegistry, build_project_summary


def _project_payload(tmp_path: Path) -> ProjectRecord:
    return ProjectRecord(
        project_slug='sample-app',
        display_name='Sample App',
        local_path=str(tmp_path),
        config_path=str(tmp_path / 'qapilot.config.yaml'),
        index_path=str(tmp_path / '.qapilot' / 'codebase-index'),
        framework='fastapi',
        language='python',
        created_at='2026-05-15T00:00:00Z',
        updated_at='2026-05-15T00:00:00Z',
    )


def test_project_registry_upsert_preserves_created_at(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = ProjectRegistry()
    first, created = registry.upsert(_project_payload(tmp_path))
    assert created is True

    second_input = first.model_copy(update={'display_name': 'Sample App Renamed'})
    second, created_again = registry.upsert(second_input)
    assert created_again is False
    assert second.display_name == 'Sample App Renamed'
    assert second.created_at == first.created_at
    assert second.updated_at != first.updated_at

    registry_file = tmp_path / '.qapilot' / 'projects' / 'registry.json'
    data = json.loads(registry_file.read_text(encoding='utf-8'))
    assert data['sample-app']['display_name'] == 'Sample App Renamed'


def test_build_project_summary_reads_codebase_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index_dir = tmp_path / '.qapilot' / 'codebase-index'
    index_dir.mkdir(parents=True)
    (index_dir / 'manifest.json').write_text(
        json.dumps({
            'scan_timestamp': '2026-05-15T12:34:56Z',
            'file_count': 9,
            'endpoint_count': 2,
        }),
        encoding='utf-8',
    )
    (index_dir / 'endpoints.json').write_text(
        json.dumps([
            {'path': '/api/users', 'method': 'GET', 'handler': 'list_users', 'file': 'app.py'},
            {'path': '/api/users', 'method': 'POST', 'handler': 'create_user', 'file': 'app.py'},
        ]),
        encoding='utf-8',
    )
    (index_dir / 'models.json').write_text(json.dumps([{'name': 'User'}, {'name': 'Role'}]), encoding='utf-8')
    (index_dir / 'callgraph.json').write_text(json.dumps({}), encoding='utf-8')

    summary = build_project_summary(_project_payload(tmp_path))
    assert summary['file_count'] == 9
    assert summary['endpoint_count'] == 2
    assert summary['model_count'] == 2
    assert summary['last_scanned_at'] == '2026-05-15T12:34:56Z'
    assert summary['endpoints'][0]['path'] == '/api/users'
