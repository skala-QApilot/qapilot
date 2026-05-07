"""API 추적 Tool.

Playwright 네트워크 리스너로 요청/응답을 캡처하고
trace_id 기준으로 UI 스텝과 API 호출을 매칭한다.

담당: E
Created: 2026-05-07
"""

from qapilot.shared.schemas import ToolInput, ToolOutput
from qapilot.tools.base_tool import BaseTool


class APITraceTool(BaseTool):
    """API 추적 Tool.

    역할: 네트워크 리스너, 요청/응답 수집, trace_id 매칭
    입력: Playwright page 인스턴스 (UI 테스트와 동일 브라우저)
    출력: List[APITraceResult]
    제한: 10MB 초과 응답 body는 저장하지 않음
    """

    async def run(self, input: ToolInput) -> ToolOutput:
        raise NotImplementedError
