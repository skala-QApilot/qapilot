"""tc_results status 도출 + generate_scenarios cost 집계 단위 검증 (#227).

본인 A 영역 (orchestrator pipeline) 2 격차 fix:
1. _derive_api_status / _derive_db_status — api/db kind 의 tc_results.status null 격차
2. _requirement_extract / _scenario_generate agent_logs append — generate_scenarios cost 0 표시

cross_check kind (yujin PR #226) 동형 패턴 — kind 별 명시적 status 도출.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from qapilot.orchestrator import pipeline as pipeline_module
from qapilot.orchestrator.pipeline import _derive_api_status, _derive_db_status


# ── _derive_api_status ───────────────────────────────────────────────────────


def test_derive_api_status_pass_when_zero_errors():
    """error_calls=0 → pass."""
    payload = {"tc_id": "TS-001-TC-01", "calls": [], "total_calls": 6, "error_calls": 0}
    assert _derive_api_status(payload) == "pass"


def test_derive_api_status_fail_when_errors_present():
    """error_calls>0 → fail."""
    payload = {"tc_id": "TS-001-TC-01", "calls": [], "total_calls": 6, "error_calls": 3}
    assert _derive_api_status(payload) == "fail"


def test_derive_api_status_none_for_non_dict():
    """payload 가 dict 아니면 None (방어)."""
    assert _derive_api_status(None) is None
    assert _derive_api_status("not-a-dict") is None  # type: ignore[arg-type]
    assert _derive_api_status([]) is None  # type: ignore[arg-type]


def test_derive_api_status_pass_when_error_calls_missing():
    """error_calls 키 없으면 0 으로 간주 (default → pass)."""
    payload = {"tc_id": "TS-001-TC-01", "calls": []}
    assert _derive_api_status(payload) == "pass"


def test_derive_api_status_none_on_invalid_error_calls_type():
    """error_calls 가 int 캐스팅 불가 (예: dict) → None (방어)."""
    payload = {"error_calls": {"bad": "value"}}
    assert _derive_api_status(payload) is None


# ── _derive_db_status ────────────────────────────────────────────────────────


def test_derive_db_status_skip_on_dbtest_skip_summary():
    """summary 가 'DBTest skip' 으로 시작 → skip (기존 trace 06b6e958 의 'DBTest skip: QAPILOT_MODULE_URL 미설정' 케이스)."""
    payload = {
        "tc_id": "TS-001-TC-01",
        "summary": "DBTest skip: QAPILOT_MODULE_URL 미설정 (env 사전 점검)",
        "snapshots": [],
    }
    assert _derive_db_status(payload) == "skip"


def test_derive_db_status_skip_case_insensitive():
    """summary 대소문자 무관."""
    payload = {"summary": "dbtest SKIP: 다른 이유", "snapshots": []}
    assert _derive_db_status(payload) == "skip"


def test_derive_db_status_pass_when_empty_summary():
    """summary 빈 문자열 + snapshots 비면 pass (기본 — DB 변화 없음 정상)."""
    payload = {"tc_id": "TS-001-TC-01", "summary": "", "snapshots": []}
    assert _derive_db_status(payload) == "pass"


def test_derive_db_status_pass_when_snapshots_present():
    """snapshots 있어도 의미 검증은 cross_check 책임 — tc_results.db 는 단순 pass."""
    payload = {
        "tc_id": "TS-001-TC-01",
        "summary": "DB snapshot complete",
        "snapshots": [{"table": "users", "row_count_before": 0, "row_count_after": 1, "added": 1}],
    }
    assert _derive_db_status(payload) == "pass"


def test_derive_db_status_none_for_non_dict():
    """payload 가 dict 아니면 None."""
    assert _derive_db_status(None) is None
    assert _derive_db_status("not-a-dict") is None  # type: ignore[arg-type]


# ── _mirror_tc_results_and_artifacts — api/db status 명시 전달 검증 ──────────


def test_mirror_tc_results_passes_explicit_status_for_api_db(tmp_path):
    """_mirror_tc_results_and_artifacts 가 api/db upsert 시 명시적 status 전달."""
    ui_result = {
        "tc_id": "TS-001-TC-01", "status": "fail",
        "steps": [], "total_duration_ms": 100,
    }
    api_result = {
        "tc_id": "TS-001-TC-01", "calls": [], "total_calls": 3, "error_calls": 0,
    }
    db_result = {
        "tc_id": "TS-001-TC-01", "summary": "DBTest skip: env 미설정", "snapshots": [],
    }
    captured: list[dict] = []

    def _fake_upsert(*, run_id, ts_id, tc_id, kind, payload, status=None):
        captured.append({"kind": kind, "status": status})
        return f"id-{kind}"

    with patch.object(pipeline_module, "upsert_tc_result", side_effect=_fake_upsert):
        pipeline_module._mirror_tc_results_and_artifacts(
            trace_id="trace-x",
            ts_id="TS-001",
            tc_id="TS-001-TC-01",
            ui_result=ui_result,
            api_result=api_result,
            db_result=db_result,
            screenshots_dir=tmp_path,
        )

    # 3 kind 모두 호출
    assert len(captured) == 3
    by_kind = {c["kind"]: c["status"] for c in captured}

    # ui — status 미전달 (upsert_tc_result 자동 추출 — payload["status"]="fail")
    assert by_kind["ui"] is None  # 명시 전달 안 함 (기존 동작 유지)
    # api — error_calls=0 → "pass" 명시 전달
    assert by_kind["api"] == "pass"
    # db — "DBTest skip" → "skip" 명시 전달
    assert by_kind["db"] == "skip"


def test_mirror_tc_results_api_fail_when_errors_present(tmp_path):
    """api error_calls>0 → status='fail' 명시 전달."""
    ui_result = {"tc_id": "TS-001-TC-01", "status": "pass", "steps": [], "total_duration_ms": 50}
    api_result = {"tc_id": "TS-001-TC-01", "calls": [], "total_calls": 5, "error_calls": 2}
    db_result = {"tc_id": "TS-001-TC-01", "summary": "", "snapshots": []}
    captured: list[dict] = []

    def _fake_upsert(*, run_id, ts_id, tc_id, kind, payload, status=None):
        captured.append({"kind": kind, "status": status})
        return f"id-{kind}"

    with patch.object(pipeline_module, "upsert_tc_result", side_effect=_fake_upsert):
        pipeline_module._mirror_tc_results_and_artifacts(
            trace_id="trace-x", ts_id="TS-001", tc_id="TS-001-TC-01",
            ui_result=ui_result, api_result=api_result, db_result=db_result,
            screenshots_dir=tmp_path,
        )

    by_kind = {c["kind"]: c["status"] for c in captured}
    assert by_kind["api"] == "fail"
    assert by_kind["db"] == "pass"  # 빈 summary + 빈 snapshots → 기본 pass


# ── _requirement_extract / _scenario_generate cost 집계 (agent_logs append) ──


def test_requirement_extract_appends_agent_logs_for_cost_aggregation(tmp_path):
    """_requirement_extract 가 return 에 agent_logs append — runner total_cost 합산 fix (#227)."""
    import asyncio

    fake_metadata = MagicMock()
    fake_metadata.model_dump.return_value = {
        "agent": "requirement_extractor",
        "cost_usd": 0.003717,
        "tokens": 16740,
    }
    fake_output = MagicMock()
    fake_output.result = {"requirements": [{"req_id": "REQ-001"}]}
    fake_output.metadata = fake_metadata

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(return_value=_async_return(fake_output))

    state = {
        "trace_id": "trace-x",
        "run_options": {"trigger": "init", "user_input": ""},
        "domain_rules": [],
        "agent_logs": [],
        "qapilot_dir": str(tmp_path),
    }

    with patch("qapilot.agents.requirement_extractor.RequirementExtractorAgent", return_value=fake_agent), \
         patch.object(pipeline_module, "_read_latest_prd_text", return_value="PRD content"):
        result = asyncio.run(pipeline_module._requirement_extract(state))  # type: ignore[arg-type]

    assert "agent_logs" in result, "agent_logs 가 return 에 포함되어야 함"
    assert len(result["agent_logs"]) == 1
    assert result["agent_logs"][0]["cost_usd"] == 0.003717


def test_scenario_generate_appends_agent_logs_for_cost_aggregation(tmp_path):
    """_scenario_generate 가 return 에 agent_logs append (#227)."""
    import asyncio

    fake_metadata = MagicMock()
    fake_metadata.model_dump.return_value = {
        "agent": "scenario_generator",
        "cost_usd": 0.745338,
        "tokens": 215691,
    }
    fake_output = MagicMock()
    fake_output.result = {"scenarios": [{"ts_id": "TS-001"}]}
    fake_output.metadata = fake_metadata

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(return_value=_async_return(fake_output))

    state = {
        "trace_id": "trace-x",
        "run_options": {"trigger": "init"},
        "scan_result": None,
        "domain_rules": [],
        "requirements": [{"req_id": "REQ-001"}],
        "agent_logs": [{"agent": "requirement_extractor", "cost_usd": 0.003717}],
        "qapilot_dir": str(tmp_path),
    }

    with patch("qapilot.agents.scenario_generator.agent.ScenarioGeneratorAgent", return_value=fake_agent), \
         patch.object(pipeline_module, "load_trace", return_value={"service_id": "svc-1"}):
        result = asyncio.run(pipeline_module._scenario_generate(state))  # type: ignore[arg-type]

    assert "agent_logs" in result
    # 이전 agent_logs (requirement_extractor) + 새 scenario_generator 누적
    assert len(result["agent_logs"]) == 2
    assert result["agent_logs"][0]["cost_usd"] == 0.003717
    assert result["agent_logs"][1]["cost_usd"] == 0.745338


def _async_return(value):
    """async mock helper."""
    async def _coro():
        return value
    return _coro()
