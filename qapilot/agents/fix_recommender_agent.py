"""해결 방안 추천 Agent.

원인 후보를 입력으로 받아 파일 경로, 담당자,
수정 코드 snippet 등 구체적 해결 가이드를 제시한다.

담당: F
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent


class FixRecommenderAgent(BaseAgent):
    """해결 방안 추천 Agent.

    역할: 파일경로+담당자+수정 snippet 제시
    입력: RootCauseResult, CodebaseContext
    출력: List[FixResult], confidence
    호출 Tool: 코드 인덱스 Tool
    """

    async def _execute(
        self, context: dict, params: dict, last_error: str | None = None
    ) -> "ExecuteResult":
        raise NotImplementedError
