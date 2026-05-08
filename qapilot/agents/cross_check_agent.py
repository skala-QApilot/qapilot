"""Cross-check Agent.

UI 실행 결과와 API 응답, DB 상태를 비교하여
데이터 정합성 불일치를 자동 탐지한다.

담당: E
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent


class CrossCheckAgent(BaseAgent):
    """Cross-check Agent.

    역할: UI↔API↔DB 데이터 정합성 검증, 정합성 점수 산출
    입력: UITestResult, APITraceResult, DBTestResult
    출력: List[CrossCheckResult], confidence
    호출 Tool: 없음
    HITL: X
    """

    async def _execute(
        self, context: dict, params: dict, last_error: str | None = None
    ) -> "ExecuteResult":
        raise NotImplementedError
