"""시나리오 버전 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qapilot.shared.scenario_store import load_all_scenarios


def load_all_versions(service: dict) -> list[dict]:
    """시나리오 버전 목록을 최신순으로 반환한다."""
    versions_dir = _versions_dir(service)
    if not versions_dir.exists():
        return []
    versions = [_load(path) for path in versions_dir.glob("*.json")]
    return sorted([v for v in versions if v], key=lambda v: v.get("created_at", ""), reverse=True)


def get_version_by_id(service: dict, version_id: str) -> dict | None:
    """version_id로 시나리오 버전을 조회한다."""
    return _load(_versions_dir(service) / f"{version_id}.json")


def create_version(
    service: dict,
    label: str,
    description: str,
    scenarios_snapshot: list[dict],
) -> dict:
    """현재 시나리오 snapshot을 버전으로 저장한다."""
    version = {
        "version_id": str(uuid.uuid4()),
        "service_id": service.get("service_id"),
        "label": label,
        "description": description,
        "scenarios_snapshot": scenarios_snapshot,
        "is_favorite": False,
        "created_at": _utc_now(),
    }
    _save(service, version)
    return version


def update_version(
    service: dict,
    version_id: str,
    is_favorite: bool | None,
    label: str | None,
) -> dict | None:
    """버전의 즐겨찾기와 라벨을 수정한다."""
    version = get_version_by_id(service, version_id)
    if not version:
        return None
    if is_favorite is not None:
        version["is_favorite"] = is_favorite
    if label is not None:
        version["label"] = label
    _save(service, version)
    return version


def delete_version(service: dict, version_id: str) -> bool:
    """버전을 삭제한다."""
    path = _versions_dir(service) / f"{version_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def get_version_diff(service: dict, version_id: str) -> dict:
    """해당 버전과 직전 버전의 시나리오 diff를 반환한다."""
    versions = list(reversed(load_all_versions(service)))
    current_index = next((i for i, v in enumerate(versions) if v["version_id"] == version_id), None)
    if current_index is None:
        return {"added": [], "removed": [], "modified": []}
    current = versions[current_index]
    previous = versions[current_index - 1] if current_index > 0 else None
    return _snapshot_diff(previous, current)


def create_current_version(service: dict, label: str, description: str) -> dict:
    """현재 저장된 시나리오 전체를 snapshot으로 버전 생성한다."""
    return create_version(service, label, description, load_all_scenarios(service))


def _versions_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "scenario-versions"


def _save(service: dict, version: dict) -> None:
    path = _versions_dir(service) / f"{version['version_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8")


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _snapshot_diff(previous: dict | None, current: dict) -> dict:
    current_map = {s.get("ts_id"): s for s in current.get("scenarios_snapshot", [])}
    if not previous:
        return {"added": list(current_map), "removed": [], "modified": []}
    previous_map = {s.get("ts_id"): s for s in previous.get("scenarios_snapshot", [])}
    added = [key for key in current_map if key not in previous_map]
    removed = [key for key in previous_map if key not in current_map]
    modified = [key for key in current_map if key in previous_map and current_map[key] != previous_map[key]]
    return {"added": added, "removed": removed, "modified": modified}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
