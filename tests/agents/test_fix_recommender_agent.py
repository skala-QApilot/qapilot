"""FixRecommenderAgent 단위 테스트.

테스트 케이스:
- 정상 케이스: RootCauseResult Top-N을 한 번에 받아 FixResult 반환
- tc_id 유지 확인
- FixSuggestion 기본값 안전 처리 (file_path, line_number 등)
- LLM 파싱 실패 시 fallback suggestion 반환
- suggestion 수 3개 초과 시 trim
- candidates 없을 때 fallback 반환
- LLM confidence 반영 / 없으면 기본값 0.5
- Top-N 전체가 단일 LLM 호출로 처리됨
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qapilot.agents.fix_recommender_agent import FixRecommenderAgent
from qapilot.shared.config import QApilotConfig
from qapilot.shared.errors import AgentExecutionError
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import ExecuteResult

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "fix_recommender"


# ─── 헬퍼 ──────────────────────────────────────────────────────────────────────


def _llm_resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, model="gpt-4o-mini", input_tokens=5, output_tokens=5, cost_usd=0.0, cached=False)


def _make_agent() -> FixRecommenderAgent:
    return FixRecommenderAgent(config=QApilotConfig())


def _mock_chat(*responses: str):
    queue = [_llm_resp(r) for r in responses]
    call_count = 0

    async def _chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        nonlocal call_count
        resp = queue[call_count] if call_count < len(queue) else queue[-1]
        call_count += 1
        return resp

    return _chat


def _minimal_fix_response(tc_id: str = "TC-001", n: int = 1, confidence: float | None = 0.7) -> str:
    suggestions = [
        {
            "file_path": "",
            "line_number": 0,
            "blame_author": None,
            "code_snippet": "",
            "description": f"수정 가이드 {i + 1}",
            "similar_issues": [],
        }
        for i in range(n)
    ]
    data: dict = {"fix_results": [{"tc_id": tc_id, "suggestions": suggestions}]}
    if confidence is not None:
        data["confidence"] = confidence
    return json.dumps(data, ensure_ascii=False)


# ─── Fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture
def root_cause_input() -> dict:
    return json.loads((FIXTURES_DIR / "root_cause_input.json").read_text(encoding="utf-8"))


@pytest.fixture
def llm_response_content() -> str:
    return (FIXTURES_DIR / "llm_response.json").read_text(encoding="utf-8")


# ─── 정상 케이스 ───────────────────────────────────────────────────────────────


async def test_execute_returns_execute_result(root_cause_input, llm_response_content):
    """Top-N candidates를 한 번에 받아 ExecuteResult를 반환하고 fix_results 구조를 만족한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content)

    result = await agent._execute(context={}, params=root_cause_input)

    assert isinstance(result, ExecuteResult)
    fix_results = result.result["fix_results"]
    assert len(fix_results) >= 1

    fr = fix_results[0]
    assert "tc_id" in fr
    assert "suggestions" in fr
    assert isinstance(fr["suggestions"], list)


async def test_fix_result_preserves_tc_id(root_cause_input, llm_response_content):
    """입력 tc_id가 결과 FixResult에 그대로 유지된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content)

    result = await agent._execute(context={}, params=root_cause_input)

    assert result.result["fix_results"][0]["tc_id"] == root_cause_input["tc_id"]


async def test_suggestion_schema_fields_present(root_cause_input, llm_response_content):
    """각 suggestion에 FixSuggestion 스키마 필드가 모두 존재한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content)

    result = await agent._execute(context={}, params=root_cause_input)

    for suggestion in result.result["fix_results"][0]["suggestions"]:
        assert "file_path" in suggestion
        assert "line_number" in suggestion
        assert "blame_author" in suggestion
        assert "code_snippet" in suggestion
        assert "description" in suggestion
        assert "similar_issues" in suggestion


async def test_suggestion_default_values_are_safe():
    """LLM이 미구현 필드를 생략해도 안전한 기본값으로 채운다."""
    agent = _make_agent()
    # file_path, line_number 등 미포함 응답
    minimal = json.dumps({
        "confidence": 0.5,
        "fix_results": [{"tc_id": "TC-001", "suggestions": [
            {"description": "가이드만 있는 suggestion"}
        ]}],
    })
    agent.llm.chat = _mock_chat(minimal)

    result = await agent._execute(context={}, params={"tc_id": "TC-001", "candidates": [
        {"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}
    ]})

    s = result.result["fix_results"][0]["suggestions"][0]
    assert s["file_path"] == ""
    assert s["line_number"] == 0
    assert s["blame_author"] is None
    assert s["code_snippet"] == ""
    assert s["similar_issues"] == []


# ─── 단일 LLM 호출 검증 ────────────────────────────────────────────────────────


async def test_single_llm_call_for_all_candidates():
    """Top-N 전체가 단일 LLM 호출로 처리된다 (candidate별 개별 호출 금지)."""
    agent = _make_agent()
    call_count = 0

    async def counting_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return _llm_resp(_minimal_fix_response())

    agent.llm.chat = counting_chat

    await agent._execute(context={}, params={
        "tc_id": "TC-001",
        "candidates": [
            {"rank": 1, "cause": "원인 1", "confidence": 0.8, "evidences": []},
            {"rank": 2, "cause": "원인 2", "confidence": 0.5, "evidences": []},
            {"rank": 3, "cause": "원인 3", "confidence": 0.3, "evidences": []},
        ],
    })

    assert call_count == 1


# ─── 파싱 실패 → fallback ──────────────────────────────────────────────────────


async def test_parse_failure_returns_fallback_suggestion():
    """LLM 응답이 JSON이 아니면 fallback suggestion을 반환한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat("이것은 JSON이 아닙니다")

    result = await agent._execute(context={}, params={
        "tc_id": "TC-001",
        "candidates": [{"rank": 1, "cause": "원인 A", "confidence": 0.7, "evidences": []}],
    })

    fix_results = result.result["fix_results"]
    assert len(fix_results) >= 1
    assert fix_results[0]["suggestions"][0]["description"]  # 비어 있지 않음


async def test_missing_fix_results_key_returns_fallback():
    """fix_results 키가 없으면 fallback suggestion을 반환한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(json.dumps({"suggestions": []}))

    result = await agent._execute(context={}, params={
        "tc_id": "TC-002",
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}],
    })

    assert result.result["fix_results"][0]["tc_id"] == "TC-002"
    assert len(result.result["fix_results"][0]["suggestions"]) >= 1


# ─── suggestion 수 제한 ────────────────────────────────────────────────────────


async def test_suggestions_trimmed_to_three():
    """LLM이 4개 이상 suggestion을 반환해도 3개까지만 유지한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_minimal_fix_response(n=4))

    result = await agent._execute(context={}, params={
        "tc_id": "TC-001",
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}],
    })

    assert len(result.result["fix_results"][0]["suggestions"]) == 3


# ─── 빈 candidates 처리 ────────────────────────────────────────────────────────


async def test_empty_candidates_returns_fallback_without_llm_call():
    """candidates가 없으면 LLM 호출 없이 fallback FixResult를 반환한다."""
    agent = _make_agent()
    call_count = 0

    async def should_not_be_called(*args, **kwargs) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return _llm_resp("{}")

    agent.llm.chat = should_not_be_called

    result = await agent._execute(context={}, params={"tc_id": "TC-001", "candidates": []})

    assert call_count == 0
    assert result.result["fix_results"][0]["tc_id"] == "TC-001"
    assert len(result.result["fix_results"][0]["suggestions"]) >= 1
    assert result.confidence < 0.5  # 빈 candidates → 낮은 confidence


async def test_empty_candidates_no_tc_id_returns_fallback():
    """tc_id도 없고 candidates도 없어도 안전하게 fallback을 반환한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat("{}")  # 호출되지 않아야 함

    result = await agent._execute(context={}, params={})

    assert "fix_results" in result.result
    assert isinstance(result.result["fix_results"], list)


# ─── confidence 계산 ───────────────────────────────────────────────────────────


async def test_confidence_from_llm():
    """LLM이 confidence를 반환하면 그 값을 사용한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_minimal_fix_response(confidence=0.85))

    result = await agent._execute(context={}, params={
        "tc_id": "TC-001",
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}],
    })

    assert result.confidence == pytest.approx(0.85)


async def test_confidence_default_when_llm_omits_it():
    """LLM이 confidence를 반환하지 않으면 기본값 0.5를 사용한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_minimal_fix_response(confidence=None))

    result = await agent._execute(context={}, params={
        "tc_id": "TC-001",
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.3, "evidences": []}],
    })

    assert result.confidence == pytest.approx(0.5)


async def test_confidence_clamped_to_valid_range():
    """LLM이 범위 밖의 confidence를 반환해도 0.0~1.0으로 clamp된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_minimal_fix_response(confidence=1.5))

    result = await agent._execute(context={}, params={
        "tc_id": "TC-001",
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}],
    })

    assert 0.0 <= result.confidence <= 1.0



# ─── 코드 인덱스 로딩 ─────────────────────────────────────────────────────────


async def test_code_context_loaded_from_tc_id_fixture():
    """tc_id에 해당하는 코드 인덱스 파일이 있으면 프롬프트에 반영된다."""
    agent = _make_agent()
    captured: dict = {}

    async def mock_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        captured["user"] = user_prompt
        return _llm_resp(_minimal_fix_response())

    agent.llm.chat = mock_chat

    await agent._execute(context={}, params={
        "tc_id": "TC-001",  # tests/fixtures/contexts/TC-001/code_context.json 존재
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}],
    })

    # code_context.json의 파일 경로가 프롬프트에 포함되어야 한다
    assert "payment_service" in captured["user"]


async def test_code_context_fallback_when_no_fixture():
    """tc_id에 해당하는 코드 인덱스 파일이 없으면 대체 문구로 처리한다."""
    agent = _make_agent()
    captured: dict = {}

    async def mock_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        captured["user"] = user_prompt
        return _llm_resp(_minimal_fix_response())

    agent.llm.chat = mock_chat

    await agent._execute(context={}, params={
        "tc_id": "TC-NONEXISTENT",
        "candidates": [{"rank": 1, "cause": "원인", "confidence": 0.5, "evidences": []}],
    })

    assert "코드 인덱스 없음" in captured["user"]


# ─── _parse_response 직접 테스트 ───────────────────────────────────────────────


def test_parse_response_raises_on_invalid_json():
    """_parse_response는 JSON 파싱 실패 시 AgentExecutionError를 발생시킨다."""
    agent = _make_agent()
    with pytest.raises(AgentExecutionError):
        agent._parse_response("not json", tc_id="TC-001")


def test_parse_response_raises_on_missing_fix_results_key():
    """_parse_response는 fix_results 키 누락 시 AgentExecutionError를 발생시킨다."""
    agent = _make_agent()
    with pytest.raises(AgentExecutionError):
        agent._parse_response(json.dumps({"suggestions": []}), tc_id="TC-001")


# ─── _normalize_suggestion 직접 테스트 ────────────────────────────────────────


def test_normalize_suggestion_fills_defaults():
    """_normalize_suggestion은 누락된 필드를 안전한 기본값으로 채운다."""
    agent = _make_agent()
    result = agent._normalize_suggestion({"description": "수정 가이드"})

    assert result["file_path"] == ""
    assert result["line_number"] == 0
    assert result["blame_author"] is None
    assert result["code_snippet"] == ""
    assert result["similar_issues"] == []
    assert result["description"] == "수정 가이드"


def test_normalize_suggestion_preserves_provided_values():
    """_normalize_suggestion은 제공된 값을 그대로 유지한다."""
    agent = _make_agent()
    raw = {
        "file_path": "app/service.py",
        "line_number": 42,
        "blame_author": None,
        "code_snippet": "db.commit()",
        "description": "커밋 추가",
        "similar_issues": ["ISSUE-123"],
    }
    result = agent._normalize_suggestion(raw)

    assert result["file_path"] == "app/service.py"
    assert result["line_number"] == 42
    assert result["code_snippet"] == "db.commit()"
    assert result["similar_issues"] == ["ISSUE-123"]
