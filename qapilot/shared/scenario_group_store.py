"""시나리오 그룹 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_all_groups(service: dict) -> list[dict]:
    """시나리오 그룹 목록을 최신순으로 반환한다."""
    groups_dir = _groups_dir(service)
    if not groups_dir.exists():
        return []
    groups = [_load(path) for path in groups_dir.glob("*.json")]
    return sorted([g for g in groups if g], key=lambda g: g.get("created_at", ""), reverse=True)


def get_group_by_id(service: dict, group_id: str) -> dict | None:
    """group_id로 그룹을 조회한다."""
    return _load(_groups_dir(service) / f"{group_id}.json")


def create_group(service: dict, name: str, scenario_ids: list[str], tc_ids: list[str]) -> dict:
    """시나리오 그룹을 생성한다."""
    now = _utc_now()
    group = {
        "group_id": str(uuid.uuid4()),
        "name": name,
        "scenario_ids": scenario_ids,
        "tc_ids": tc_ids,
        "schedule": None,
        "created_at": now,
        "updated_at": now,
    }
    _save(service, group)
    return group


def update_group(service: dict, group_id: str, name: str | None) -> dict | None:
    """그룹 이름을 수정한다."""
    group = get_group_by_id(service, group_id)
    if not group:
        return None
    if name is not None:
        group["name"] = name
    group["updated_at"] = _utc_now()
    _save(service, group)
    return group


def delete_group(service: dict, group_id: str) -> bool:
    """그룹을 삭제한다."""
    path = _groups_dir(service) / f"{group_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def set_schedule(
    service: dict,
    group_id: str,
    cron: str,
    timezone: str,
    enabled: bool,
) -> dict | None:
    """그룹 스케줄을 설정한다."""
    group = get_group_by_id(service, group_id)
    if not group:
        return None
    group["schedule"] = {
        "cron": cron,
        "timezone": timezone,
        "enabled": enabled,
        "created_at": _utc_now(),
    }
    group["updated_at"] = _utc_now()
    _save(service, group)
    return group


def _groups_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "scenario-groups"


def _save(service: dict, group: dict) -> None:
    path = _groups_dir(service) / f"{group['group_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(group, ensure_ascii=False, indent=2), encoding="utf-8")


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
