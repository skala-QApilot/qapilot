"""RootCauseAgent 단위 테스트.

테스트 케이스:
- 정상 케이스: RootCauseResult 구조(tc_id + candidates) 반환
- confidence 보정: code_location +0.2, runtime_data +0.2, 둘 다 +0.4 (최대 1.0)
- summary 빈값 시 mismatches fallback
- LLM 응답 파싱 실패 시 fallback candidate 반환 (예외 없음)
- candidate 3개 초과 시 3개까지 trim
- 잘못된 evidence type 제거
- Judge 실패 시 max candidate confidence fallback
- tc_id 있는데 context 없으면 warnings 포함
- _select_relevant_code_context 키워드 필터링
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qapilot.agents.root_cause_agent import RootCauseAgent
from qapilot.shared.config import QApilotConfig
from qapilot.shared.errors import AgentExecutionError
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import ExecuteResult

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "root_cause"

# Judge 정상 응답 (relevance=4, evidence_quality=4, diversity=3)
_JUDGE_OK = json.dumps(
    {"relevance": 4, "evidence_quality": 4, "diversity": 3, "reason": "분석 적절"}
)


# ─── 헬퍼 ──────────────────────────────────────────────────────────────────────


def _llm_resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, model="gpt-4o-mini", tokens_used=10, cached=False)


def _make_agent() -> RootCauseAgent:
    return RootCauseAgent(config=QApilotConfig())


def _mock_chat(*responses: str):
    """호출 순서대로 다른 LLMResponse를 반환하는 async callable."""
    queue = [_llm_resp(r) for r in responses]
    call_count = 0

    async def _chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        nonlocal call_count
        resp = queue[call_count] if call_count < len(queue) else queue[-1]
        call_count += 1
        return resp

    return _chat


def _single_candidate_llm(confidence: float = 0.5, evidences: list | None = None) -> str:
    return json.dumps({"root_causes": [
        {"rank": 1, "cause": "테스트 원인", "confidence": confidence,
         "evidences": evidences if evidences is not None else []}
    ]})


# ─── Fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture
def cross_check_input() -> dict:
    return json.loads((FIXTURES_DIR / "cross_check_input.json").read_text(encoding="utf-8"))


@pytest.fixture
def llm_response_content() -> str:
    return (FIXTURES_DIR / "llm_response.json").read_text(encoding="utf-8")


@pytest.fixture
def code_context() -> dict:
    return json.loads((FIXTURES_DIR / "code_context.json").read_text(encoding="utf-8"))


@pytest.fixture
def runtime_context() -> dict:
    return json.loads((FIXTURES_DIR / "runtime_context.json").read_text(encoding="utf-8"))


# ─── 반환 구조 검증 ────────────────────────────────────────────────────────────


async def test_result_has_root_cause_result_structure(
    cross_check_input, llm_response_content, code_context, runtime_context
):
    """결과는 RootCauseResult 구조(tc_id + candidates)를 가져야 한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content, _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={
            **cross_check_input,
            "code_context": json.dumps(code_context, ensure_ascii=False),
            "runtime_context": json.dumps(runtime_context, ensure_ascii=False),
        },
    )

    assert isinstance(result, ExecuteResult)
    root_causes = result.result["root_causes"]
    assert len(root_causes) == 1

    rc = root_causes[0]
    assert rc["tc_id"] == cross_check_input["tc_id"]
    assert "candidates" in rc
    assert 1 <= len(rc["candidates"]) <= 3

    for c in rc["candidates"]:
        assert "rank" in c
        assert "cause" in c
        assert 0.0 <= c["confidence"] <= 1.0
        assert "evidences" in c


async def test_result_has_no_warning_field_when_contexts_provided(
    cross_check_input, llm_response_content, code_context, runtime_context
):
    """컨텍스트가 모두 주어지면 result에 warnings 필드가 없다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content, _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={
            **cross_check_input,
            "code_context": json.dumps(code_context, ensure_ascii=False),
            "runtime_context": json.dumps(runtime_context, ensure_ascii=False),
        },
    )

    assert "warnings" not in result.result


# ─── agent-level confidence ────────────────────────────────────────────────────


async def test_agent_confidence_from_judge(llm_response_content):
    """Judge 점수로 agent-level confidence를 계산한다.

    relevance=4, evidence_quality=4, diversity=4 → avg=4 → (4-1)/4 = 0.75
    """
    agent = _make_agent()
    judge_resp = json.dumps(
        {"relevance": 4, "evidence_quality": 4, "diversity": 4, "reason": "ok"}
    )
    agent.llm.chat = _mock_chat(llm_response_content, judge_resp)

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.confidence == pytest.approx(0.75)


# ─── confidence 보정 케이스 ────────────────────────────────────────────────────


async def test_confidence_boost_code_location():
    """code_location evidence 1개 이상이면 +0.2."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        _single_candidate_llm(0.5, [{"type": "code_location", "content": "app/service.py:84"}]),
        _JUDGE_OK,
    )

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.7)


async def test_confidence_boost_runtime_data():
    """runtime_data evidence 1개 이상이면 +0.2."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        _single_candidate_llm(0.5, [{"type": "runtime_data", "content": "HTTP 500"}]),
        _JUDGE_OK,
    )

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.7)


async def test_confidence_boost_both_types_capped_at_one():
    """code_location + runtime_data 둘 다 있으면 +0.4, 1.0 초과 금지."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        _single_candidate_llm(0.9, [
            {"type": "code_location", "content": "app/service.py:10"},
            {"type": "runtime_data", "content": "TimeoutError"},
        ]),
        _JUDGE_OK,
    )

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(1.0)


async def test_confidence_without_evidence_stays_low():
    """evidence가 없으면 LLM confidence 그대로 유지된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(0.3, []), _JUDGE_OK)

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.3)


# ─── summary fallback ──────────────────────────────────────────────────────────


async def test_summary_fallback_from_mismatches():
    """summary가 비어 있을 때 mismatches 기반 fallback이 프롬프트에 포함된다."""
    agent = _make_agent()
    call_count = 0
    captured_user_prompt: str = ""

    async def mock_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        nonlocal call_count, captured_user_prompt
        call_count += 1
        if call_count == 1:
            captured_user_prompt = user_prompt
            return _llm_resp(_single_candidate_llm())
        return _llm_resp(_JUDGE_OK)

    agent.llm.chat = mock_chat

    await agent._execute(
        context={},
        params={
            "tc_id": "TC-001",
            "summary": "",
            "mismatches": [
                {
                    "field": "status",
                    "ui_value": "ok",
                    "api_value": "fail",
                    "db_value": None,
                    "severity": "critical",
                }
            ],
        },
    )

    assert "status" in captured_user_prompt
    assert "ok" in captured_user_prompt or "fail" in captured_user_prompt


# ─── 파싱 실패 → fallback ──────────────────────────────────────────────────────


async def test_parse_failure_returns_fallback_result():
    """LLM 응답이 JSON이 아니면 예외 없이 fallback candidate를 반환한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat("이것은 JSON이 아닙니다", _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-001", "error_code": "HTTP_500", "summary": "오류 발생"},
    )

    candidates = result.result["root_causes"][0]["candidates"]
    assert len(candidates) >= 1
    assert candidates[0]["confidence"] < 0.4  # fallback은 0.1 기반 (evidence boost 포함해도 낮음)
    assert "warnings" not in result.result  # warnings 필드 없음, cause에 상황 표현


async def test_missing_root_causes_key_returns_fallback_result():
    """root_causes 키가 없으면 예외 없이 fallback candidate를 반환한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(json.dumps({"candidates": []}), _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-001", "error_code": "DB_ERROR"},
    )

    candidates = result.result["root_causes"][0]["candidates"]
    assert len(candidates) >= 1
    assert candidates[0]["confidence"] < 0.4  # fallback은 0.1 기반 (evidence boost 포함해도 낮음)


async def test_parse_response_raises_on_invalid_json():
    """_parse_response는 JSON 파싱 실패 시 AgentExecutionError를 발생시킨다."""
    agent = _make_agent()
    with pytest.raises(AgentExecutionError):
        agent._parse_response("not json at all")


async def test_parse_response_raises_on_missing_root_causes_key():
    """_parse_response는 root_causes 키 누락 시 AgentExecutionError를 발생시킨다."""
    agent = _make_agent()
    with pytest.raises(AgentExecutionError):
        agent._parse_response(json.dumps({"candidates": []}))


# ─── candidate 수 제한 ─────────────────────────────────────────────────────────


async def test_candidates_trimmed_to_three():
    """LLM이 4개 이상 반환해도 3개까지만 유지한다."""
    agent = _make_agent()
    root_causes = json.dumps({"root_causes": [
        {"rank": 1, "cause": "원인 1", "confidence": 0.8, "evidences": []},
        {"rank": 2, "cause": "원인 2", "confidence": 0.6, "evidences": []},
        {"rank": 3, "cause": "원인 3", "confidence": 0.4, "evidences": []},
        {"rank": 4, "cause": "원인 4", "confidence": 0.3, "evidences": []},
    ]})
    agent.llm.chat = _mock_chat(root_causes, _JUDGE_OK)

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert len(result.result["root_causes"][0]["candidates"]) == 3


# ─── evidence type 검증 ────────────────────────────────────────────────────────


async def test_invalid_evidence_type_removed():
    """evidence type이 잘못되면 해당 항목을 제거한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        _single_candidate_llm(0.5, [
            {"type": "unknown_type", "content": "제거될 항목"},
            {"type": "code_location", "content": "유효한 항목"},
        ]),
        _JUDGE_OK,
    )

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    evidences = result.result["root_causes"][0]["candidates"][0]["evidences"]
    assert len(evidences) == 1
    assert evidences[0]["type"] == "code_location"


async def test_all_invalid_evidences_result_in_empty_list():
    """모든 evidence type이 잘못되면 evidences가 빈 리스트가 된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        _single_candidate_llm(0.5, [
            {"type": "git_blame", "content": "잘못된 타입"},
            {"type": "log_trace", "content": "잘못된 타입"},
        ]),
        _JUDGE_OK,
    )

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["evidences"] == []


# ─── Judge 실패 fallback ───────────────────────────────────────────────────────


async def test_judge_failure_fallback_to_max_candidate_confidence():
    """Judge 호출 실패(파싱 불가) 시 candidate 최고 confidence를 agent confidence로 사용한다."""
    agent = _make_agent()
    root_causes = json.dumps({"root_causes": [
        {"rank": 1, "cause": "원인 1", "confidence": 0.7, "evidences": []},
        {"rank": 2, "cause": "원인 2", "confidence": 0.4, "evidences": []},
    ]})
    agent.llm.chat = _mock_chat(root_causes, "judge 응답 파싱 불가 텍스트")

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.confidence == pytest.approx(0.7)


async def test_judge_failure_missing_fields_fallback():
    """Judge 응답에 필드가 누락되면 fallback을 사용한다."""
    agent = _make_agent()
    root_causes = json.dumps({"root_causes": [
        {"rank": 1, "cause": "원인", "confidence": 0.6, "evidences": []},
    ]})
    agent.llm.chat = _mock_chat(root_causes, json.dumps({"relevance": 3}))

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.confidence == pytest.approx(0.6)


# ─── tc_id context 없을 때 candidate 처리 ─────────────────────────────────────


async def test_both_contexts_missing_forces_confidence_zero():
    """code_context, runtime_context 모두 없으면 모든 candidate confidence가 0.0이 된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(0.8), _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-NONEXISTENT"},
    )

    candidates = result.result["root_causes"][0]["candidates"]
    for c in candidates:
        assert c["confidence"] == 0.0


async def test_both_contexts_missing_adds_note_evidence():
    """context가 모두 없으면 각 candidate에 '추론 근거 제한' note evidence가 추가된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(0.8), _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-NONEXISTENT"},
    )

    candidates = result.result["root_causes"][0]["candidates"]
    for c in candidates:
        note_evidences = [e for e in c["evidences"] if "summary만으로 추론" in e.get("content", "")]
        assert len(note_evidences) >= 1
        assert note_evidences[0]["type"] == "runtime_data"


async def test_both_contexts_missing_no_warnings_field():
    """context 미발견 시 warnings 필드 없이 candidate에 상황을 표현한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(), _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-NONEXISTENT"},
    )

    assert "warnings" not in result.result


async def test_no_confidence_override_when_tc_id_empty():
    """tc_id가 비어 있으면 context 미발견 처리를 하지 않아 confidence가 정상값이다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(0.7), _JUDGE_OK)

    result = await agent._execute(context={}, params={})

    candidates = result.result["root_causes"][0]["candidates"]
    assert candidates[0]["confidence"] > 0.0  # 0.0으로 강제되지 않음


# ─── 관련 코드 컨텍스트 필터링 ────────────────────────────────────────────────


def test_select_relevant_code_context_filters_by_keyword():
    """error_code 키워드와 일치하는 파일만 반환한다."""
    code_ctx = json.dumps({
        "files": [
            {"path": "app/payment_service.py", "note": "payment processing"},
            {"path": "app/user_service.py", "note": "user management"},
        ]
    })

    filtered = RootCauseAgent._select_relevant_code_context(
        code_ctx, error_code="PAYMENT_FAILED", summary="", mismatches=[]
    )

    data = json.loads(filtered)
    assert len(data["files"]) == 1
    assert "payment" in data["files"][0]["path"]


def test_select_relevant_code_context_no_keywords_returns_full():
    """키워드가 전혀 없으면 전체 컨텍스트를 그대로 반환한다."""
    code_ctx = json.dumps({
        "files": [
            {"path": "app/service.py", "note": "some service"},
            {"path": "app/other.py", "note": "other stuff"},
        ]
    })

    filtered = RootCauseAgent._select_relevant_code_context(
        code_ctx, error_code="", summary="", mismatches=[]
    )

    assert json.loads(filtered) == json.loads(code_ctx)


def test_select_relevant_code_context_no_match_returns_full():
    """키워드가 있지만 일치하는 파일이 없으면 전체를 반환한다 (안전 fallback)."""
    code_ctx = json.dumps({
        "files": [
            {"path": "app/service.py", "note": "generic service"},
        ]
    })

    filtered = RootCauseAgent._select_relevant_code_context(
        code_ctx, error_code="UNKNOWN_XYZ", summary="", mismatches=[]
    )

    assert json.loads(filtered) == json.loads(code_ctx)


def test_select_relevant_code_context_mismatch_field_keyword():
    """mismatches field 이름도 키워드로 사용된다."""
    code_ctx = json.dumps({
        "files": [
            {"path": "app/order_service.py", "note": "order processing"},
            {"path": "app/auth_service.py", "note": "authentication"},
        ]
    })

    filtered = RootCauseAgent._select_relevant_code_context(
        code_ctx,
        error_code="",
        summary="",
        mismatches=[{"field": "order_status", "ui_value": "x", "api_value": "y",
                     "db_value": None, "severity": "high"}],
    )

    data = json.loads(filtered)
    assert len(data["files"]) == 1
    assert "order" in data["files"][0]["path"]


def test_select_relevant_code_context_plain_string():
    """plain 문자열 컨텍스트는 줄 단위로 필터링한다."""
    code_ctx = (
        "app/payment_service.py:84 - commit 누락\n"
        "app/user_service.py:10 - 일반 로직\n"
        "app/payment_service.py:90 - rollback 처리\n"
    )

    filtered = RootCauseAgent._select_relevant_code_context(
        code_ctx, error_code="PAYMENT_ERROR", summary="", mismatches=[]
    )

    lines = [l for l in filtered.splitlines() if l.strip()]
    assert all("payment" in l.lower() for l in lines)
    assert len(lines) == 2
