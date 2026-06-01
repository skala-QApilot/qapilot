"""APITraceTool 단위 테스트."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from qapilot.shared.errors import ToolExecutionError
from qapilot.shared.schemas import ToolInput
from qapilot.tools.api_trace_tool import APITraceTool


@pytest.fixture
def tool():
    return APITraceTool(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_run_without_page_raises_error(tool):
    """page 없으면 ToolExecutionError 발생 확인."""
    input = ToolInput(
        trace_id="test-trace-001",
        params={"tc_id": "TC-001"}
    )
    with pytest.raises(ToolExecutionError):
        await tool.run(input)


@pytest.mark.asyncio
async def test_listener_registered(tool):
    """Playwright 리스너 등록 확인."""
    mock_page = MagicMock()
    mock_page.on = MagicMock()

    input = ToolInput(
        trace_id="test-trace-001",
        params={"tc_id": "TC-001", "page": mock_page}
    )
    output = await tool.run(input)

    assert mock_page.on.call_count == 2
    assert output.trace_id == "test-trace-001"
    assert "api_trace" in output.result


def test_match_steps_matched(tool):
    """API 호출이 스텝 시간 범위 안에 있으면 matched_step_no 매칭."""
    test_start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

    # 스텝 1: 0~500ms, 스텝 2: 500~1000ms
    steps = [
        {"step_no": 1, "duration_ms": 500},
        {"step_no": 2, "duration_ms": 500},
    ]

    # 스텝 1 범위(200ms)에 API 호출 추가
    tool._calls = [
        {
            "timestamp": (test_start + timedelta(milliseconds=200)).isoformat(),
            "status_code": 200,
            "matched_step_no": None,
        }
    ]

    tool._match_steps(steps, test_start)

    assert tool._calls[0]["matched_step_no"] == 1


def test_match_steps_second_step(tool):
    """API 호출이 두 번째 스텝 시간 범위 안에 있으면 step_no 2 매칭."""
    test_start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

    steps = [
        {"step_no": 1, "duration_ms": 500},
        {"step_no": 2, "duration_ms": 500},
    ]

    # 스텝 2 범위(700ms)에 API 호출 추가
    tool._calls = [
        {
            "timestamp": (test_start + timedelta(milliseconds=700)).isoformat(),
            "status_code": 200,
            "matched_step_no": None,
        }
    ]

    tool._match_steps(steps, test_start)

    assert tool._calls[0]["matched_step_no"] == 2


def test_match_steps_out_of_range(tool):
    """API 호출이 모든 스텝 범위 밖이면 matched_step_no None 유지."""
    test_start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

    steps = [
        {"step_no": 1, "duration_ms": 500},
    ]

    # 스텝 범위 밖(2000ms)에 API 호출
    tool._calls = [
        {
            "timestamp": (test_start + timedelta(milliseconds=2000)).isoformat(),
            "status_code": 200,
            "matched_step_no": None,
        }
    ]

    tool._match_steps(steps, test_start)

    assert tool._calls[0]["matched_step_no"] is None


def test_match_steps_empty_steps(tool):
    """steps가 비어있으면 matched_step_no 변경 없음."""
    tool._calls = [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status_code": 200,
            "matched_step_no": None,
        }
    ]

    tool._match_steps([], datetime.now(timezone.utc))

    assert tool._calls[0]["matched_step_no"] is None