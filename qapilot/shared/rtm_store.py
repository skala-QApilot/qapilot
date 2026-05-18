"""RTM 버전 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_all_rtm_versions(service: dict) -> list[dict]:
    """RTM 버전 목록을 최신순으로 반환한다."""
    rtm_dir = _rtm_dir(service)
    if not rtm_dir.exists():
        return []
    versions = [_load(path) for path in rtm_dir.glob("*.json")]
    return sorted([v for v in versions if v], key=lambda v: v.get("created_at", ""), reverse=True)


def get_rtm_version_by_id(service: dict, rtm_version_id: str) -> dict | None:
    """rtm_version_id로 RTM 버전을 조회한다."""
    return _load(_rtm_dir(service) / f"{rtm_version_id}.json")


def create_rtm_version(
    service: dict,
    label: str,
    trace_id: str | None,
    requirements: list[dict],
) -> dict:
    """RTM 버전 snapshot을 생성한다."""
    version = {
        "rtm_version_id": str(uuid.uuid4()),
        "service_id": service.get("service_id"),
        "label": label,
        "trace_id": trace_id,
        "requirements": requirements,
        "summary": _summary(requirements),
        "created_at": _utc_now(),
    }
    _save(service, version)
    return version


def get_rtm_requirements(service: dict, rtm_version_id: str) -> list[dict] | None:
    """RTM 버전의 요구사항 목록을 반환한다."""
    version = get_rtm_version_by_id(service, rtm_version_id)
    return version.get("requirements", []) if version else None


def get_rtm_requirement_detail(
    service: dict,
    rtm_version_id: str,
    fr_id: str,
) -> dict | None:
    """RTM 버전에서 단일 요구사항을 반환한다."""
    requirements = get_rtm_requirements(service, rtm_version_id)
    if requirements is None:
        return None
    return next((item for item in requirements if item.get("fr_id") == fr_id), None)


def _rtm_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "rtm-versions"


def _save(service: dict, version: dict) -> None:
    path = _rtm_dir(service) / f"{version['rtm_version_id']}.json"
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


def _summary(requirements: list[dict]) -> dict:
    total = len(requirements)
    satisfied = len([r for r in requirements if r.get("status") == "충족"])
    unsatisfied = len([r for r in requirements if r.get("status") == "미충족"])
    unmeasured = len([r for r in requirements if r.get("status") == "미측정"])
    return {
        "total": total,
        "satisfied": satisfied,
        "unsatisfied": unsatisfied,
        "unmeasured": unmeasured,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
