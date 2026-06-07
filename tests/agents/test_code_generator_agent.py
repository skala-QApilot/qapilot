"""CodeGeneratorAgent 단위 테스트."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from qapilot.agents.code_generator_agent import CodeGeneratorAgent
from qapilot.shared.config import QApilotConfig
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import AgentInput, ExecuteResult


def _llm_resp(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.0,
        cached=False,
    )


@pytest.fixture
def agent():
    return CodeGeneratorAgent(config=QApilotConfig())


@pytest.mark.asyncio
async def test_empty_action_mappings(agent):
    """action_mappings가 비어있으면 LLM 호출 없이 빈 리스트를 반환한다."""
    agent.llm.chat = AsyncMock()
    
    result = await agent.run(AgentInput(trace_id="test", context={"action_mappings": []}))
    assert result.result["generated_codes"] == []
    assert result.confidence == 1.0
    agent.llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_generate_code_valid_syntax(agent):
    """문법에 맞는 코드를 생성하고 syntax_valid가 True가 되는지 확인한다."""
    mock_response = json.dumps({
        "generated_codes": [
            {
                "tc_id": "TC-001",
                "code": "const { test, expect } = require('@playwright/test');\n\ntest('my test', async ({ page }) => {\n  await page.goto('/');\n});",
                "self_fix_count": 0
            }
        ],
        "confidence": 0.9
    })
    agent.llm.chat = AsyncMock(return_value=_llm_resp(mock_response))
    # 이슈 #140: per-TC LLMClient 분리 후 self.llm 단일 인스턴스 reuse 시 override
    agent._create_tc_llm = lambda: agent.llm

    action_mappings = [{"tc_id": "TC-001", "steps": []}]
    result = await agent.run(
        AgentInput(trace_id="test", context={"action_mappings": action_mappings})
    )
    
    codes = result.result["generated_codes"]
    assert len(codes) == 1
    assert codes[0]["tc_id"] == "TC-001"
    assert codes[0]["syntax_valid"] is True
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_generate_code_invalid_syntax(agent):
    """문법에 어긋난 코드를 생성하면 syntax_valid가 False가 되는지 확인한다."""
    mock_response = json.dumps({
        "generated_codes": [
            {
                "tc_id": "TC-002",
                "code": "const { test, expect } = require('@playwright/test');\n\ntest('my test', async ({ page }) => {\n  await page.goto('/'; // Syntax error missing parenthesis\n});",
                "self_fix_count": 0
            }
        ],
        "confidence": 0.5
    })
    agent.llm.chat = AsyncMock(return_value=_llm_resp(mock_response))
    # 이슈 #140: per-TC LLMClient 분리 후 self.llm 단일 인스턴스 reuse 시 override
    agent._create_tc_llm = lambda: agent.llm

    action_mappings = [{"tc_id": "TC-002", "steps": []}]
    result = await agent.run(
        AgentInput(trace_id="test", context={"action_mappings": action_mappings})
    )
    
    codes = result.result["generated_codes"]
    assert len(codes) == 1
    assert codes[0]["tc_id"] == "TC-002"
    assert codes[0]["syntax_valid"] is False
