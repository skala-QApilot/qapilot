"""C3: TC / Scenario 별 status 집계 단위 테스트.

`_aggregate_tc_results`, `_aggregate_scenario_results` 가 ui/api/db 결과를
정확히 합쳐서 trace.json 에 보존될 status 맵을 생성하는지 검증.
"""
from __future__ import annotations

from qapilot.orchestrator import pipeline as P


# ── TC 단위 집계 ──────────────────────────────────────────────────────────────


def test_aggregate_tc_all_pass():
    ui = [{"tc_id": "TC-001", "status": "pass"}]
    api = [{"tc_id": "TC-001", "error_calls": 0}]
    db = [{"tc_id": "TC-001"}]
    assert P._aggregate_tc_results(ui, api, db) == {"TC-001": "passed"}


def test_aggregate_tc_ui_fail():
    ui = [{"tc_id": "TC-001", "status": "fail"}]
    api = [{"tc_id": "TC-001", "error_calls": 0}]
    db = [{"tc_id": "TC-001"}]
    assert P._aggregate_tc_results(ui, api, db) == {"TC-001": "failed"}


def test_aggregate_tc_api_error_calls_marks_failed():
    ui = [{"tc_id": "TC-001", "status": "pass"}]
    api = [{"tc_id": "TC-001", "error_calls": 1}]
    db = [{"tc_id": "TC-001"}]
    assert P._aggregate_tc_results(ui, api, db) == {"TC-001": "failed"}


def test_aggregate_tc_db_error_marks_failed():
    ui = [{"tc_id": "TC-001", "status": "pass"}]
    api = [{"tc_id": "TC-001", "error_calls": 0}]
    db = [{"tc_id": "TC-001", "error": "db unreachable"}]
    assert P._aggregate_tc_results(ui, api, db) == {"TC-001": "failed"}


def test_aggregate_tc_skip_separated_fallback_used_counts_as_pass():
    """ui skip = 검증 안 됨 → skipped 분리 (passed 로 세면 false-positive).

    fallback_used 는 실제로 검증을 수행한 것이라 passed 유지.
    (구버전: skip 도 passed — 2026-06-11 false-pass 채널 봉인으로 변경)
    """
    ui = [
        {"tc_id": "TC-001", "status": "skip"},
        {"tc_id": "TC-002", "status": "fallback_used"},
    ]
    api = [
        {"tc_id": "TC-001", "error_calls": 0},
        {"tc_id": "TC-002", "error_calls": 0},
    ]
    db = [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}]
    assert P._aggregate_tc_results(ui, api, db) == {
        "TC-001": "skipped",
        "TC-002": "passed",
    }


def test_aggregate_tc_ignores_results_without_tc_id():
    ui = [{"status": "pass"}, {"tc_id": "TC-001", "status": "pass"}]
    api = [{"tc_id": "TC-001", "error_calls": 0}]
    db = [{"tc_id": "TC-001"}]
    assert P._aggregate_tc_results(ui, api, db) == {"TC-001": "passed"}


# ── Scenario 단위 집계 ────────────────────────────────────────────────────────


def test_aggregate_scenario_all_tc_passed():
    tc_results = {"TC-001": "passed", "TC-002": "passed"}
    scenarios = [
        {"ts_id": "TS-001", "test_cases": [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}]},
    ]
    assert P._aggregate_scenario_results(tc_results, scenarios) == {"TS-001": "passed"}


def test_aggregate_scenario_any_tc_failed():
    tc_results = {"TC-001": "passed", "TC-002": "failed"}
    scenarios = [
        {"ts_id": "TS-001", "test_cases": [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}]},
    ]
    assert P._aggregate_scenario_results(tc_results, scenarios) == {"TS-001": "failed"}


def test_aggregate_scenario_skips_when_no_tc_ran():
    """selective 실행 시 본 시나리오 TC 가 하나도 안 돌면 status 미수록."""
    tc_results = {"TC-999": "passed"}  # 본 시나리오와 무관
    scenarios = [
        {"ts_id": "TS-001", "test_cases": [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}]},
    ]
    assert P._aggregate_scenario_results(tc_results, scenarios) == {}


def test_aggregate_scenario_partial_run_uses_only_executed_tcs():
    """일부 TC 만 실행되면, 실행된 것들로만 판정."""
    tc_results = {"TC-001": "passed"}  # TC-002 는 실행 안 됨
    scenarios = [
        {"ts_id": "TS-001", "test_cases": [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}]},
    ]
    assert P._aggregate_scenario_results(tc_results, scenarios) == {"TS-001": "passed"}


def test_aggregate_scenario_handles_empty_scenarios():
    assert P._aggregate_scenario_results({"TC-001": "passed"}, []) == {}


def test_aggregate_scenario_ignores_scenario_without_ts_id():
    tc_results = {"TC-001": "passed"}
    scenarios = [{"test_cases": [{"tc_id": "TC-001"}]}]
    assert P._aggregate_scenario_results(tc_results, scenarios) == {}
