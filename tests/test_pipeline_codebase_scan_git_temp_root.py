"""pipeline._codebase_scan 의 Git mode 결과의 temp_repo_root 자동 주입 검증.

SaaS 흐름 본질 fix:
- 사용자가 UI 등록 시 GitHub repo URL + token + branch 만 입력 (로컬 path 없음)
- 8001 의 GitCodebaseScannerTool 이 REST API 로 받은 file content 를 임시 dir 에 reconstruct
- `_codebase_scan` 이 그 임시 dir path 를 (a) scan_all_metadata 의 _project_root 로 직접 사용
  + (b) state.target_root 로 자동 주입해 후속 노드 (TV codebase-aware) 도 활용

상세: memory/project_qapilot_saas_target_root_gap.md
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.orchestrator.pipeline import _codebase_scan


def _scan_result_with_commit(commit: str = "abc1234567890") -> dict[str, Any]:
    return {
        "files": [],
        "git_diff": {"commit_hash": commit, "prev_hash": "", "changed_files": [],
                     "added_lines": 0, "deleted_lines": 0, "diff_detail": [],
                     "author": "x", "author_email": "x", "commit_timestamp": "x", "blame": []},
        "framework": "fastapi",
        "language": "python",
        "endpoint_count": 0,
    }


def _state_with_git() -> dict[str, Any]:
    return {
        "run_options": {
            "trigger": "init",
            "repo_url": "https://github.com/org/repo",
            "token": "ghp_xxx",
            "branch": "develop",
        },
        "trace_id": "trace-git-temp",
        "service_id": "svc-1",
    }


@pytest.mark.asyncio
async def test_git_mode_temp_repo_root_injected_to_target_root_in_state(tmp_path, mock_base_tool):
    """Git mode 결과의 _metadata.temp_repo_root 가 return dict 의 target_root 로 주입."""
    state = _state_with_git()
    temp_root = str(tmp_path / "git_temp")
    (tmp_path / "git_temp").mkdir()

    mock_base_tool.run = AsyncMock(return_value=MagicMock(result={
        "scan_result": _scan_result_with_commit(),
        "_metadata": {"temp_repo_root": temp_root},
    }))

    with patch("qapilot.tools.git_codebase_scanner_tool.GitCodebaseScannerTool",
               return_value=mock_base_tool), \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"), \
         patch("qapilot.orchestrator.pipeline.load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.scan.orchestrator.scan_all_metadata", new=AsyncMock()):
        result = await _codebase_scan(state)  # type: ignore[arg-type]

    assert result["target_root"] == temp_root
    assert result["current_layer"] == "L1A"


@pytest.mark.asyncio
async def test_git_mode_scan_all_metadata_called_with_temp_root(tmp_path, mock_base_tool):
    """Git mode 의 임시 dir 이 scan_all_metadata 의 project_root 로 직접 전달."""
    state = _state_with_git()
    temp_root = str(tmp_path / "git_temp")
    (tmp_path / "git_temp").mkdir()

    mock_base_tool.run = AsyncMock(return_value=MagicMock(result={
        "scan_result": _scan_result_with_commit(commit="def4567890ab"),
        "_metadata": {"temp_repo_root": temp_root},
    }))

    scan_meta_mock = AsyncMock()
    with patch("qapilot.tools.git_codebase_scanner_tool.GitCodebaseScannerTool",
               return_value=mock_base_tool), \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"), \
         patch("qapilot.orchestrator.pipeline.load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.scan.orchestrator.scan_all_metadata", new=scan_meta_mock):
        await _codebase_scan(state)  # type: ignore[arg-type]

    scan_meta_mock.assert_called_once()
    # positional: (service_id, project_root, commit_sha)
    args, kwargs = scan_meta_mock.call_args
    from pathlib import Path
    assert args[0] == "svc-1"
    assert args[1] == Path(temp_root)
    assert args[2] == "def4567890ab"


@pytest.mark.asyncio
async def test_no_temp_repo_root_when_local_mode(tmp_path, mock_base_tool):
    """로컬 mode (CodebaseScannerTool) 결과에 _metadata 없으면 target_root 자동 주입 안 함."""
    state = {
        "run_options": {"trigger": "init"},  # repo_url / repos 없음
        "trace_id": "trace-local",
        "service_id": "svc-1",
    }

    mock_base_tool.run = AsyncMock(return_value=MagicMock(result={
        "scan_result": _scan_result_with_commit(),
        # _metadata 키 자체 없음 (로컬 tool)
    }))

    with patch("qapilot.tools.codebase_scanner_tool.CodebaseScannerTool",
               return_value=mock_base_tool), \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"), \
         patch("qapilot.orchestrator.pipeline.load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.scan.orchestrator.scan_all_metadata", new=AsyncMock()):
        result = await _codebase_scan(state)  # type: ignore[arg-type]

    assert "target_root" not in result  # 로컬 흐름은 state 갱신 안 함


@pytest.mark.asyncio
async def test_git_mode_with_temp_root_none_skips_injection(tmp_path, mock_base_tool):
    """Git mode 인데 _metadata.temp_repo_root = None 이면 자동 주입 안 함."""
    state = _state_with_git()

    mock_base_tool.run = AsyncMock(return_value=MagicMock(result={
        "scan_result": _scan_result_with_commit(),
        "_metadata": {"temp_repo_root": None},
    }))

    with patch("qapilot.tools.git_codebase_scanner_tool.GitCodebaseScannerTool",
               return_value=mock_base_tool), \
         patch("qapilot.orchestrator.pipeline._save_codebase_index_to_disk"), \
         patch("qapilot.orchestrator.pipeline.load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.scan.orchestrator.scan_all_metadata", new=AsyncMock()):
        result = await _codebase_scan(state)  # type: ignore[arg-type]

    assert "target_root" not in result
