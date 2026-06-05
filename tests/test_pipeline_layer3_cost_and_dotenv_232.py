"""Layer 3 cost 집계 + load_dotenv 타이밍 단위 검증 (#232).

본인 A 영역 (orchestrator pipeline + api/main) 2 격차 fix:
1. _cross_check / _root_cause / _fix_recommend 노드의 return 에 agent_logs append
2. main.py 의 load_dotenv() 가 module 최상단에서 호출되는지 검증

PR #228 (_requirement_extract / _scenario_generate) 동형 패턴 — Layer 3 multi-TC for loop 호출.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from qapilot.orchestrator import pipeline as pipeline_module


def _async_return(value):
    async def _coro():
        return value
    return _coro()


def _fake_metadata(cost_usd: float, tokens: int = 100):
    md = MagicMock()
    md.model_dump.return_value = {
        "agent": "fake",
        "cost_usd": cost_usd,
        "tokens": tokens,
    }
    return md


# ── _cross_check agent_logs append ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_check_appends_agent_logs_per_tc():
    """_cross_check 가 매 TC 마다 agent_logs 누적 append + return 에 포함."""
    fake_output = MagicMock()
    fake_output.result = {"cross_check": {"tc_id": "TS-001-TC-01", "has_mismatch": False}}
    fake_output.metadata = _fake_metadata(0.0004)

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(side_effect=lambda *args, **kwargs: _async_return(fake_output))

    state = {
        "trace_id": "trace-x",
        "ui_results": [
            {"tc_id": "TC-1", "status": "pass"},
            {"tc_id": "TC-2", "status": "pass"},
            {"tc_id": "TC-3", "status": "pass"},
        ],
        "api_results": [],
        "db_results": [],
        "agent_logs": [],
    }

    with patch("qapilot.agents.cross_check_agent.CrossCheckAgent", return_value=fake_agent), \
         patch.object(pipeline_module, "upsert_tc_result"):
        result = await pipeline_module._cross_check(state)  # type: ignore[arg-type]

    assert "agent_logs" in result
    assert len(result["agent_logs"]) == 3, f"3 TC × 1 호출 = 3 logs 기대, 실제 {result['agent_logs']}"
    assert all(log["cost_usd"] == 0.0004 for log in result["agent_logs"])


@pytest.mark.asyncio
async def test_cross_check_preserves_existing_agent_logs():
    """이전 노드의 agent_logs (예: ActionMapper) 보존 + Layer 3 누적."""
    fake_output = MagicMock()
    fake_output.result = {"cross_check": {"tc_id": "TC-1", "has_mismatch": False}}
    fake_output.metadata = _fake_metadata(0.0005)

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(side_effect=lambda *args, **kwargs: _async_return(fake_output))

    state = {
        "trace_id": "trace-x",
        "ui_results": [{"tc_id": "TC-1", "status": "pass"}],
        "api_results": [],
        "db_results": [],
        "agent_logs": [{"agent": "action_mapper", "cost_usd": 0.072}],
    }

    with patch("qapilot.agents.cross_check_agent.CrossCheckAgent", return_value=fake_agent), \
         patch.object(pipeline_module, "upsert_tc_result"):
        result = await pipeline_module._cross_check(state)  # type: ignore[arg-type]

    # 이전 action_mapper + 신규 cross_check 누적
    assert len(result["agent_logs"]) == 2
    assert result["agent_logs"][0]["agent"] == "action_mapper"
    assert result["agent_logs"][1]["cost_usd"] == 0.0005


# ── _root_cause agent_logs append ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_cause_appends_agent_logs_per_mismatched_tc():
    """_root_cause 가 has_mismatch=True TC 만 호출 + 호출당 agent_logs 누적."""
    fake_output = MagicMock()
    fake_output.result = {"root_causes": [{"tc_id": "TC-1", "candidates": []}]}
    fake_output.metadata = _fake_metadata(0.00036)

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(side_effect=lambda *args, **kwargs: _async_return(fake_output))

    state = {
        "trace_id": "trace-x",
        "cross_check_results": [
            {"tc_id": "TC-1", "has_mismatch": True},
            {"tc_id": "TC-2", "has_mismatch": False},  # skip
            {"tc_id": "TC-3", "has_mismatch": True},
        ],
        "agent_logs": [],
    }

    with patch("qapilot.agents.root_cause_agent.RootCauseAgent", return_value=fake_agent):
        result = await pipeline_module._root_cause(state)  # type: ignore[arg-type]

    # has_mismatch=True 2건만
    assert len(result["agent_logs"]) == 2


# ── _fix_recommend agent_logs append ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_recommend_appends_agent_logs_per_rc():
    """_fix_recommend 가 root_cause_results 각 항목마다 agent_logs 누적."""
    fake_output = MagicMock()
    fake_output.result = {"fix_results": [{"tc_id": "TC-1", "suggestions": []}]}
    fake_output.metadata = _fake_metadata(0.00028)

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(side_effect=lambda *args, **kwargs: _async_return(fake_output))

    state = {
        "trace_id": "trace-x",
        "root_cause_results": [
            {"tc_id": "TC-1", "candidates": []},
            {"tc_id": "TC-2", "candidates": []},
        ],
        "agent_logs": [],
    }

    with patch("qapilot.agents.fix_recommender_agent.FixRecommenderAgent", return_value=fake_agent):
        result = await pipeline_module._fix_recommend(state)  # type: ignore[arg-type]

    assert len(result["agent_logs"]) == 2
    assert all(log["cost_usd"] == 0.00028 for log in result["agent_logs"])


# ── load_dotenv 타이밍 ────────────────────────────────────────────────────────


def test_main_py_calls_load_dotenv_at_module_top():
    """qapilot/api/main.py 가 module import 시 (create_app 함수 외부) load_dotenv 호출.

    create_app 내부 호출만 있으면 다른 module 의 module-level os.getenv capture (예:
    db_test_tool.py:20 MODULE_URL) 가 .env 로드 전에 빈 값으로 평가됨. 본 fix 회귀 방지.
    """
    from pathlib import Path

    main_py = Path(__file__).resolve().parent.parent / "qapilot" / "api" / "main.py"
    assert main_py.exists()
    content = main_py.read_text(encoding="utf-8")

    # module-level (def 들 전에) load_dotenv() 호출 있어야 함
    # 단순 검증: 첫 def / class 등장 전 라인에 "load_dotenv()" 포함
    lines = content.splitlines()
    first_def_idx = next(
        (i for i, line in enumerate(lines) if line.startswith(("def ", "class ", "async def "))),
        len(lines),
    )
    module_level_section = "\n".join(lines[:first_def_idx])
    assert "load_dotenv()" in module_level_section, (
        "load_dotenv() 호출이 module 최상단 (첫 def 전) 에 없음. "
        "create_app() 내부에만 있으면 다른 module 의 module-level os.getenv 가 빈 값 캡쳐 (#232)."
    )


def test_db_test_tool_module_url_capture_safe_after_load_dotenv(monkeypatch):
    """load_dotenv 가 import 전에 호출되면 db_test_tool.MODULE_URL 정상 평가 (회귀).

    실제로는 main.py 의 module 최상단 load_dotenv() 가 보장. 본 테스트는 그 결과로
    환경변수가 module-level 에서 정상 캡쳐되는지 검증.
    """
    monkeypatch.setenv("QAPILOT_SUT_DB_URL", "http://example.test/sut-db")
    # 새 import — module 다시 로드
    import importlib
    import qapilot.tools.db_test_tool as mod
    importlib.reload(mod)

    assert mod.MODULE_URL == "http://example.test/sut-db"
