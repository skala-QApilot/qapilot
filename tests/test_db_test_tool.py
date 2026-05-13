"""DBTestTool 단위 테스트."""

import pytest
from qapilot.shared.errors import ToolExecutionError
from qapilot.shared.schemas import ToolInput
from qapilot.tools.db_test_tool import DBTestTool


@pytest.fixture
def tool():
    return DBTestTool(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_not_implemented(tool):
    """DB 스캔 모듈 스펙 확정 전 ToolExecutionError 확인."""
    input = ToolInput(
        trace_id="test-trace-001",
        params={"tc_id": "TC-001"}
    )
    with pytest.raises(ToolExecutionError):
        await tool.run(input)