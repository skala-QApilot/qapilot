"""장애 분류 Agent.

실패 지점을 UI 오류, API 오류, 데이터 불일치,
인프라 문제, 도메인 규칙 위반으로 자동 분류한다.

담당: F
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent


class DefectClassifierAgent(BaseAgent):
    """장애 분류 Agent.

    역할: 규칙 1차 + LLM 보조 → 5개 카테고리 분류
    입력: CrossCheckResult, 실패 로그
    출력: List[DefectClassification], confidence
    호출 Tool: 없음
    """

    async def _execute(
        self, context: dict, params: dict, last_error: str | None = None
    ) -> "ExecuteResult":
        raise NotImplementedError
