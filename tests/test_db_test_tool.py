"""DBTestTool 단위 테스트."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from qapilot.shared.schemas import ToolInput
from qapilot.tools.db_test_tool import DBTestTool


@pytest.fixture
def tool():
    return DBTestTool(trace_id="test-trace-001")


@pytest.mark.asyncio
async def test_snapshot_before_after(tool):
    """테스트 전후 스냅샷 비교 확인."""
    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(fetchall=lambda: [("users",), ("orders",)]),  # 테이블 목록 (before)
        MagicMock(scalar=lambda: 10),  # users before
        MagicMock(scalar=lambda: 5),   # orders before
        MagicMock(fetchall=lambda: [("users",), ("orders",)]),  # 테이블 목록 (after)
        MagicMock(scalar=lambda: 11),  # users after
        MagicMock(scalar=lambda: 5),   # orders after
        MagicMock(fetchall=lambda: []),  # SQL 로그 캡처
    ]

    with patch.object(tool, "session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = ToolInput(
            trace_id="test-trace-001",
            params={"tc_id": "TC-001"}
        )
        output = await tool.run(result)

    assert output.trace_id == "test-trace-001"
    assert "db_test" in output.result


@pytest.mark.asyncio
async def test_rollback_called(tool):
    """롤백이 호출되는지 확인."""
    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(fetchall=lambda: []),  # 테이블 목록 (before)
        MagicMock(fetchall=lambda: []),  # 테이블 목록 (after)
        MagicMock(fetchall=lambda: []),  # SQL 로그 캡처
    ]

    with patch.object(tool, "session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = ToolInput(
            trace_id="test-trace-001",
            params={"tc_id": "TC-001"}
        )
        await tool.run(result)

    mock_session.rollback.assert_called_once()