"""시나리오 변경 요청 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_all_requests(
    service: dict,
    status: str | None = None,
    trigger: str | None = None,
) -> list[dict]:
    """변경 요청 목록을 최신순으로 반환한다."""
    requests_dir = _requests_dir(service)
    if not requests_dir.exists():
        return []
    requests = [_load(path) for path in requests_dir.glob("*.json")]
    items = [item for item in requests if item]
    if status:
        items = [item for item in items if item.get("status") == status]
    if trigger:
        items = [item for item in items if item.get("trigger") == trigger]
    return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)


def get_request_by_id(service: dict, request_id: str) -> dict | None:
    """request_id로 변경 요청을 조회한다."""
    return _load(_requests_dir(service) / f"{request_id}.json")


def create_request(service: dict, scenario_id: str, reason: str, trigger: str) -> dict:
    """변경 요청을 생성한다."""
    now = _utc_now()
    request = {
        "request_id": str(uuid.uuid4()),
        "scenario_id": scenario_id,
        "reason": reason,
        "trigger": trigger,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "reviewed_at": None,
        "reviewer": None,
    }
    _save(service, request)
    return request


def update_request_status(
    service: dict,
    request_id: str,
    status: str,
    reviewer: str | None = None,
) -> dict | None:
    """변경 요청 상태를 수정한다."""
    request = get_request_by_id(service, request_id)
    if not request:
        return None
    now = _utc_now()
    request["status"] = status
    request["reviewed_at"] = now
    request["reviewer"] = reviewer
    request["updated_at"] = now
    _save(service, request)
    return request


def _requests_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "change-requests"


def _save(service: dict, request: dict) -> None:
    path = _requests_dir(service) / f"{request['request_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")


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
