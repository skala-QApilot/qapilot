"""리포트 파일 조회 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from qapilot.shared.trace_store import load_trace


def load_all_reports(service: dict) -> list[dict]:
    """저장된 리포트 목록을 최신순으로 반환한다."""
    reports_dir = _reports_dir(service)
    if not reports_dir.exists():
        return []
    reports = [_load(path) for path in reports_dir.glob("*.json")]
    return sorted([r for r in reports if r], key=lambda r: r.get("generated_at", ""), reverse=True)


def load_report(service: dict, trace_id: str) -> dict | None:
    """리포트를 읽고, 없으면 trace 기반 기본 리포트를 생성해 반환한다."""
    report = _load(_reports_dir(service) / f"{trace_id}.json")
    if report:
        return report
    trace = load_trace(service, trace_id)
    return _build_report_from_trace(trace) if trace else None


def export_report(service: dict, trace_id: str) -> str:
    """리포트를 JSON 문자열로 반환한다."""
    report = load_report(service, trace_id)
    return json.dumps(report, ensure_ascii=False, indent=2) if report else ""


def _reports_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "reports"


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _build_report_from_trace(trace: dict) -> dict:
    return {
        "trace_id": trace["trace_id"],
        "title": f"테스트 리포트 - {trace['trace_id'][:8]}",
        "generated_at": trace.get("completed_at") or _utc_now(),
        "summary": trace.get("result_summary", {}),
        "sections": [],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
