"""요구사항 추출 Agent.

PRD 문서에서 개별 요구사항 항목을 구조화된 형식으로 추출하고
REQ-XXX ID를 부여한다. RTM의 행을 구성하는 기준 데이터.

담당: B
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent


class RequirementExtractorAgent(BaseAgent):
    """요구사항 추출 Agent.

    역할: PRD에서 REQ-XXX 구조화 추출, RTM 행 생성
    입력: 도메인 문서 텍스트
    출력: List[RequirementItem], confidence
    호출 Tool: 도메인 지식 Tool
    HITL: X
    """

    async def _execute(self, context: dict, params: dict) -> "ExecuteResult":
        raise NotImplementedError
