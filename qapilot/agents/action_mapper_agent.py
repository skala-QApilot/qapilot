"""시나리오-액션 매핑 Agent.

구조화된 시나리오를 UI 액션 리스트, API 매핑,
검증 포인트로 분해하고 실제 셀렉터/엔드포인트와 매칭한다.

담당: C
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent


class ActionMapperAgent(BaseAgent):
    """시나리오-액션 매핑 Agent.

    역할: 시나리오 → UI액션+API매핑+검증포인트 분해
    입력: TestScenario, CodebaseContext (endpoints.json)
    출력: List[ActionMapping], confidence
    호출 Tool: 코드 인덱스 Tool
    HITL: X
    """

    async def _execute(
        self, context: dict, params: dict, last_error: str | None = None
    ) -> "ExecuteResult":
        raise NotImplementedError
