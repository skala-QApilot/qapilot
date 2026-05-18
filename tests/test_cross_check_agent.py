"""CrossCheckAgent 단위 테스트."""

import pytest
from unittest.mock import AsyncMock, patch
from dotenv import load_dotenv

load_dotenv()

from qapilot.agents.cross_check_agent import CrossCheckAgent
from qapilot.shared.schemas import AgentInput, CrossCheckMismatch, ExecuteResult


@pytest.fixture
def agent():
    return CrossCheckAgent(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_route_a_api_error_code_detected(agent):
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

    with patch.object(agent, "_analyze_with_llm", new=AsyncMock(return_value=([], 1.0, "none", "모든 계층 데이터가 일치함."))):
        output = await agent.run(input)

    assert output.result["route"] == "A"
    assert output.result["error_code"] == "AUTH_TOKEN_EXPIRED"
    assert "summary" in output.result


@pytest.mark.asyncio
async def test_route_a_ui_error_code_detected(agent):
    """경로 A: UI 에러 코드 있을 때 Cross-check 건너뜀."""
    input = AgentInput(
        trace_id="test-trace-001",
        context={
            "ui_result": {
                "status": "fail",
                "steps": [{"step_no": 1, "action": "click", "status": "fail", "error": "TOOL_UI_LOCATOR_NOT_FOUND: locator('#submit') timeout", "screenshot_path": None, "console_logs": [], "duration_ms": 100}]
            },
            "api_trace": {
                "calls": [],
                "total_calls": 0,
                "error_calls": 0,
            },
            "db_result": {"snapshots": []},
        },
        params={"tc_id": "TC-001"}
    )

    with patch.object(agent, "_analyze_with_llm", new=AsyncMock(return_value=([], 1.0, "none", "모든 계층 데이터가 일치함."))):
        output = await agent.run(input)

    assert output.result["route"] == "A"
    assert output.result["error_code"] == "TOOL_UI_LOCATOR_NOT_FOUND"
    assert "summary" in output.result


@pytest.mark.asyncio
async def test_route_a_db_error_code_detected(agent):
    """경로 A: DB 에러 코드 있을 때 Cross-check 건너뜀."""
    input = AgentInput(
        trace_id="test-trace-001",
        context={
            "ui_result": {"status": "pass", "steps": []},
            "api_trace": {
                "calls": [],
                "total_calls": 0,
                "error_calls": 0,
            },
            "db_result": {"snapshots": [], "error_code": 500},
        },
        params={"tc_id": "TC-001"}
    )

    with patch.object(agent, "_analyze_with_llm", new=AsyncMock(return_value=([], 1.0, "none", "모든 계층 데이터가 일치함."))):
        output = await agent.run(input)

    assert output.result["route"] == "A"
    assert output.result["error_code"] == "500"
    assert "summary" in output.result


@pytest.mark.asyncio
async def test_route_b_no_error_code(agent):
    """경로 B: 에러 코드 없을 때 LLM으로 불일치 분석."""
    with patch.object(agent, "_analyze_with_llm", new=AsyncMock(return_value=([], 0.5, "ui-api-mismatch", "api /api/login의 응답은 hong이(가) 왔기 때문에, 로그인 화면의 username 부분에서 홍길동이 떠야 하는데 hong이 떴음."))):
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

    assert output.result["route"] == "B"
    assert output.result["error_code"] == "ui-api-mismatch"
    assert "summary" in output.result