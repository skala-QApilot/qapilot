"""C3: trace_store.update_trace 가 tc_results / scenario_results 를 trace.json 에 보존하는지 검증.

Spring 측 ScenarioStatusAggregator 가 trace.json 단일 파일만 읽어 시나리오/TC
별 last_run_status 를 도출할 수 있도록 보장한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from qapilot.shared import trace_store


def _read_trace_file(qapilot_dir: Path, trace_id: str) -> dict:
    path = qapilot_dir / "traces" / f"{trace_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_update_trace_persists_tc_and_scenario_results(tmp_path: Path):
    qapilot_dir = tmp_path
    trace_store.create_trace(qapilot_dir, "trace-1", "test", None, "svc-1")

    state = {
        "status": "completed",
        "agent_logs": [],
        "tc_results": {"TC-001": "passed", "TC-002": "failed"},
        "scenario_results": {"TS-001": "failed"},
        "scenarios": [],
        "ui_results": [],
        "api_results": [],
        "db_results": [],
    }
    trace_store.update_trace(qapilot_dir, "trace-1", state)

    saved = _read_trace_file(qapilot_dir, "trace-1")
    assert saved["tc_results"] == {"TC-001": "passed", "TC-002": "failed"}
    assert saved["scenario_results"] == {"TS-001": "failed"}
    assert saved["status"] == "completed"


def test_update_trace_omits_empty_status_maps(tmp_path: Path):
    """비테스트 명령(generate_scenarios 등) 은 결과 맵이 비어 trace 에 추가되지 않아야 한다."""
    qapilot_dir = tmp_path
    trace_store.create_trace(qapilot_dir, "trace-2", "generate_scenarios", "init", "svc-1")

    state = {
        "status": "completed",
        "agent_logs": [],
        "scenarios": [],
        # tc_results / scenario_results 키 자체 없음
    }
    trace_store.update_trace(qapilot_dir, "trace-2", state)

    saved = _read_trace_file(qapilot_dir, "trace-2")
    assert "tc_results" not in saved
    assert "scenario_results" not in saved


def test_update_trace_omits_when_maps_present_but_empty(tmp_path: Path):
    qapilot_dir = tmp_path
    trace_store.create_trace(qapilot_dir, "trace-3", "test", None, "svc-1")

    state = {
        "status": "completed",
        "agent_logs": [],
        "tc_results": {},
        "scenario_results": {},
    }
    trace_store.update_trace(qapilot_dir, "trace-3", state)

    saved = _read_trace_file(qapilot_dir, "trace-3")
    assert "tc_results" not in saved
    assert "scenario_results" not in saved
