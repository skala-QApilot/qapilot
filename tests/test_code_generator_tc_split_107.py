"""이슈 #107 — CodeGeneratorAgent TC-별 LLM 호출 분할 검증.

spec §4.5.1 의 "C(CodeGenerator) generates leniently" 본격 구현. 한 TC 의 LLM
응답 JSON parse 실패가 다른 TC 의 코드 생성을 차단하지 않음.

배경:
- 이전 단일 LLM 호출 구조: 78 TC 한 응답의 escape 오류 1건 → 전체 실패 (이슈 #105)
- TC-별 분할 후: 그 TC 만 skip, 나머지 정상 생성
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.agents.code_generator_agent import (
    CodeGeneratorAgent,
    _build_tc_to_scenario_index,
)


def _make_agent() -> CodeGeneratorAgent:
    """LLM / prompts / logger 만 mock 한 Agent — _execute 직접 호출."""
    agent = CodeGeneratorAgent.__new__(CodeGeneratorAgent)
    agent.llm = MagicMock()
    agent.llm.chat = AsyncMock()
    agent.prompts = MagicMock()
    agent.prompts.system = MagicMock(return_value="SYSTEM")
    agent.prompts.render = MagicMock(side_effect=lambda **kw: f"USER<{kw.get('action_mappings','')}>")
    agent.logger = MagicMock()
    agent.with_correction_hint = MagicMock(side_effect=lambda p, e: p)
    return agent


def _ok_response(tc_id: str, code: str = "test('ok', async ({ page }) => {});") -> MagicMock:
    return MagicMock(content=json.dumps({
        "generated_codes": [{"tc_id": tc_id, "code": code, "self_fix_count": 0, "syntax_valid": True}],
        "confidence": 0.9,
    }))


# ── _build_tc_to_scenario_index ────────────────────────────────────────────


def test_tc_index_slices_single_tc_per_entry():
    scenarios = [{
        "ts_id": "TS-001", "name": "auth", "depends_on": [],
        "affected_files": ["a.py"],
        "test_cases": [
            {"tc_id": "TS-001-TC-01", "name": "정상 로그인", "given": "g", "when": "w", "then": "t"},
            {"tc_id": "TS-001-TC-02", "name": "실패 로그인", "given": "g", "when": "w", "then": "t"},
        ],
    }]
    idx = _build_tc_to_scenario_index(scenarios)
    assert set(idx.keys()) == {"TS-001-TC-01", "TS-001-TC-02"}
    # 각 entry 는 단일 TC 만 포함
    for tc_id, sliced in idx.items():
        assert sliced["ts_id"] == "TS-001"
        assert len(sliced["test_cases"]) == 1
        assert sliced["test_cases"][0]["tc_id"] == tc_id
        # 메타 보존
        assert sliced["affected_files"] == ["a.py"]


def test_tc_index_empty_when_no_scenarios():
    assert _build_tc_to_scenario_index([]) == {}


# ── _execute graceful 동작 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_all_success():
    """3 TC 모두 LLM 응답 정상 → generated_codes 3건 / failed_tcs 0건."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TS-001-TC-01"),
        _ok_response("TS-001-TC-02"),
        _ok_response("TS-001-TC-03"),
    ])

    action_mappings = [
        {"tc_id": "TS-001-TC-01", "actions": []},
        {"tc_id": "TS-001-TC-02", "actions": []},
        {"tc_id": "TS-001-TC-03", "actions": []},
    ]
    scenarios = [{
        "ts_id": "TS-001", "test_cases": [
            {"tc_id": "TS-001-TC-01"}, {"tc_id": "TS-001-TC-02"}, {"tc_id": "TS-001-TC-03"},
        ],
    }]

    result = await agent._execute({"action_mappings": action_mappings, "scenarios": scenarios}, {})
    assert len(result.result["generated_codes"]) == 3
    assert result.result["failed_tcs"] == []
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_execute_partial_failure_skips_only_failed_tc():
    """3 TC 중 가운데 TC LLM JSON parse 실패 → 나머지 2 TC 정상."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TS-001-TC-01"),
        MagicMock(content='{"generated_codes": [{invalid \\escape'),  # JSON parse 실패
        _ok_response("TS-001-TC-03"),
    ])

    action_mappings = [
        {"tc_id": "TS-001-TC-01"}, {"tc_id": "TS-001-TC-02"}, {"tc_id": "TS-001-TC-03"},
    ]
    result = await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})

    generated = result.result["generated_codes"]
    failed = result.result["failed_tcs"]
    assert len(generated) == 2
    assert sorted(c["tc_id"] for c in generated) == ["TS-001-TC-01", "TS-001-TC-03"]
    assert len(failed) == 1
    assert failed[0]["tc_id"] == "TS-001-TC-02"
    assert "JSONDecodeError" in failed[0]["error_type"]
    assert abs(result.confidence - 2/3) < 0.01


@pytest.mark.asyncio
async def test_execute_all_failure_returns_empty_with_failed_tcs():
    """모든 TC 가 JSON parse 실패 → generated 0건 / failed N건. confidence=0."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        MagicMock(content="invalid"),
        MagicMock(content="also invalid"),
    ])
    action_mappings = [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}]
    result = await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})

    assert result.result["generated_codes"] == []
    assert len(result.result["failed_tcs"]) == 2
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_execute_empty_input_returns_clean_result():
    """ActionMapping 비어있으면 LLM 호출 없이 정상 종료."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=AssertionError("LLM 호출 되면 안 됨"))
    result = await agent._execute({"action_mappings": [], "scenarios": []}, {})
    assert result.result["generated_codes"] == []
    assert result.result["failed_tcs"] == []
    assert result.confidence == 1.0
    agent.llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_execute_empty_generated_codes_response_creates_stub():
    """LLM 이 generated_codes=[] 만 반환해도 stub 결과 (code='') 반환 — pipeline 진행."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(return_value=MagicMock(content='{"generated_codes": [], "confidence": 0}'))
    action_mappings = [{"tc_id": "TC-X"}]
    result = await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})
    assert len(result.result["generated_codes"]) == 1
    code_obj = result.result["generated_codes"][0]
    assert code_obj["tc_id"] == "TC-X"
    assert code_obj["code"] == ""
    assert code_obj["syntax_valid"] is False


@pytest.mark.asyncio
async def test_execute_concurrency_respects_semaphore():
    """동시 LLM 호출이 _MAX_CONCURRENT_LLM_CALLS 초과하지 않음."""
    from qapilot.agents.code_generator_agent import _MAX_CONCURRENT_LLM_CALLS

    agent = _make_agent()
    in_flight = 0
    max_in_flight = 0

    async def fake_chat(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        import asyncio
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _ok_response("TC")

    agent.llm.chat = AsyncMock(side_effect=fake_chat)
    action_mappings = [{"tc_id": f"TC-{i}"} for i in range(20)]
    await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})
    assert max_in_flight <= _MAX_CONCURRENT_LLM_CALLS, f"동시 호출 {max_in_flight} > 한도 {_MAX_CONCURRENT_LLM_CALLS}"


# ── 비즈니스 의도 보존 (scenario slice) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_passes_scenario_slice_to_prompt():
    """각 TC LLM 호출에 해당 TC 의 scenario slice 만 전달 (다른 TC 의 비즈니스 의도 누락 X)."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TC-A"), _ok_response("TC-B"),
    ])
    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [
            {"tc_id": "TC-A", "name": "A 시나리오", "given": "GA", "when": "WA", "then": "TA"},
            {"tc_id": "TC-B", "name": "B 시나리오", "given": "GB", "when": "WB", "then": "TB"},
        ],
    }]
    action_mappings = [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}]

    await agent._execute({"action_mappings": action_mappings, "scenarios": scenarios}, {})

    # prompts.render 가 호출된 두 번의 scenarios 인자 검증
    calls = agent.prompts.render.call_args_list
    assert len(calls) == 2
    rendered_scenarios = [json.loads(c.kwargs["scenarios"]) for c in calls]

    # 각 호출이 단일 TC 만 포함
    for sliced in rendered_scenarios:
        assert len(sliced) == 1
        assert len(sliced[0]["test_cases"]) == 1

    # 호출별 TC id 매칭
    tc_ids_in_calls = [s[0]["test_cases"][0]["tc_id"] for s in rendered_scenarios]
    assert sorted(tc_ids_in_calls) == ["TC-A", "TC-B"]
