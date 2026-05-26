"""이슈 #156 — pipeline._codebase_scan 의 로컬/Git 임시 분기 검증.

PR #154 가 무조건 GitCodebaseScannerTool 로 교체하여 로컬 디렉토리 e2e
(mini-bss-lite 등) 가 `KeyError: 'repos'` 로 즉시 fail 하던 회귀 해결.

회의 결정 (\"CLI 로컬 vs Git 분기 도입\") 반영 + 추후 Git 전용 전환 시 제거 예정.

분기 정책:
- run_options.repo_url 또는 repos 있음 → GitCodebaseScannerTool
- 둘 다 없음 → CodebaseScannerTool (로컬, 임시 fallback)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.orchestrator.pipeline import _codebase_scan


def _mock_scan_result() -> dict[str, Any]:
    """양쪽 Tool 의 ScanResult 공통 schema."""
    return {
        "files": [],
        "git_diff": None,
        "framework": "fastapi",
        "language": "python",
        "endpoint_count": 0,
    }


def _make_state(run_options: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_options": run_options,
        "trace_id": "test-trace-156",
    }


@pytest.mark.asyncio
async def test_local_mode_uses_codebase_scanner_tool_when_no_repo_url(mock_base_tool):
    """로컬 모드 — run_options 에 repo_url / repos 없음 → CodebaseScannerTool 사용."""
    state = _make_state({"trigger": "init"})

    mock_base_tool.run.return_value = MagicMock(result={"scan_result": _mock_scan_result()})

    with patch("qapilot.tools.codebase_scanner_tool.CodebaseScannerTool", return_value=mock_base_tool) as local_cls, \
         patch("qapilot.tools.git_codebase_scanner_tool.GitCodebaseScannerTool") as git_cls, \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"):
        result = await _codebase_scan(state)  # type: ignore[arg-type]

    local_cls.assert_called_once()
    git_cls.assert_not_called()
    assert result["current_layer"] == "L1A"
    assert result["scan_result"]["framework"] == "fastapi"


@pytest.mark.asyncio
async def test_git_mode_uses_git_codebase_scanner_when_repo_url_provided(mock_base_tool):
    """Git 모드 — run_options.repo_url 있음 → GitCodebaseScannerTool 사용."""
    state = _make_state({
        "trigger": "init",
        "repo_url": "https://github.com/example/repo",
        "token": "tok",
        "branch": "main",
    })

    mock_base_tool.run.return_value = MagicMock(result={"scan_result": _mock_scan_result()})

    with patch("qapilot.tools.codebase_scanner_tool.CodebaseScannerTool") as local_cls, \
         patch("qapilot.tools.git_codebase_scanner_tool.GitCodebaseScannerTool", return_value=mock_base_tool) as git_cls, \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"):
        await _codebase_scan(state)  # type: ignore[arg-type]

    git_cls.assert_called_once()
    local_cls.assert_not_called()


@pytest.mark.asyncio
async def test_git_mode_uses_git_scanner_when_repos_list_provided(mock_base_tool):
    """Git 모드 — repos (멀티 레포) 있음 → GitCodebaseScannerTool 사용."""
    state = _make_state({
        "trigger": "init",
        "repos": [{"repo_url": "https://github.com/a/b", "token": "t"}],
    })

    mock_base_tool.run.return_value = MagicMock(result={"scan_result": _mock_scan_result()})

    with patch("qapilot.tools.codebase_scanner_tool.CodebaseScannerTool") as local_cls, \
         patch("qapilot.tools.git_codebase_scanner_tool.GitCodebaseScannerTool", return_value=mock_base_tool) as git_cls, \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"):
        await _codebase_scan(state)  # type: ignore[arg-type]

    git_cls.assert_called_once()
    local_cls.assert_not_called()


@pytest.mark.asyncio
async def test_local_mode_passes_trigger_through(mock_base_tool):
    """로컬 모드도 trigger 등 params 전달."""
    state = _make_state({"trigger": "code_change"})

    mock_base_tool.run.return_value = MagicMock(result={"scan_result": _mock_scan_result()})

    with patch("qapilot.tools.codebase_scanner_tool.CodebaseScannerTool", return_value=mock_base_tool), \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"):
        await _codebase_scan(state)  # type: ignore[arg-type]

    # tool.run 의 호출 인자에서 trigger=code_change 확인
    call_args = mock_base_tool.run.await_args
    tool_input = call_args.args[0]
    assert tool_input.params.get("trigger") == "code_change"
