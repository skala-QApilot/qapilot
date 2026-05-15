"""증적 파일 조회 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
from pathlib import Path


def load_evidence_index(service: dict, trace_id: str) -> dict | None:
    """trace_id의 증적 index.json을 읽는다."""
    path = _evidence_dir(service, trace_id) / "index.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def load_tc_evidence(service: dict, trace_id: str, tc_id: str) -> dict | None:
    """TC 단위 증적 메타와 파일 경로를 반환한다."""
    index = load_evidence_index(service, trace_id)
    if not index:
        return None
    evidence = next(
        (item for item in index.get("tc_evidences", []) if item.get("tc_id") == tc_id),
        None,
    )
    if not evidence:
        return None
    base = _evidence_dir(service, trace_id) / tc_id
    files = evidence.get("files", [])
    return {
        "tc_id": tc_id,
        "files": files,
        "file_paths": [str(base / name) for name in files],
        "created_at": evidence.get("created_at"),
    }


def _evidence_dir(service: dict, trace_id: str) -> Path:
    return Path(str(service["qapilot_dir"])) / "evidence" / trace_id
