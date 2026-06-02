"""ScenarioGeneratorAgent 단위 테스트.

LLM 응답을 모킹하여 agent 핵심 로직을 검증한다.
- 도메인 규칙·요구사항 → 시나리오 생성
- TC 중복 제거
- PRD-코드 불일치 탐지
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.agents.scenario_generator import ScenarioGeneratorAgent
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import AgentInput

# ── 공통 픽스처 ────────────────────────────────────────────────────────────────

SAMPLE_REQUIREMENTS = [
    {
        "req_id": "REQ-001",
        "req_type": "functional",
        "content": "사용자는 이메일과 비밀번호로 회원 가입할 수 있다",
        "priority": "high",
        "domain_area": "회원",
    },
    {
        "req_id": "REQ-002",
        "req_type": "functional",
        "content": "사용자는 로그인 후 마이페이지에 접근할 수 있다",
        "priority": "medium",
        "domain_area": "인증",
    },
]

SAMPLE_DOMAIN_RULES = [
    {
        "rule_id": "RULE-001",
        "source": "PRD_v3.0.pdf",
        "category": "회원",
        "content": "비밀번호는 8자 이상이어야 한다",
        "similarity_score": 0.95,
    },
]

SAMPLE_SCAN_RESULT = {
    "framework": "FastAPI",
    "language": "Python",
    "endpoint_count": 3,
    "files": [
        {
            "path": "src/auth.py",
            "language": "Python",
            "endpoints": [
                {"method": "POST", "path": "/api/auth/signup"},
                {"method": "POST", "path": "/api/auth/login"},
            ],
        }
    ],
}

VALID_LLM_JSON = {
    "scenarios": [
        {
            "name": "회원 가입 시나리오",
            "description": "회원 가입 정상/예외 흐름 검증",
            "affected_files": ["src/auth.py"],
            "test_cases": [
                {
                    "name": "정상 회원 가입",
                    "given": "이메일과 비밀번호가 유효한 상태에서",
                    "when": "회원 가입을 요청하면",
                    "then": "계정이 생성되고 환영 이메일이 발송된다",
                    "values": [
                        {"field": "email", "value": "test@example.com", "type": "string", "purpose": "유효한 이메일"}
                    ],
                    "tags": ["normal"],
                    "req_id": "REQ-001",
                },
                {
                    "name": "중복 이메일로 가입 시도",
                    "given": "이미 가입된 이메일이 있는 상태에서",
                    "when": "동일한 이메일로 회원 가입을 요청하면",
                    "then": "중복 이메일 오류가 반환된다",
                    "values": [
                        {"field": "email", "value": "dup@example.com", "type": "string", "purpose": "중복 이메일"}
                    ],
                    "tags": ["edge_case"],
                    "req_id": "REQ-001",
                },
            ],
        }
    ],
    "confidence": 0.9,
}


def _make_llm_response(data: dict) -> LLMResponse:
    return LLMResponse(content=json.dumps(data, ensure_ascii=False), model="gpt-4o-mini", input_tokens=50, output_tokens=50, cost_usd=0.0, cached=False)


def _make_input(**context_kwargs) -> AgentInput:
    return AgentInput(
        trace_id="test-trace",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "domain_rules": SAMPLE_DOMAIN_RULES,
            "scan_result": SAMPLE_SCAN_RESULT,
            **context_kwargs,
        },
        params={"trigger": "code_change", "affected_only": False},
    )


# ── 테스트 ──────────────────────────────────────────────────────────────────────


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_시나리오_정상_생성(mock_save):
    """도메인 규칙·요구사항·스캔 결과가 context에 있을 때 시나리오가 생성된다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    output = await agent.run(_make_input())

    scenarios = output.result["scenarios"]
    # 요구사항 수(2)만큼 TS 생성 — 요구사항별 1:1 생성 방식
    assert len(scenarios) == len(SAMPLE_REQUIREMENTS)
    assert scenarios[0]["ts_id"] == "TS-001"
    assert scenarios[0]["name"] == "회원 가입 시나리오"
    assert len(scenarios[0]["test_cases"]) == 2
    assert output.confidence == pytest.approx(0.9)
    mock_save.assert_called_once_with(scenarios)


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_TC_중복_제거(mock_save):
    """동일한 given/when/then 조합의 TC는 한 번만 포함된다."""
    tc = {
        "name": "중복 TC",
        "given": "동일한 given",
        "when": "동일한 when",
        "then": "동일한 then",
        "values": [],
        "tags": ["normal"],
        "req_id": None,
    }
    data = {
        "scenarios": [
            {
                "name": "중복 테스트",
                "description": "",
                "affected_files": [],
                "test_cases": [tc, tc],  # 동일 TC 2개
            }
        ],
        "confidence": 0.7,
    }

    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(data))

    output = await agent.run(_make_input())

    scenarios = output.result["scenarios"]
    assert len(scenarios[0]["test_cases"]) == 1


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_PRD_코드_불일치_탐지(mock_save):
    """high priority 요구사항의 도메인이 코드에 없으면 불일치 목록에 포함된다."""
    requirements_with_missing = [
        {
            "req_id": "REQ-099",
            "req_type": "functional",
            "content": "사용자는 배송 현황을 조회할 수 있다",
            "priority": "high",
            "domain_area": "배송",  # scan_result에 배송 관련 코드 없음
        }
    ]
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    output = await agent.run(_make_input(requirements=requirements_with_missing))

    mismatches = output.result["prd_code_mismatches"]
    assert len(mismatches) == 1
    assert mismatches[0]["req_id"] == "REQ-099"
    assert "배송" in mismatches[0]["note"]


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_affected_only_파라미터(mock_save):
    """affected_only=True이면 git_diff 변경 파일만 스캔 대상으로 설정한다."""
    scan_with_diff = {
        **SAMPLE_SCAN_RESULT,
        "git_diff": {"changed_files": ["src/auth.py"]},
    }
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    agent_input = AgentInput(
        trace_id="test-trace",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "domain_rules": SAMPLE_DOMAIN_RULES,
            "scan_result": scan_with_diff,
        },
        params={"trigger": "code_change", "affected_only": True},
    )
    output = await agent.run(agent_input)

    scenarios = output.result["scenarios"]
    # affected_files가 git_diff 기반으로 설정되어야 함
    assert scenarios[0]["affected_files"] == ["src/auth.py"]


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_TS_TC_ID_자동_부여(mock_save):
    """TS-001, TS-001-TC-01 형식의 ID가 순서대로 부여된다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    output = await agent.run(_make_input())

    scenarios = output.result["scenarios"]
    assert scenarios[0]["ts_id"] == "TS-001"
    tcs = scenarios[0]["test_cases"]
    assert tcs[0]["tc_id"] == "TS-001-TC-01"
    assert tcs[1]["tc_id"] == "TS-001-TC-02"
