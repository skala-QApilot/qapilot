"""자연어 요구사항 해석 Agent.

QA 담당자가 입력한 자연어 시나리오를
구조화된 형식(Given/When/Then)으로 변환한다.

담당: C
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import AgentInput, AgentOutput


class NaturalLanguageAgent(BaseAgent):
    """자연어 요구사항 해석 Agent.

    역할: 자연어 → Given/When/Then 구조화
    입력: 사용자 자연어 텍스트
    출력: List[TestScenario], confidence
    호출 Tool: 도메인 지식 Tool
    HITL: O (항상, 승인 후 액션 매핑으로 진행)
    """

    async def run(self, input: AgentInput) -> AgentOutput:
        raise NotImplementedError
