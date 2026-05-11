"""CrossCheckAgent 단위 테스트."""

import pytest
from unittest.mock import AsyncMock, patch
from dotenv import load_dotenv

load_dotenv()

from qapilot.agents.cross_check_agent import CrossCheckAgent
from qapilot.shared.schemas import AgentInput, CrossCheckMismatch


@pytest.fixture
def agent():
    return CrossCheckAgent(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_run_returns_cross_check_result(agent):
    """CrossCheckAgent 실행 결과 확인."""
    with patch.object(
        agent,
        "_map_fields_with_llm",
        new=AsyncMock(return_value=([], 1.0))
    ):
        input = AgentInput(
            trace_id="test-trace-001",
            context={
                "ui_result": {"steps": [{"step_no": 1, "action": "click", "status": "pass"}]},
                "api_trace": {"calls": [{"url": "/api/login", "response_body": {"token": "abc"}}]},
                "db_result": {"snapshots": []},
            },
            params={"tc_id": "TC-001"}
        )
        output = await agent.run(input)

    assert output.trace_id == "test-trace-001"
    assert output.confidence == 1.0
    assert "cross_check" in output.result


@pytest.mark.asyncio
async def test_mismatch_detected(agent):
    """불일치 탐지 확인."""
    mismatch = CrossCheckMismatch(
        field="username",
        ui_value="홍길동",
        api_value="hong",
        db_value=None,
        severity="high"
    )

    with patch.object(
        agent,
        "_map_fields_with_llm",
        new=AsyncMock(return_value=([mismatch], 0.5))
    ):
        input = AgentInput(
            trace_id="test-trace-001",
            context={
                "ui_result": {"steps": []},
                "api_trace": {"calls": []},
                "db_result": {"snapshots": []},
            },
            params={"tc_id": "TC-001"}
        )
        output = await agent.run(input)

    assert output.confidence == 0.5
    assert output.result["cross_check"]["has_mismatch"] is True