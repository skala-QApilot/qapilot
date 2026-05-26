"""이슈 #105 fix 검증.

`_code_generate` 노드가 Agent fail 시 graceful — 빈 generated_codes 반환,
pipeline 정상 진행. ActionMapping 78건이 다음 `_save_codes` 노드에 그대로 전달됨.

배경: 2026-05-18 검증 사이클에서 CodeGeneratorAgent 의 단일 LLM 호출 JSON parse
실패 (Invalid \\escape) 가 retry 4회 후 pipeline 전체를 중단시킴. 직전 ActionMapper
의 78 TC 매핑이 `_save_codes` 미도달로 모두 휘발. spec §4.5 (UITestTool 은
ActionMapping 으로 직접 실행) 와 충돌.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.orchestrator import pipeline as P


@pytest.mark.asyncio
async def test_code_generate_graceful_on_agent_failure():
    """Agent.run() 이 raise 해도 노드는 dict 반환 + generated_codes=[]."""
    state = {
        "trace_id": "trace-105",
        "action_mappings": [
            {"tc_id": "TS-001-TC-01", "actions": []},
            {"tc_id": "TS-001-TC-02", "actions": []},
        ],
        "scenarios": [{"ts_id": "TS-001"}],
        "agent_logs": [],
    }

    with patch("qapilot.agents.code_generator_agent.CodeGeneratorAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(side_effect=ValueError("AGENT_005: retry 4회 모두 실패"))
        MockAgent.return_value = instance

        result = await P._code_generate(state)  # type: ignore[arg-type]

    assert isinstance(result, dict)
    assert result["generated_codes"] == []
    assert result["agent_logs"] == []  # 실패 시 agent_logs 추가 안 됨


@pytest.mark.asyncio
async def test_code_generate_normal_path_preserves_result():
    """정상 케이스 — Agent.run() 성공 시 generated_codes 와 agent_logs 보존."""
    state = {
        "trace_id": "trace-105",
        "action_mappings": [{"tc_id": "TS-001-TC-01", "actions": []}],
        "scenarios": [{"ts_id": "TS-001"}],
        "agent_logs": [{"existing": "log"}],
    }

    fake_metadata = MagicMock()
    fake_metadata.model_dump = MagicMock(return_value={"agent": "code_generator", "ok": True})

    with patch("qapilot.agents.code_generator_agent.CodeGeneratorAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(return_value=MagicMock(
            result={"generated_codes": [{"tc_id": "TS-001-TC-01", "code": "// ok"}]},
            metadata=fake_metadata,
        ))
        MockAgent.return_value = instance

        result = await P._code_generate(state)  # type: ignore[arg-type]

    assert len(result["generated_codes"]) == 1
    assert result["generated_codes"][0]["tc_id"] == "TS-001-TC-01"
    assert len(result["agent_logs"]) == 2  # 기존 1 + 신규 1


@pytest.mark.asyncio
async def test_save_codes_persists_action_mappings_when_code_empty(tmp_path, monkeypatch):
    """spec §4.5 보장 — generated_codes 가 비어도 ActionMapping 은 디스크 저장."""
    monkeypatch.chdir(tmp_path)
    state = {
        "trace_id": "trace-105",
        "qapilot_dir": str(tmp_path / ".qapilot"),
        "generated_codes": [],
        "action_mappings": [
            {"tc_id": "TS-001-TC-01", "actions": [{"type": "click"}]},
            {"tc_id": "TS-001-TC-02", "actions": [{"type": "fill"}]},
        ],
    }

    result = await P._save_codes(state)  # type: ignore[arg-type]

    am_dir = tmp_path / ".qapilot" / "action-mappings"
    assert (am_dir / "TS-001-TC-01.json").exists()
    assert (am_dir / "TS-001-TC-02.json").exists()
    # generated-code 디렉토리는 생성되나 비어있음 (CodeGen fail 흔적)
    assert result["saved_code_paths"] == []
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_code_generate_json_decode_error_does_not_propagate():
    """원인 시나리오 재현 — JSONDecodeError 발생해도 pipeline 중단 안 됨."""
    import json

    state = {
        "trace_id": "trace-105",
        "action_mappings": [{"tc_id": "TS-001-TC-01", "actions": []}],
        "scenarios": [],
        "agent_logs": [],
    }

    with patch("qapilot.agents.code_generator_agent.CodeGeneratorAgent") as MockAgent:
        instance = MagicMock()
        instance.run = AsyncMock(
            side_effect=json.JSONDecodeError("Invalid \\escape", doc="...", pos=1169)
        )
        MockAgent.return_value = instance

        result = await P._code_generate(state)  # type: ignore[arg-type]

    # raise 가 아닌 dict 반환
    assert result["generated_codes"] == []
