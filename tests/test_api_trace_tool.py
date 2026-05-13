"""APITraceTool 단위 테스트."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from qapilot.shared.schemas import ToolInput
from qapilot.tools.api_trace_tool import APITraceTool


@pytest.fixture
def tool():
    return APITraceTool(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_run_without_page_raises_error(tool):
    """page 없으면 ValueError 발생 확인."""
    input = ToolInput(
        trace_id="test-trace-001",
        params={"tc_id": "TC-001"}
    )
    with pytest.raises(ValueError):
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

    # request, response 리스너 2개 등록됐는지 확인
    assert mock_page.on.call_count == 2
    assert output.trace_id == "test-trace-001"
    assert "api_trace" in output.result