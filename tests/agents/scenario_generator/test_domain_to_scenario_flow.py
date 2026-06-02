"""도메인 지식 → 시나리오 생성 흐름 통합 테스트.

context에 domain_rules / scan_result 가 없을 때
agent가 Tool을 자동 호출하여 보완한 뒤 시나리오를 생성하는 흐름을 검증한다.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from qapilot.agents.scenario_generator import ScenarioGeneratorAgent
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import AgentInput

SAMPLE_DOMAIN_RULES_FROM_TOOL = [
    {
        "rule_id": "RULE-001",
        "source": "PRD_v3.0.pdf",
        "category": "결제",
        "content": "결제 금액은 100원 이상이어야 한다",
        "similarity_score": 0.92,
    },
    {
        "rule_id": "RULE-002",
        "source": "PRD_v3.0.pdf",
        "category": "결제",
        "content": "결제 실패 시 재시도는 3회까지 허용한다",
        "similarity_score": 0.88,
    },
]

SAMPLE_SCAN_FROM_TOOL = {
    "framework": "FastAPI",
    "language": "Python",
    "endpoint_count": 2,
    "files": [
        {
            "path": "src/payment.py",
            "language": "Python",
            "endpoints": [
                {"method": "POST", "path": "/api/payment"},
                {"method": "GET", "path": "/api/payment/history"},
            ],
        }
    ],
}

SAMPLE_REQUIREMENTS = [
    {
        "req_id": "REQ-010",
        "req_type": "functional",
        "content": "사용자는 신용카드로 결제할 수 있다",
        "priority": "high",
        "domain_area": "결제",
    }
]

LLM_RESPONSE_JSON = {
    "scenarios": [
        {
            "name": "결제 처리 시나리오",
            "description": "신용카드 결제 정상/예외 흐름 검증",
            "affected_files": ["src/payment.py"],
            "test_cases": [
                {
                    "name": "정상 결제 성공",
                    "given": "유효한 신용카드와 충분한 잔액이 있는 상태에서",
                    "when": "10,000원 결제를 요청하면",
                    "then": "결제가 완료되고 영수증이 발행된다",
                    "values": [
                        {"field": "amount", "value": "10000", "type": "integer", "purpose": "정상 결제 금액"}
                    ],
                    "tags": ["normal"],
                    "req_id": "REQ-010",
                },
                {
                    "name": "최솟값 미만 금액으로 결제 시도",
                    "given": "결제 최솟값이 100원인 정책이 적용된 상태에서",
                    "when": "50원으로 결제를 요청하면",
                    "then": "최솟값 미달 오류가 반환된다",
                    "values": [
                        {"field": "amount", "value": "50", "type": "integer", "purpose": "최솟값 미만"}
                    ],
                    "tags": ["boundary"],
                    "req_id": "REQ-010",
                },
            ],
        }
    ],
    "confidence": 0.88,
}


def _llm_resp(data: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(data, ensure_ascii=False),
        model="gpt-4o-mini",
        input_tokens=60,
        output_tokens=60,
        cost_usd=0.0,
        cached=False,
    )


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_domain_rules_없으면_tool_호출_후_생성(mock_save):
    """context에 domain_rules가 없으면 domain_knowledge Tool을 호출해 보완한 뒤 시나리오를 생성한다."""
    agent = ScenarioGeneratorAgent(trace_id="test-flow")
    agent.llm.chat = AsyncMock(return_value=_llm_resp(LLM_RESPONSE_JSON))
    agent.use_tool = AsyncMock(return_value={"rules": SAMPLE_DOMAIN_RULES_FROM_TOOL})

    agent_input = AgentInput(
        trace_id="test-flow",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "scan_result": SAMPLE_SCAN_FROM_TOOL,
            # domain_rules 미제공 → Tool 자동 호출
        },
        params={"trigger": "init", "affected_only": False},
    )
    output = await agent.run(agent_input)

    # domain_knowledge Tool이 search action으로 호출되었는지 확인
    agent.use_tool.assert_awaited_once()
    call_args = agent.use_tool.await_args
    assert call_args.args[0] == "domain_knowledge"
    assert call_args.args[1]["action"] == "search"

    scenarios = output.result["scenarios"]
    assert len(scenarios) == 1
    assert scenarios[0]["name"] == "결제 처리 시나리오"
    assert output.confidence == pytest.approx(0.88)


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_scan_result_없으면_codebase_scanner_호출(mock_save):
    """context에 scan_result가 없으면 codebase_scanner Tool을 호출해 보완한다."""
    agent = ScenarioGeneratorAgent(trace_id="test-flow")
    agent.llm.chat = AsyncMock(return_value=_llm_resp(LLM_RESPONSE_JSON))

    scan_call = AsyncMock(return_value=SAMPLE_SCAN_FROM_TOOL)
    domain_call = AsyncMock(return_value={"rules": SAMPLE_DOMAIN_RULES_FROM_TOOL})

    async def use_tool_side_effect(tool_name: str, params: dict):
        if tool_name == "codebase_scanner":
            return await scan_call(tool_name, params)
        return await domain_call(tool_name, params)

    agent.use_tool = AsyncMock(side_effect=use_tool_side_effect)

    agent_input = AgentInput(
        trace_id="test-flow",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            # scan_result, domain_rules 모두 미제공 → 둘 다 Tool 호출
        },
        params={"trigger": "code_change", "affected_only": False},
    )
    output = await agent.run(agent_input)

    tool_calls = [c.args[0] for c in agent.use_tool.await_args_list]
    assert "codebase_scanner" in tool_calls
    assert "domain_knowledge" in tool_calls
    assert len(output.result["scenarios"]) == 1


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_domain_rules_있으면_tool_호출_안함(mock_save):
    """context에 domain_rules가 이미 있으면 domain_knowledge Tool을 호출하지 않는다."""
    agent = ScenarioGeneratorAgent(trace_id="test-flow")
    agent.llm.chat = AsyncMock(return_value=_llm_resp(LLM_RESPONSE_JSON))
    agent.use_tool = AsyncMock()

    agent_input = AgentInput(
        trace_id="test-flow",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "domain_rules": SAMPLE_DOMAIN_RULES_FROM_TOOL,  # 이미 있음
            "scan_result": SAMPLE_SCAN_FROM_TOOL,
        },
        params={"trigger": "code_change", "affected_only": False},
    )
    await agent.run(agent_input)

    agent.use_tool.assert_not_awaited()


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_도메인_규칙이_프롬프트에_포함됨(mock_save):
    """도메인 규칙 내용이 LLM에 전달되는 user_prompt에 포함된다."""
    agent = ScenarioGeneratorAgent(trace_id="test-flow")

    captured_prompt: dict = {}

    async def capture_chat(system_prompt: str, user_prompt: str, **kwargs):
        captured_prompt["user"] = user_prompt
        return _llm_resp(LLM_RESPONSE_JSON)

    agent.llm.chat = capture_chat

    agent_input = AgentInput(
        trace_id="test-flow",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "domain_rules": SAMPLE_DOMAIN_RULES_FROM_TOOL,
            "scan_result": SAMPLE_SCAN_FROM_TOOL,
        },
        params={"trigger": "init", "affected_only": False},
    )
    await agent.run(agent_input)

    user_prompt = captured_prompt["user"]
    assert "결제 금액은 100원 이상이어야 한다" in user_prompt
    assert "REQ-010" in user_prompt
