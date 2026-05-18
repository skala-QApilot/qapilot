"""파일 기반 Agent trace 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qapilot.shared.logger import get_logger

_logger = get_logger("trace_store")


def create_trace(
    service: dict,
    trace_id: str,
    command: str,
    trigger: str | None,
) -> dict:
    """실행 중 trace 파일을 생성한다."""
    trace = {
        "trace_id": trace_id,
        "command": command,
        "trigger": trigger,
        "status": "running",
        "started_at": _utc_now(),
        "completed_at": None,
        "error": None,
        "confidence": None,
        "agent_logs": [],
        "total_cost": 0.0,
        "result_summary": {},
    }
    _save_trace(service, trace_id, trace)
    return trace


def update_trace(service: dict, trace_id: str, state: dict) -> None:
    """파이프라인 완료 후 PipelineState 기반으로 trace를 갱신한다."""
    trace = load_trace(service, trace_id) or {}
    agent_logs = state.get("agent_logs", [])
    trace.update(
        {
            "status": state.get("status") or "completed",
            "completed_at": _utc_now(),
            "error": state.get("error"),
            "confidence": _average_confidence(agent_logs),
            "agent_logs": agent_logs,
            "total_cost": state.get("total_cost", 0.0),
            "result_summary": _result_summary(state),
        }
    )
    _save_trace(service, trace_id, trace)


def update_trace_failed(service: dict, trace_id: str, error: str) -> None:
    """파이프라인 실패 상태로 trace를 갱신한다."""
    trace = load_trace(service, trace_id) or {"trace_id": trace_id}
    trace.update(
        {
            "status": "failed",
            "completed_at": _utc_now(),
            "error": error,
        }
    )
    _save_trace(service, trace_id, trace)


def load_trace(service: dict, trace_id: str) -> dict | None:
    """trace 파일을 읽어 반환한다."""
    path = _trace_path(service, trace_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.error("trace_load_failed", path=str(path), error=str(e))
        return None
    return data if isinstance(data, dict) else None


def list_traces(service: dict) -> list[dict]:
    """서비스 trace 목록을 최신순으로 반환한다."""
    traces_dir = _traces_dir(service)
    if not traces_dir.exists():
        return []

    traces = []
    for path in traces_dir.glob("*.json"):
        trace = _load_trace_file(path)
        if trace:
            traces.append(trace)
    return sorted(traces, key=lambda item: item.get("started_at", ""), reverse=True)


def _save_trace(service: dict, trace_id: str, trace: dict) -> None:
    path = _trace_path(service, trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(trace, ensure_ascii=False, indent=2, default=str)
    path.write_text(content, encoding="utf-8")


def _load_trace_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _traces_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "traces"


def _trace_path(service: dict, trace_id: str) -> Path:
    return _traces_dir(service) / f"{trace_id}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result_summary(state: dict) -> dict:
    return {
        "scenarios_count": len(state.get("scenarios", [])),
        "action_mappings_count": len(state.get("action_mappings", [])),
        "generated_codes_count": len(state.get("generated_codes", [])),
        "ui_results_count": len(state.get("ui_results", [])),
        "has_mismatch": state.get("has_mismatch", False),
        "report_path": state.get("report_path"),
    }


def _average_confidence(agent_logs: list[dict]) -> float | None:
    values = [_as_float(log.get("confidence")) for log in agent_logs]
    confidences = [value for value in values if value is not None]
    if not confidences:
        return None
    return round(sum(confidences) / len(confidences), 4)


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
