"""파일 기반 Agent trace 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from qapilot.db.run_writer import upsert_run
from qapilot.messaging.redis_pubsub import publish_run_event
from qapilot.shared.logger import get_logger

_logger = get_logger("trace_store")
_KST = ZoneInfo("Asia/Seoul")


def create_trace(
    qapilot_dir: str | Path,
    trace_id: str,
    command: str,
    trigger: str | None,
    service_id: str | None = None,
) -> dict:
    """실행 중 trace 파일을 생성한다."""
    trace = {
        "trace_id": trace_id,
        "service_id": service_id,
        "qapilot_dir": str(qapilot_dir),
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
    _save_trace(qapilot_dir, trace_id, trace)
    upsert_run(trace)
    publish_run_event(trace_id, "status", {"status": trace.get("status"), "started_at": trace.get("started_at")})
    return trace


def update_trace(qapilot_dir: str | Path, trace_id: str, state: dict) -> None:
    """파이프라인 완료 후 PipelineState 기반으로 trace를 갱신한다.

    test 명령의 경우 TC / 시나리오 별 status 요약(tc_results / scenario_results)도
    trace.json 에 보존하여, Spring 이 별도 디스크 스캔 없이 시나리오 카드에서
    last_run_status 를 표시할 수 있도록 한다.
    """
    trace = load_trace(qapilot_dir, trace_id) or {}
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
    _save_trace(qapilot_dir, trace_id, trace)
    upsert_run(trace)
    publish_run_event(trace_id, "status", {
        "status": trace.get("status"),
        "completed_at": trace.get("completed_at"),
        "summary": trace.get("result_summary"),
    })


def annotate_trace(qapilot_dir: str | Path, trace_id: str, **fields: Any) -> None:
    """trace.json 의 일부 필드를 갱신한다.

    create_trace 와 update_trace 사이에서 부분 정보를 누적 기록할 때 사용.
    예: 파이프라인 시작 직후 옵션 (scenario_ids, staging_url 등) 또는
    _load_scenarios_for_test 단계의 selected_total_tc_count 보존.
    """
    if not fields:
        return
    trace = load_trace(qapilot_dir, trace_id) or {"trace_id": trace_id}
    trace.update(fields)
    _save_trace(qapilot_dir, trace_id, trace)
    upsert_run(trace)
    # annotate 는 partial 갱신 — UI 가 관심 갖는 필드만 골라 push
    publishable = {k: v for k, v in fields.items() if k in {"selected_total_tc_count", "scenario_ids", "staging_url"}}
    if publishable:
        publish_run_event(trace_id, "annotate", publishable)


def update_trace_aborted(qapilot_dir: str | Path, trace_id: str, error: str) -> None:
    """파이프라인 비정상 종료(예외/Ctrl+C 등) 상태로 trace를 갱신한다.

    "aborted" 는 trace lifecycle 의 한 종단 상태이며, TC-level 의 ``status="failed"``
    (개별 테스트 케이스 실패) 와는 의미가 다르다. 두 축이 같은 단어를 쓰지 않도록
    분리한다.
    """
    trace = load_trace(qapilot_dir, trace_id) or {"trace_id": trace_id}
    trace.update(
        {
            "status": "aborted",
            "completed_at": _now_kst(),
            "error": error,
        }
    )
    _save_trace(qapilot_dir, trace_id, trace)
    upsert_run(trace)
    publish_run_event(trace_id, "status", {"status": "aborted", "error": error})


def load_trace(qapilot_dir: str | Path, trace_id: str) -> dict | None:
    """trace 파일을 읽어 반환한다."""
    path = _trace_path(qapilot_dir, trace_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.error("trace_load_failed", path=str(path), error=str(e))
        return None
    return data if isinstance(data, dict) else None


def list_traces(qapilot_dir: str | Path) -> list[dict]:
    """서비스 trace 목록을 최신순으로 반환한다."""
    traces_dir = _traces_dir(qapilot_dir)
    if not traces_dir.exists():
        return []

    traces = []
    for path in traces_dir.glob("*.json"):
        trace = _load_trace_file(path)
        if trace:
            traces.append(trace)
    return sorted(traces, key=lambda item: item.get("started_at", ""), reverse=True)


def _save_trace(qapilot_dir: str | Path, trace_id: str, trace: dict) -> None:
    path = _trace_path(qapilot_dir, trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(trace, ensure_ascii=False, indent=2, default=str)
    path.write_text(content, encoding="utf-8")


def _load_trace_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _traces_dir(qapilot_dir: str | Path) -> Path:
    return Path(qapilot_dir) / "traces"


def _trace_path(qapilot_dir: str | Path, trace_id: str) -> Path:
    return _traces_dir(qapilot_dir) / f"{trace_id}.json"


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
