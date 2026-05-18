"""DBTestTool 단위 테스트."""

import pytest
from unittest.mock import AsyncMock, patch

from qapilot.shared.schemas import ToolInput
from qapilot.tools.db_test_tool import DBTestTool


@pytest.fixture
def tool():
    return DBTestTool(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_snapshot_before_after(tool):
    """테스트 전후 스냅샷 비교 확인."""
    with patch("qapilot.tools.db_test_tool.MODULE_URL", "http://localhost:8001"), \
         patch.object(tool, "_get_tables", new=AsyncMock(return_value=["users", "orders"])), \
         patch.object(tool, "_get_snapshot", new=AsyncMock(side_effect=[
             {"row_count": 10},
             {"row_count": 5},
             {"row_count": 11},
             {"row_count": 5},
         ])), \
         patch.object(tool, "_get_sql_logs", new=AsyncMock(return_value=[])), \
         patch.object(tool, "_rollback", new=AsyncMock()):

        input = ToolInput(
            trace_id="test-trace-001",
            params={"tc_id": "TC-001"}
        )
        output = await tool.run(input)

    assert output.trace_id == "test-trace-001"
    assert "db_test" in output.result


@pytest.mark.asyncio
async def test_rollback_called(tool):
    """롤백 호출 확인."""
    mock_rollback = AsyncMock()