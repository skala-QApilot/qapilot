"""Layer 3 wire-up 단위 테스트.

mock Agent 들로 `_root_cause` / `_fix_recommend` / 확장된 `_report`
의 wire-up 동작 검증. 실 LLM 의존은 통합 테스트로 분리.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.orchestrator import pipeline as P


def _state_with_mismatch() -> dict:
    return {
        "trace_id": "trace-l3",
        "ui_results": [{"tc_id": "TC-1", "status": "fail", "steps": [
            {"step_no": 1, "action": "click", "status": "fail",
             "error": "TOOL_UI_LOCATOR_NOT_FOUND: ..."},
        ]}],
        "cross_check_results": [
            {"tc_id": "TC-1", "match_score": 0.2, "mismatched_fields": 3,
             "mismatches": [{"field": "amount", "ui_value": "5000", "api_value": "4500"}],
             "has_mismatch": True, "error_code": "TOOL_UI_ASSERTION_FAIL",
             "summary": "금액 불일치"},
            {"tc_id": "TC-2", "match_score": 1.0, "mismatched_fields": 0,
             "mismatches": [], "has_mismatch": False},
        ],
    }


# ── _root_cause ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_cause_passes_error_code_summary_mismatches():
    """RootCauseAgent params 에 cross_check 의 error_code/summary/mismatches 전달 확인."""
    state = _state_with_mismatch()

    captured: dict = {}

    async def fake_run(agent_input):
        captured["params"] = agent_input.params
        return MagicMock(result={
            "root_causes": [{
                "tc_id": agent_input.params["tc_id"],
                "candidates": [{"rank": 1, "cause": "X", "confidence": 0.9,
                                "evidences": [], "affected_file": "a.py", "affected_line": 10}],
            }]
        })

    with patch("qapilot.agents.root_cause_agent.RootCauseAgent") as MockAgent:
        instance = MagicMock()
        instance.run = fake_run
        MockAgent.return_value = instance

        result = await P._root_cause(state)  # type: ignore[arg-type]

    assert captured["params"]["tc_id"] == "TC-1"
    assert captured["params"]["error_code"] == "TOOL_UI_ASSERTION_FAIL"
    assert captured["params"]["summary"] == "금액 불일치"
    assert len(captured["params"]["mismatches"]) == 1
    assert captured["params"]["has_mismatch"] is True

    assert len(result["root_cause_results"]) == 1
    assert result["root_cause_results"][0]["candidates"][0]["cause"] == "X"


@pytest.mark.asyncio
async def test_root_cause_graceful_on_exception():
    state = _state_with_mismatch()
    with patch("qapilot.agents.root_cause_agent.RootCauseAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(side_effect=ValueError("LLM 실패"))
        MockAgent.return_value = instance
        result = await P._root_cause(state)  # type: ignore[arg-type]
    assert "RootCause skip" in result["root_cause_results"][0]["error"]


# ── _fix_recommend ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_recommend_passes_candidates():
    """FixRecommender params 에 candidates 전달 확인."""
    state = {
        "trace_id": "t",
        "root_cause_results": [{
            "tc_id": "TC-1",
            "candidates": [
                {"rank": 1, "cause": "C1", "confidence": 0.8, "evidences": [],
                 "affected_file": "a.py", "affected_line": 10},
            ],
        }],
    }
    captured: dict = {}

    async def fake_run(agent_input):
        captured["params"] = agent_input.params
        return MagicMock(result={
            "fix_results": [{
                "tc_id": agent_input.params["tc_id"],
                "suggestions": [{"file_path": "a.py", "line_number": 10,
                                "description": "fix", "code_snippet": "X", "similar_issues": []}],
            }]
        })

    with patch("qapilot.agents.fix_recommender_agent.FixRecommenderAgent") as MockAgent:
        instance = MagicMock()
        instance.run = fake_run
        MockAgent.return_value = instance
        result = await P._fix_recommend(state)  # type: ignore[arg-type]

    assert captured["params"]["tc_id"] == "TC-1"
    assert len(captured["params"]["candidates"]) == 1
    assert result["fix_results"][0]["suggestions"][0]["description"] == "fix"


@pytest.mark.asyncio
async def test_fix_recommend_skips_empty_root_cause():
    """root_cause_results 빈 list → fix_results 빈 list."""
    state = {"trace_id": "t", "root_cause_results": []}
    result = await P._fix_recommend(state)  # type: ignore[arg-type]
    assert result["fix_results"] == []


# ── _report 확장 (Layer 3 정보 포함) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_includes_layer3_sections(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = {
        "trace_id": "trace-r3",
        "qapilot_dir": str(tmp_path / ".qapilot"),
        "ui_results": [{"tc_id": "TC-1", "status": "fail", "steps": []}],
        "cross_check_results": [
            {"tc_id": "TC-1", "match_score": 0.0, "mismatched_fields": 2, "has_mismatch": True},
        ],
        "root_cause_results": [
            {"tc_id": "TC-1", "candidates": [
                {"rank": 1, "cause": "Null pointer at line 42", "confidence": 0.85,
                 "evidences": [], "affected_file": "backend/app.py", "affected_line": 42},
            ]},
        ],
        "fix_results": [
            {"tc_id": "TC-1", "suggestions": [
                {"file_path": "backend/app.py", "line_number": 42,
                 "description": "None 체크 추가", "code_snippet": "...",
                 "similar_issues": []},
            ]},
        ],
    }
    result = await P._report(state)  # type: ignore[arg-type]

    body = Path(result["report_path"]).read_text(encoding="utf-8")
    # 4 섹션 모두 포함 (장애 분류 섹션은 제거됨)
    assert "## 1. 실패 케이스" in body
    assert "## 2. Cross-check" in body
    assert "## 3. 원인 분석" in body
    assert "## 4. 해결 방안" in body
    # 콘텐츠 검증
    assert "Null pointer" in body  # cause
    assert "backend/app.py:42" in body  # 위치
    assert "None 체크 추가" in body  # fix description


@pytest.mark.asyncio
async def test_report_omits_layer3_sections_when_empty(tmp_path: Path, monkeypatch):
    """Layer 3 결과 빈 list → 해당 섹션 생략."""
    monkeypatch.chdir(tmp_path)
    state = {
        "trace_id": "trace-empty",
        "qapilot_dir": str(tmp_path / ".qapilot"),
        "ui_results": [{"tc_id": "TC-1", "status": "pass", "steps": []}],
        "cross_check_results": [
            {"tc_id": "TC-1", "match_score": 1.0, "mismatched_fields": 0, "has_mismatch": False},
        ],
    }
    result = await P._report(state)  # type: ignore[arg-type]
    body = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "## 1. 실패 케이스" in body
    assert "## 2. Cross-check" in body
    assert "## 3. 원인 분석" not in body
    assert "## 4. 해결 방안" not in body
