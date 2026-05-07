"""원인 추론 Agent.

로그, 코드베이스, Git 이력을 종합 분석하여
결함 원인 후보 Top-N을 신뢰도 및 근거와 함께 도출한다.

담당: F
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import AgentInput, AgentOutput


class RootCauseAgent(BaseAgent):
    """원인 추론 Agent.

    역할: 로그+코드+Git → Top-N 원인 후보 + 근거 3종
    입력: DefectClassification, CodebaseContext
    출력: List[RootCauseResult], confidence
    호출 Tool: 코드 인덱스 Tool, 도메인 지식 Tool
    HITL: X
    """

    async def run(self, input: AgentInput) -> AgentOutput:
        raise NotImplementedError
