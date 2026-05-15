"""RootCauseAgent 단위 테스트.

cause별 confidence (RootCauseCandidate.confidence):
- LLM 기본값 + 관련성 페널티(-0.4) + evidence 보정(code_location +0.3, runtime_data +0.3)

응답 전체 confidence (ExecuteResult.confidence):
- LLM-as-Judge: relevance / diversity / ranking_validity / evidence_quality 4개 지표 평균
  → (avg - 1) / 4 로 0.0~1.0 정규화
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from qapilot.agents.root_cause_agent import RootCauseAgent
from qapilot.shared.codebase_context_loader import CodebaseContextLoader
from qapilot.shared.config import ProjectConfig, QApilotConfig
from qapilot.shared.errors import AgentExecutionError
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import ExecuteResult

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "root_cause"

# Judge 정상 응답 — 4개 지표 포함
_JUDGE_OK = json.dumps({
    "relevance": 4,
    "diversity": 3,
    "ranking_validity": 4,
    "evidence_quality": 4,
    "reason": "분석 적절",
})


# ─── 헬퍼 ──────────────────────────────────────────────────────────────────────


def _llm_resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, model="gpt-4o-mini", input_tokens=5, output_tokens=5, cost_usd=0.0, cached=False)


def _make_agent() -> RootCauseAgent:
    return RootCauseAgent(config=QApilotConfig())


_CLUE_EMPTY = json.dumps(
    {"endpoints": [], "files": [], "functions": [], "models": [], "keywords": []}
)

def _mock_chat(*responses: str):
    """호출 순서대로 다른 LLMResponse를 반환하는 async callable.

    단서 추출 호출(system_prompt에 '단서 추출' 포함)은 큐를 소비하지 않고
    빈 단서 JSON을 반환해 기존 테스트 순서를 유지한다.
    """
    queue = [_llm_resp(r) for r in responses]
    call_count = 0

    async def _chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        nonlocal call_count
        if "단서 추출" in system_prompt:          # _extract_clues_with_llm 호출
            return _llm_resp(_CLUE_EMPTY)
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



# ─── 반환 구조 검증 ────────────────────────────────────────────────────────────


async def test_result_has_root_cause_result_structure(
    cross_check_input, llm_response_content
):
    """결과는 RootCauseResult 구조(tc_id + candidates)를 가져야 한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content, _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={**cross_check_input},
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
    cross_check_input, llm_response_content
):
    """결과에 warnings 필드가 없다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content, _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={**cross_check_input},
    )

    assert "warnings" not in result.result


# ─── 응답 전체 confidence (Judge 기반) ────────────────────────────────────────


async def test_agent_confidence_from_judge(llm_response_content):
    """Judge 4개 지표 평균으로 agent-level confidence를 계산한다.

    relevance=4, diversity=4, ranking_validity=4, evidence_quality=4
    → avg=4 → (4-1)/4 = 0.75
    """
    agent = _make_agent()
    judge_resp = json.dumps({
        "relevance": 4, "diversity": 4, "ranking_validity": 4,
        "evidence_quality": 4, "reason": "ok",
    })
    agent.llm.chat = _mock_chat(llm_response_content, judge_resp)

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.confidence == pytest.approx(0.75)


async def test_judge_four_criteria_max_score():
    """Judge 4개 지표 모두 5점이면 agent confidence = 1.0."""
    agent = _make_agent()
    judge_resp = json.dumps({
        "relevance": 5, "diversity": 5, "ranking_validity": 5,
        "evidence_quality": 5, "reason": "perfect",
    })
    agent.llm.chat = _mock_chat(_single_candidate_llm(), judge_resp)

    result = await agent._execute(context={}, params={})

    assert result.confidence == pytest.approx(1.0)


async def test_judge_failure_fallback_to_max_candidate_confidence():
    """Judge 호출 실패 시 rule-base로 계산된 candidate 최고 confidence를 사용한다."""
    agent = _make_agent()
    root_causes = json.dumps({"root_causes": [
        {"rank": 1, "cause": "원인 1", "confidence": 0.7, "evidences": []},
        {"rank": 2, "cause": "원인 2", "confidence": 0.4, "evidences": []},
    ]})
    agent.llm.chat = _mock_chat(root_causes, "judge 응답 파싱 불가 텍스트")

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    # LLM confidence 무시 → rule-base 고정 base 0.5, evidence/penalty 없음 → 0.5
    assert result.confidence == pytest.approx(0.5)


async def test_judge_failure_missing_fields_fallback():
    """Judge 응답에 필드가 누락되면 fallback을 사용한다."""
    agent = _make_agent()
    root_causes = json.dumps({"root_causes": [
        {"rank": 1, "cause": "원인", "confidence": 0.6, "evidences": []},
    ]})
    agent.llm.chat = _mock_chat(root_causes, json.dumps({"relevance": 3}))

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    # LLM confidence 무시 → rule-base 0.5
    assert result.confidence == pytest.approx(0.5)


# ─── cause별 confidence — evidence 보정 ────────────────────────────────────────


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


async def test_confidence_boost_both_types():
    """code_location + runtime_data 둘 다 있으면 base 0.5 + 0.4 = 0.9."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        _single_candidate_llm(0.9, [
            {"type": "code_location", "content": "app/service.py:10"},
            {"type": "runtime_data", "content": "TimeoutError"},
        ]),
        _JUDGE_OK,
    )

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.9)


async def test_confidence_without_evidence_uses_rule_base():
    """evidence가 없으면 rule-base 고정값 0.5가 그대로 사용된다. LLM confidence는 무시."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(0.3, []), _JUDGE_OK)

    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.5)


# ─── cause별 confidence — 관련성 페널티 ───────────────────────────────────────


async def test_relevance_penalty_when_cause_unrelated():
    """cause에 입력 키워드가 전혀 없으면 -0.4 페널티를 적용한다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        json.dumps({"root_causes": [
            {"rank": 1, "cause": "완전히 무관한 원인 설명", "confidence": 0.8, "evidences": []}
        ]}),
        _JUDGE_OK,
    )

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-001", "error_code": "HTTP_500", "summary": "결제 실패"},
    )

    # base 0.5 (LLM confidence 무시), 입력 토큰이 cause에 없음 → -0.4
    # 0.5 - 0.4 = 0.1
    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.1)


async def test_no_relevance_penalty_when_cause_matches():
    """cause에 입력 키워드가 포함되면 페널티를 적용하지 않는다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        json.dumps({"root_causes": [
            {"rank": 1, "cause": "결제 처리 중 HTTP_500 오류 발생", "confidence": 0.5, "evidences": []}
        ]}),
        _JUDGE_OK,
    )

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-001", "error_code": "HTTP_500", "summary": "결제 실패"},
    )

    # base 0.5 (LLM confidence 무시), 입력 토큰이 cause에 포함됨 → 페널티 없음 → 0.5
    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.5)


async def test_no_relevance_penalty_when_no_input_tokens():
    """error_code, summary, mismatches가 없으면 관련성 페널티를 적용하지 않는다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(_single_candidate_llm(0.8), _JUDGE_OK)

    # error_code, summary, mismatches 모두 없음
    result = await agent._execute(context={}, params={"tc_id": "TC-001"})

    # base 0.5 (LLM confidence 무시), 입력 토큰 없음 → 페널티 없음 → 0.5
    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.5)


async def test_relevance_penalty_combined_with_evidence_boost():
    """관련성 페널티와 evidence 보정이 함께 적용된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        json.dumps({"root_causes": [
            {"rank": 1, "cause": "무관한 원인 설명", "confidence": 0.8,
             "evidences": [{"type": "code_location", "content": "app/x.py:1"}]}
        ]}),
        _JUDGE_OK,
    )

    result = await agent._execute(
        context={},
        params={"tc_id": "TC-001", "error_code": "HTTP_500", "summary": "결제 실패"},
    )

    # base 0.5 - 0.4 (관련성 페널티) + 0.2 (code_location) = 0.3
    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.3)


async def test_relevance_check_uses_mismatch_fields():
    """mismatches의 field 이름도 관련성 키워드로 활용된다."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(
        json.dumps({"root_causes": [
            {"rank": 1, "cause": "order_status 불일치 발생", "confidence": 0.6, "evidences": []}
        ]}),
        _JUDGE_OK,
    )

    result = await agent._execute(
        context={},
        params={
            "tc_id": "TC-001",
            "mismatches": [{"field": "order_status", "ui_value": "ok",
                            "api_value": "fail", "db_value": None, "severity": "high"}],
        },
    )

    # base 0.5, "order" 또는 "status"가 cause에 포함됨 → 페널티 없음 → 0.5
    assert result.result["root_causes"][0]["candidates"][0]["confidence"] == pytest.approx(0.5)


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
    assert candidates[0]["confidence"] < 0.5  # fallback은 낮은 confidence
    assert "warnings" not in result.result


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
    assert candidates[0]["confidence"] < 0.5


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
    assert candidates[0]["confidence"] > 0.0



# ─── CodebaseContextLoader 테스트 ──────────────────────────────────────────────


def _make_index_dir(tmp_path: Path) -> Path:
    """tmp_path 아래 .qapilot/codebase-index/ 를 생성하고 반환한다."""
    d = tmp_path / ".qapilot" / "codebase-index"
    d.mkdir(parents=True)
    return d


def test_loader_loads_all_four_files(tmp_path):
    """모든 4개 JSON 파일을 올바르게 로드한다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text('[{"path": "/test", "method": "GET"}]')
    (d / "models.json").write_text('[{"name": "TestModel", "fields": ["id"]}]')
    (d / "callgraph.json").write_text('{"file.py": ["dep.py"]}')
    (d / "manifest.json").write_text('{"language": "python", "framework": "fastapi"}')

    result = CodebaseContextLoader.load(base_dir=tmp_path)

    assert result["_dir_found"] is True
    assert result["endpoints"] == [{"path": "/test", "method": "GET"}]
    assert result["models"] == [{"name": "TestModel", "fields": ["id"]}]
    assert result["callgraph"] == {"file.py": ["dep.py"]}
    assert result["manifest"] == {"language": "python", "framework": "fastapi"}


def test_loader_missing_files_return_empty_defaults(tmp_path):
    """일부 파일이 없어도 빈 기본값으로 안전하게 처리한다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text('[{"path": "/only"}]')
    # models, callgraph, manifest 없음

    result = CodebaseContextLoader.load(base_dir=tmp_path)

    assert result["_dir_found"] is True
    assert result["endpoints"] == [{"path": "/only"}]
    assert result["models"] == []
    assert result["callgraph"] == {}
    assert result["manifest"] == {}


def test_loader_missing_directory_returns_dir_not_found(tmp_path):
    """인덱스 디렉토리 자체가 없으면 _dir_found=False와 빈 구조를 반환한다."""
    result = CodebaseContextLoader.load(base_dir=tmp_path)

    assert result["_dir_found"] is False
    assert result["endpoints"] == []
    assert result["models"] == []
    assert result["callgraph"] == {}
    assert result["manifest"] == {}


def test_loader_default_base_dir_does_not_crash():
    """base_dir 미지정 시 현재 디렉토리 기준으로 동작하며 예외가 없다."""
    result = CodebaseContextLoader.load()  # base_dir=None → Path(".")
    assert isinstance(result, dict)
    assert "_dir_found" in result


# ─── RootCauseAgent + codebase-index 연동 테스트 ─────────────────────────────


def _agent_with_index(tmp_path: Path) -> RootCauseAgent:
    """repo_path가 tmp_path로 설정된 에이전트를 반환한다."""
    return RootCauseAgent(
        config=QApilotConfig(project=ProjectConfig(repo_path=str(tmp_path)))
    )


def _write_full_index(index_dir: Path) -> None:
    """테스트용 기본 인덱스 파일 세트를 생성한다."""
    (index_dir / "endpoints.json").write_text(json.dumps([
        {"file": "app/routers/orders.py", "method": "POST", "path": "/orders", "handler": "create_order"},
        {"file": "app/routers/auth.py", "method": "POST", "path": "/login", "handler": "login"},
    ]))
    (index_dir / "models.json").write_text(json.dumps([
        {"file": "app/schemas.py", "name": "OrderOut", "fields": ["id", "status", "total_amount"]},
        {"file": "app/schemas.py", "name": "LoginRequest", "fields": ["email", "password"]},
    ]))
    (index_dir / "callgraph.json").write_text(json.dumps({
        "app/routers/orders.py": ["app/schemas.py", "app/models.py"],
        "app/routers/auth.py": ["app/schemas.py"],
    }))
    (index_dir / "manifest.json").write_text(json.dumps({
        "language": "python",
        "framework": "fastapi",
        "file_count": 10,
        "endpoint_count": 5,
        "scan_timestamp": "2026-05-15T00:00:00Z",
        "commit_hash": "abc123",
    }))


async def test_codebase_index_injected_into_prompt_when_no_code_context(tmp_path):
    """code_context가 없으면 codebase-index 내용이 LLM 프롬프트에 포함된다."""
    d = _make_index_dir(tmp_path)
    _write_full_index(d)

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat

    await agent._execute(
        context={},
        params={"error_code": "ORDER_ERROR", "summary": "주문 처리 실패"},
    )

    assert captured, "LLM이 호출되지 않았습니다"
    prompt = captured[0]
    assert "codebase-index" in prompt
    assert "create_order" in prompt  # order 토큰으로 orders 엔드포인트 선택됨
    assert "OrderOut" in prompt       # order 토큰으로 OrderOut 모델 선택됨


async def test_error_code_summary_based_filtering(tmp_path):
    """error_code/summary 토큰과 매칭되는 항목만 선별된다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text(json.dumps([
        {"file": "payment.py", "method": "POST", "path": "/pay", "handler": "process_payment"},
        {"file": "auth.py", "method": "GET", "path": "/profile", "handler": "get_profile"},
    ]))
    (d / "models.json").write_text("[]")
    (d / "callgraph.json").write_text("{}")
    (d / "manifest.json").write_text('{"language": "python"}')

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat
    await agent._execute(
        context={},
        params={"error_code": "PAYMENT_FAILED", "summary": "결제 실패"},
    )

    prompt = captured[0]
    assert "process_payment" in prompt   # payment 토큰 매칭
    assert "get_profile" not in prompt   # 무관 항목 제외됨


async def test_manifest_includes_only_summary_fields(tmp_path):
    """manifest는 framework/language/file_count/endpoint_count 필드만 포함한다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text("[]")
    (d / "models.json").write_text("[]")
    (d / "callgraph.json").write_text("{}")
    (d / "manifest.json").write_text(json.dumps({
        "framework": "fastapi",
        "language": "python",
        "file_count": 62,
        "endpoint_count": 33,
        "scan_timestamp": "2026-05-15T00:00:00Z",  # 제외 대상
        "commit_hash": "abc123deadbeef",             # 제외 대상
    }))

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat
    await agent._execute(context={}, params={"error_code": "ANY_ERROR"})

    prompt = captured[0]
    assert "fastapi" in prompt
    assert "python" in prompt
    assert "scan_timestamp" not in prompt
    assert "abc123deadbeef" not in prompt


async def test_no_match_fallback_to_manifest_only(tmp_path):
    """관련 항목이 없으면 manifest만 포함한 컨텍스트로 LLM을 호출한다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text(json.dumps([
        {"file": "x.py", "method": "GET", "path": "/xyz", "handler": "xyz_handler"},
    ]))
    (d / "models.json").write_text("[]")
    (d / "callgraph.json").write_text("{}")
    (d / "manifest.json").write_text('{"language": "python", "framework": "fastapi"}')

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat
    await agent._execute(
        context={},
        params={"error_code": "TOTALLY_UNRELATED_CODE_ZZZ"},
    )

    prompt = captured[0]
    assert "fastapi" in prompt    # manifest는 포함됨
    assert "xyz_handler" not in prompt  # 매칭 안 된 항목은 제외됨


async def test_missing_index_logs_warning(tmp_path):
    """codebase-index 디렉토리가 없으면 codebase_index_not_found warning을 남긴다."""
    # tmp_path에 .qapilot/codebase-index/ 없음
    agent = _agent_with_index(tmp_path)
    agent.llm.chat = _mock_chat(_single_candidate_llm(), _JUDGE_OK)
    mock_logger = MagicMock()
    agent.logger = mock_logger

    await agent._execute(context={}, params={})

    warning_events = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert "codebase_index_not_found" in warning_events


async def test_selection_start_and_result_logged(tmp_path):
    """codebase-index 선별 시 selection_start / selection_result 로그가 남는다."""
    d = _make_index_dir(tmp_path)
    _write_full_index(d)

    agent = _agent_with_index(tmp_path)
    agent.llm.chat = _mock_chat(_single_candidate_llm(), _JUDGE_OK)
    mock_logger = MagicMock()
    agent.logger = mock_logger

    await agent._execute(
        context={},
        params={"error_code": "ORDER_ERROR", "summary": "주문 실패"},
    )

    info_events = [call.args[0] for call in mock_logger.info.call_args_list]
    assert "codebase_index_selection_start" in info_events
    assert "codebase_index_selection_result" in info_events


async def test_no_relevant_match_logs_warning(tmp_path):
    """토큰 매칭 항목이 없으면 codebase_index_no_relevant_match warning을 남긴다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text(json.dumps([
        {"file": "x.py", "method": "GET", "path": "/xyz", "handler": "xyz_handler"},
    ]))
    (d / "models.json").write_text("[]")
    (d / "callgraph.json").write_text("{}")
    (d / "manifest.json").write_text('{"language": "python"}')

    agent = _agent_with_index(tmp_path)
    agent.llm.chat = _mock_chat(_single_candidate_llm(), _JUDGE_OK)
    mock_logger = MagicMock()
    agent.logger = mock_logger

    await agent._execute(
        context={},
        params={"error_code": "TOTALLY_UNRELATED_ZZZ_999"},
    )

    warning_events = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert "codebase_index_no_relevant_match" in warning_events


async def test_index_always_runs_regardless_of_direct_code_context(tmp_path):
    """codebase-index 탐색은 code_context 제공 여부와 무관하게 항상 실행된다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text(json.dumps([
        {"file": "routers/orders.py", "method": "POST", "path": "/orders", "handler": "create_order"},
    ]))
    (d / "models.json").write_text("[]")
    (d / "callgraph.json").write_text("{}")
    (d / "manifest.json").write_text('{"language": "python"}')

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat

    # code_context를 직접 줘도 index 탐색이 실행돼 index 결과가 프롬프트에 포함된다
    await agent._execute(
        context={},
        params={
            "code_context": json.dumps({"files": [{"path": "/direct"}]}),
            "error_code": "ORDER_ERROR",
        },
    )

    prompt = captured[0]
    assert "create_order" in prompt  # index 결과가 항상 포함됨


async def test_callgraph_entries_for_matched_files_included(tmp_path):
    """선별된 endpoint/model의 파일에 대응하는 callgraph 항목만 포함된다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text(json.dumps([
        {"file": "routers/orders.py", "method": "POST", "path": "/orders", "handler": "create_order"},
    ]))
    (d / "models.json").write_text("[]")
    (d / "callgraph.json").write_text(json.dumps({
        "routers/orders.py": ["schemas.py", "models.py"],
        "routers/auth.py": ["schemas.py"],  # 관련 없는 파일
    }))
    (d / "manifest.json").write_text("{}")

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat
    await agent._execute(
        context={},
        params={"error_code": "ORDER_FAIL", "summary": "order 처리 오류"},
    )

    prompt = captured[0]
    assert "routers/orders.py" in prompt   # 매칭된 파일의 callgraph
    assert "routers/auth.py" not in prompt  # 무관 파일의 callgraph는 제외


def test_runtime_context_request_path_extracted_as_url_segment():
    """runtime_context의 request.path에서 URL 세그먼트가 추출된다."""
    import json as _json
    runtime_ctx = _json.dumps({
        "request": {"method": "POST", "path": "/api/payment"},
        "error": {"type": "ValueError"},
    })
    clues = RootCauseAgent._extract_clues("UNKNOWN", "", [], runtime_ctx)
    assert "payment" in clues["url_segments"]


def test_runtime_context_stack_trace_extracted_as_file_stem():
    """runtime_context의 stack trace .py 참조에서 파일명이 추출된다."""
    import json as _json
    runtime_ctx = _json.dumps({
        "traceback": "File routers/orders.py line 42 in create_order",
    })
    clues = RootCauseAgent._extract_clues("XYZ", "", [], runtime_ctx)
    assert "orders" in clues["file_stems"]


async def test_mismatch_field_exact_match_scores_high(tmp_path):
    """mismatch field명이 model fields에 있으면 높은 점수로 선별된다."""
    d = _make_index_dir(tmp_path)
    (d / "endpoints.json").write_text("[]")
    (d / "models.json").write_text(json.dumps([
        {"file": "schemas.py", "name": "OrderOut", "fields": ["id", "penalty_amount", "status"]},
        {"file": "schemas.py", "name": "LoginRequest", "fields": ["email", "password"]},
    ]))
    (d / "callgraph.json").write_text("{}")
    (d / "manifest.json").write_text("{}")

    agent = _agent_with_index(tmp_path)
    captured: list[str] = []

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        if not captured and "단서 추출" not in system_prompt:
            captured.append(user_prompt)
        return _llm_resp(_single_candidate_llm())

    agent.llm.chat = capture_chat

    await agent._execute(
        context={},
        params={
            "error_code": "MISMATCH",
            "mismatches": [{"field": "penalty_amount", "ui_value": "0", "api_value": "5000"}],
        },
    )

    prompt = captured[0]
    assert "OrderOut" in prompt       # penalty_amount field 정확 매칭 → 고점수
    assert "LoginRequest" not in prompt  # 무관 model 제외


async def test_regression_cross_check_input_flow_unchanged(
    cross_check_input, llm_response_content
):
    """cross_check_input 흐름이 변경 없이 동작한다. runtime_context는 tc_id 기반 더미에서 로드."""
    agent = _make_agent()
    agent.llm.chat = _mock_chat(llm_response_content, _JUDGE_OK)

    result = await agent._execute(
        context={},
        params={**cross_check_input},
    )

    rc = result.result["root_causes"][0]
    assert rc["tc_id"] == cross_check_input["tc_id"]
    assert len(rc["candidates"]) >= 1


# ─── code_context_raw 내용 확인 (디버그용) ────────────────────────────────────


async def test_code_context_raw():
    """ code_context_raw 내용 확인

    흐름:
      CrossCheck 입력 → tc_id 기반 runtime 로드 → LLM 단서 추출
      → 인덱스 검색 → 원인 추론 LLM 호출 → 결과 출력
    """
    SYSTEM_UNDER_TEST = Path(__file__).parent.parent.parent.parent / "system-under-test"
    if not SYSTEM_UNDER_TEST.exists():
        pytest.skip("system-under-test 경로 없음")

    # ── 1. CrossCheck 입력 (TC-001 fixture)
    cross_check = json.loads((FIXTURES_DIR / "cross_check_input.json").read_text())
    print("\n" + "=" * 60)
    print("① CrossCheck 입력")
    print(f"   tc_id      : {cross_check['tc_id']}")
    print(f"   error_code : {cross_check['error_code']}")
    print(f"   summary    : {cross_check['summary']}")
    print(f"   mismatches : {[m['field'] for m in cross_check.get('mismatches', [])]}")

    # ── 2. Agent 구성 (실제 인덱스 사용)
    agent = RootCauseAgent(config=QApilotConfig(
        project=ProjectConfig(repo_path=str(SYSTEM_UNDER_TEST))
    ))

    # LLM mock — 단서 추출·원인 분석·Judge 각 응답 캡처
    llm_calls: list[dict] = []
    analysis_resp = _single_candidate_llm()

    async def capturing_llm(system_prompt: str, user_prompt: str, **_) -> LLMResponse:
        if "단서 추출" in system_prompt:
            # 실제 단서 추출은 mock 반환 (LLM 비용 없이 테스트)
            content = json.dumps({
                "endpoints": ["orders"],
                "files": ["orders"],
                "functions": ["create_order"],
                "models": ["OrderOut", "ContractOut"],
                "keywords": ["integrity", "order"],
            })
            llm_calls.append({"role": "clue_extraction", "response": content})
            return _llm_resp(content)
        if "root_causes" not in "".join(llm_calls[c]["role"] for c in range(len(llm_calls)) if "analysis" in llm_calls[c]["role"] if False) and len([c for c in llm_calls if c["role"] == "analysis"]) == 0:
            llm_calls.append({"role": "analysis", "prompt_snippet": user_prompt[:300]})
            return _llm_resp(analysis_resp)
        llm_calls.append({"role": "judge"})
        return _llm_resp(_JUDGE_OK)

    agent.llm.chat = capturing_llm

    # ── 3. 단서 추출 미리 보기
    runtime_raw = agent._load_dummy_context(cross_check["tc_id"], "runtime_context")
    clues = await agent._extract_clues_with_llm(
        error_code=cross_check["error_code"],
        summary=cross_check["summary"],
        mismatches=cross_check.get("mismatches", []),
        runtime_context=runtime_raw,
    )

    print("\n② Runtime Context (tc_id 기반 더미)")
    if runtime_raw:
        rt = json.loads(runtime_raw)
        print(f"   api_calls  : {len((rt.get('api_trace') or {}).get('calls', []))}건")
        print(f"   stack_trace: {rt.get('stack_trace', '')[:80]}...")
    else:
        print("   (없음)")

    print("\n③ LLM 단서 추출 결과")
    print(f"   from_llm  : {clues['from_llm']}")
    print(f"   tokens    : {sorted(clues['tokens'])[:8]} ...")
    print(f"   endpoints : {sorted(clues['endpoints'])}")
    print(f"   functions : {sorted(clues['functions'])}")
    print(f"   models    : {sorted(clues['models'])}")

    # ── 4. 인덱스 검색 결과 미리 보기
    code_context_raw = agent._load_from_codebase_index(clues, cross_check["error_code"])
    if code_context_raw:
        ctx = json.loads(code_context_raw)
        print("\n④ 인덱스 검색 결과")
        print(f"   endpoints : {len(ctx.get('endpoints', []))}개")
        for ep in ctx.get("endpoints", [])[:3]:
            print(f"     - {ep['method']} {ep['path']} ({ep['handler']})")
        print(f"   models    : {len(ctx.get('models', []))}개")
        for m in ctx.get("models", [])[:3]:
            print(f"     - {m['name']}")
        print(f"   callgraph : {len(ctx.get('callgraph', {}))}개 파일") 

   

async def test_full_pipeline_debug():
    """실제 LLM으로 전체 흐름을 실행한다. -s 옵션으로 실행.

    OPENAI_API_KEY 환경변수가 없으면 skip.
    """
    import os
    env_file = Path(__file__).parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY 없음")

    SYSTEM_UNDER_TEST = Path(__file__).parent.parent.parent.parent / "system-under-test"
    if not SYSTEM_UNDER_TEST.exists():
        pytest.skip("system-under-test 경로 없음")

    from qapilot.shared.schemas import AgentInput

    agent = RootCauseAgent(config=QApilotConfig(
        project=ProjectConfig(repo_path=str(SYSTEM_UNDER_TEST))
    ))

    output = await agent.run(AgentInput(
        trace_id="debug-001",
        context={},
        params={
            "tc_id":        "TC-001",
            "error_code":   "HTTP_500",
            "summary":      "결제 처리 중 IntegrityError 발생",
            "mismatches":   [{"field": "order_status", "ui_value": "completed", "api_value": "pending"}],
            "has_mismatch": True,
        },
    ))

    print("\n" + "=" * 60)
    print(json.dumps({
        "result":     output.result,
        "confidence": output.confidence,
        "metadata":   output.metadata.model_dump(),
    }, ensure_ascii=False, indent=2))
    print("=" * 60)

    assert output.result["root_causes"]
