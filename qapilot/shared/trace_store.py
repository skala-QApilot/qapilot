"""DB 기반 Agent trace 저장소.

.qapilot/ 파일 시스템 의존성을 제거하고 runs 테이블을 source of truth 로 사용한다.

Author: C
Created: 2026-05-15 / Refactored: 2026-06-04
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from qapilot.db.run_reader import load_run, load_runs_by_service
from qapilot.db.run_writer import upsert_run
from qapilot.messaging.redis_pubsub import publish_run_event
from qapilot.shared import progress
from qapilot.shared.logger import get_logger

_logger = get_logger("trace_store")
_KST = ZoneInfo("Asia/Seoul")


def create_trace(
    trace_id: str,
    command: str,
    trigger: str | None,
    service_id: str | None = None,
) -> dict:
    """runs 테이블에 실행 trace 를 생성한다."""
    trace = {
        "trace_id": trace_id,
        "service_id": service_id,
        "command": command,
        "trigger": trigger,
        "status": "running",
        "started_at": _now_kst(),
        "completed_at": None,
        "error": None,
        "confidence": None,
        "agent_logs": [],
        "total_cost": 0.0,
        "result_summary": {},
    }
    upsert_run(trace)
    publish_run_event(trace_id, "status", {"status": "running", "started_at": trace["started_at"]})
    return trace


def update_trace(trace_id: str, state: dict) -> None:
    """파이프라인 완료 후 PipelineState 기반으로 trace 를 갱신한다."""
    trace = load_trace(trace_id) or {"trace_id": trace_id}
    agent_logs = state.get("agent_logs", [])
    payload: dict = {
        "status": state.get("status") or "completed",
        "completed_at": _now_kst(),
        "error": state.get("error"),
        "confidence": _average_confidence(agent_logs),
        "agent_logs": agent_logs,
        "total_cost": state.get("total_cost", 0.0),
        "result_summary": _result_summary(state),
    }
    tc_results = state.get("tc_results")
    if isinstance(tc_results, dict) and tc_results:
        payload["tc_results"] = tc_results
    scenario_results = state.get("scenario_results")
    if isinstance(scenario_results, dict) and scenario_results:
        payload["scenario_results"] = scenario_results
    trace.update(payload)
    upsert_run(trace)
    progress.reset(trace_id)
    publish_run_event(trace_id, "status", {
        "status": trace.get("status"),
        "completed_at": trace.get("completed_at"),
        "summary": trace.get("result_summary"),
    })


def annotate_trace(trace_id: str, **fields: Any) -> None:
    """runs 테이블의 일부 필드를 갱신한다.

    create_trace 와 update_trace 사이 부분 정보를 누적 기록할 때 사용.
    """
    if not fields:
        return
    trace = load_trace(trace_id) or {"trace_id": trace_id}
    trace.update(fields)
    upsert_run(trace)
    publishable = {k: v for k, v in fields.items() if k in {"selected_total_tc_count", "scenario_ids", "staging_url"}}
    if publishable:
        publish_run_event(trace_id, "annotate", publishable)


def update_trace_aborted(trace_id: str, error: str) -> None:
    """파이프라인 비정상 종료 상태로 trace 를 갱신한다."""
    trace = load_trace(trace_id) or {"trace_id": trace_id}
    trace.update(
        {
            "status": "aborted",
            "completed_at": _now_kst(),
            "error": error,
        }
    )
    upsert_run(trace)
    progress.reset(trace_id)
    publish_run_event(trace_id, "status", {"status": "aborted", "error": error})


def load_trace(trace_id: str) -> dict | None:
    """runs 테이블에서 trace 를 조회해 반환한다."""
    return load_run(trace_id)


def list_traces(service_id: str) -> list[dict]:
    """service_id 기준 trace 목록을 최신순으로 반환한다."""
    return load_runs_by_service(service_id)


def _now_kst() -> str:
    return datetime.now(_KST).isoformat()


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
