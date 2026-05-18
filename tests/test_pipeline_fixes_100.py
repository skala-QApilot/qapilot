"""이슈 #100 fix 검증.

1) `_run_db_test_safe` 가 QAPILOT_MODULE_URL 미설정 시 Tool 호출 없이 즉시 skip 반환
2) `_cross_check` 가 UI fail TC 를 has_mismatch=True 로 보강
3) `is_chromium_installed_async` / `ensure_chromium_for_test_async` 가 RuntimeWarning 없이 동작
"""
from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.cli import _ensure_browser as EB
from qapilot.orchestrator import pipeline as P


# ── Fix #1: RuntimeWarning 0 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_chromium_installed_async_no_runtime_warning():
    """async 컨텍스트에서 await 가능 + RuntimeWarning 0."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch.object(EB, "_check_chromium_path", new_callable=AsyncMock, return_value=True):
            result = await EB.is_chromium_installed_async()
    assert result is True
    runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert runtime_warnings == []


@pytest.mark.asyncio
async def test_ensure_chromium_for_test_async_already_installed():
    """async 헬퍼 — 이미 설치 시 subprocess 호출 X."""
    with patch.object(EB, "_check_chromium_path", new_callable=AsyncMock, return_value=True):
        with patch.object(EB, "_run_playwright_install") as install:
            result = await EB.ensure_chromium_for_test_async()
    assert result is True
    install.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_chromium_for_test_async_install_when_missing():
    """async 헬퍼 — 부재 시 subprocess install 호출."""
    with patch.object(EB, "_check_chromium_path", new_callable=AsyncMock, return_value=False):
        with patch.object(EB, "_run_playwright_install", return_value=True) as install:
            result = await EB.ensure_chromium_for_test_async()
    assert result is True
    install.assert_called_once()


def test_is_chromium_installed_sync_closes_coroutine_on_runtime_error():
    """sync 진입점 — asyncio.run RuntimeError 시 coroutine close (RuntimeWarning 차단)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch("qapilot.cli._ensure_browser.asyncio.run", side_effect=RuntimeError):
            with patch.object(EB, "_check_chromium_path_sync", return_value=True):
                assert EB.is_chromium_installed() is True
    runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert runtime_warnings == []


# ── Fix #2: DBTest env 사전 점검 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_db_test_safe_skips_when_env_missing(monkeypatch):
    """QAPILOT_MODULE_URL 미설정 → Tool 호출 자체 X, skip 결과 즉시 반환."""
    monkeypatch.delenv("QAPILOT_MODULE_URL", raising=False)

    mock_tool_class = MagicMock()
    mock_input_class = MagicMock()

    result = await P._run_db_test_safe(
        tc_id="TC-1",
        trace_id="trace-1",
        DBTestTool=mock_tool_class,
        ToolInput=mock_input_class,
    )

    mock_tool_class.assert_not_called()  # Tool 생성 자체 안 함
    assert result["tc_id"] == "TC-1"
    assert "QAPILOT_MODULE_URL 미설정" in result["summary"]


@pytest.mark.asyncio
async def test_run_db_test_safe_calls_tool_when_env_set(monkeypatch):
    """QAPILOT_MODULE_URL 설정 시 Tool 호출."""
    monkeypatch.setenv("QAPILOT_MODULE_URL", "http://localhost:9999")

    mock_tool = MagicMock()
    mock_tool.run = AsyncMock(return_value=MagicMock(
        result={"db_test": {"tc_id": "TC-1", "snapshots": [], "summary": "OK"}}
    ))
    mock_tool_class = MagicMock(return_value=mock_tool)
    mock_input_class = MagicMock()

    result = await P._run_db_test_safe(
        tc_id="TC-1",
        trace_id="trace-1",
        DBTestTool=mock_tool_class,
        ToolInput=mock_input_class,
    )

    mock_tool_class.assert_called_once()
    assert result["summary"] == "OK"


# ── Fix #3: has_mismatch 의 UI fail 보강 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_check_ui_fail_forces_has_mismatch(monkeypatch):
    """모든 UI fail + CrossCheck 가 mismatch=False 반환 → has_mismatch=True 강제."""
    state = {
        "trace_id": "t",
        "ui_results": [
            {"tc_id": "TC-1", "status": "fail", "steps": []},
            {"tc_id": "TC-2", "status": "fail", "steps": []},
        ],
        "api_results": [{"tc_id": "TC-1", "calls": []}, {"tc_id": "TC-2", "calls": []}],
        "db_results": [{"tc_id": "TC-1"}, {"tc_id": "TC-2"}],
    }

    with patch("qapilot.agents.cross_check_agent.CrossCheckAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(return_value=MagicMock(result={
            "cross_check": {"tc_id": "TC-1", "match_score": 1.0,
                            "matched_fields": 0, "mismatched_fields": 0,
                            "mismatches": [], "has_mismatch": False},
        }))
        MockAgent.return_value = instance

        result = await P._cross_check(state)  # type: ignore[arg-type]

    # UI fail TC 둘 다 has_mismatch=True 로 강제
    assert all(cc["has_mismatch"] is True for cc in result["cross_check_results"])
    assert all(cc.get("ui_failed") is True for cc in result["cross_check_results"])
    assert result["has_mismatch"] is True


@pytest.mark.asyncio
async def test_cross_check_ui_pass_keeps_original_has_mismatch():
    """UI pass + CrossCheck mismatch=False → has_mismatch=False 보존."""
    state = {
        "trace_id": "t",
        "ui_results": [{"tc_id": "TC-1", "status": "pass", "steps": []}],
        "api_results": [{"tc_id": "TC-1", "calls": []}],
        "db_results": [{"tc_id": "TC-1"}],
    }

    with patch("qapilot.agents.cross_check_agent.CrossCheckAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(return_value=MagicMock(result={
            "cross_check": {"tc_id": "TC-1", "match_score": 1.0,
                            "matched_fields": 5, "mismatched_fields": 0,
                            "mismatches": [], "has_mismatch": False},
        }))
        MockAgent.return_value = instance

        result = await P._cross_check(state)  # type: ignore[arg-type]

    assert result["cross_check_results"][0]["has_mismatch"] is False
    assert result["has_mismatch"] is False


@pytest.mark.asyncio
async def test_cross_check_exception_preserves_ui_fail_signal():
    """CrossCheck Agent 자체 실패 시에도 UI fail TC 의 has_mismatch=True 보존."""
    state = {
        "trace_id": "t",
        "ui_results": [
            {"tc_id": "TC-1", "status": "fail", "steps": []},
            {"tc_id": "TC-2", "status": "pass", "steps": []},
        ],
        "api_results": [],
        "db_results": [],
    }

    with patch("qapilot.agents.cross_check_agent.CrossCheckAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(side_effect=ValueError("LLM fail"))
        MockAgent.return_value = instance

        result = await P._cross_check(state)  # type: ignore[arg-type]

    by_id = {cc["tc_id"]: cc for cc in result["cross_check_results"]}
    assert by_id["TC-1"]["has_mismatch"] is True  # UI fail → 강제 True
    assert by_id["TC-2"]["has_mismatch"] is False  # UI pass → False
    assert result["has_mismatch"] is True  # 어느 하나라도 True
