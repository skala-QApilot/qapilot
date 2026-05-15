"""재테스트 그룹 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_all_retest_groups(service: dict) -> list[dict]:
    """재테스트 그룹 목록을 최신순으로 반환한다."""
    retest_dir = _retest_dir(service)
    if not retest_dir.exists():
        return []
    groups = [_load(path) for path in retest_dir.glob("*.json")]
    return sorted([g for g in groups if g], key=lambda g: g.get("created_at", ""), reverse=True)


def get_retest_group_by_id(service: dict, retest_group_id: str) -> dict | None:
    """retest_group_id로 재테스트 그룹을 조회한다."""
    return _load(_retest_dir(service) / f"{retest_group_id}.json")


def create_retest_group(
    service: dict,
    source_trace_id: str,
    failed_tc_ids: list[str],
) -> dict:
    """재테스트 그룹을 생성한다."""
    now = _utc_now()
    group = {
        "retest_group_id": str(uuid.uuid4()),
        "name": f"재테스트 그룹 {now[:10]}",
        "source_trace_id": source_trace_id,
        "failed_tc_ids": failed_tc_ids,
        "status": "pending",
        "created_at": now,
    }
    _save(service, group)
    return group


def _retest_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "retest-groups"


def _save(service: dict, group: dict) -> None:
    path = _retest_dir(service) / f"{group['retest_group_id']}.json"
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
