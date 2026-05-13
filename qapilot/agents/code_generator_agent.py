"""Playwright 코드 생성 Agent.

UI 액션/API 매핑 리스트를 Playwright 테스트 코드로
변환하고 trace_id를 주입한다.

담당: D
Created: 2026-05-07
"""

from qapilot.agents.base_agent import BaseAgent


class CodeGeneratorAgent(BaseAgent):
    """Playwright 코드 생성 Agent.

    역할: 액션 시퀀스 → Playwright JS 코드
    입력: List[ActionMapping]
    출력: List[GeneratedCode], confidence
    호출 Tool: 없음
    """

    async def _execute(
        self, context: dict, params: dict, last_error: str | None = None
    ) -> "ExecuteResult":
        raise NotImplementedError
