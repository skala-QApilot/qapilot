"""CrossCheckAgent 단위 테스트."""

import pytest
from unittest.mock import AsyncMock, patch
from dotenv import load_dotenv

load_dotenv()

from qapilot.agents.cross_check_agent import CrossCheckAgent
from qapilot.shared.schemas import AgentInput, CrossCheckMismatch
from qapilot.shared.schemas import AgentInput, CrossCheckMismatch, ExecuteResult


@pytest.fixture
def agent():
    return CrossCheckAgent(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_route_a_error_code_detected(agent):
    """경로 A: API 에러 코드 있을 때 Cross-check 건너뜀."""
    input = AgentInput(
        trace_id="test-trace-001",
        context={
            "ui_result": {"status": "fail", "steps": []},
            "api_trace": {
                "calls": [{"url": "/api/login", "status_code": 401, "response_body": {"code": "AUTH_TOKEN_EXPIRED"}}],
                "total_calls": 1,
                "error_calls": 1,
            },
            "db_result": {"snapshots": []},
        },
        params={"tc_id": "TC-001"}
    )
    output = await agent.run(input)
    print(output.result)

    assert output.result["route"] == "A"
    assert output.result["error_code"] == "AUTH_TOKEN_EXPIRED"
    assert "current_state_summary" in output.result


@pytest.mark.asyncio
async def test_route_b_no_error_code(agent):
    """경로 B: 에러 코드 없을 때 LLM으로 불일치 분석."""
    mock_execute_result = ExecuteResult(
        result={
            "cross_check": {
                "tc_id": "TC-001",
                "match_score": 0.5,
                "matched_fields": 0,
                "mismatched_fields": 1,
                "mismatches": [],
                "has_mismatch": True,
            },
            "current_state_summary": "UI 테스트: pass",
            "route": "B",
        },
        confidence=0.5,
    )

    with patch.object(agent, "_analyze_with_llm", new=AsyncMock(return_value=([], 0.5))):
        input = AgentInput(
            trace_id="test-trace-001",
            context={
                "ui_result": {"status": "pass", "steps": []},
                "api_trace": {
                    "calls": [{"url": "/api/login", "status_code": 200, "response_body": {"token": "abc"}}],
                    "total_calls": 1,
                    "error_calls": 0,
                },
                "db_result": {"snapshots": []},
            },
            params={"tc_id": "TC-001"}
        )
        output = await agent.run(input)

        print(output.result)

    assert output.result["route"] == "B"
    assert "current_state_summary" in output.result

