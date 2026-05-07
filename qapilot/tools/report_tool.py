"""리포트 Tool.

테스트 결과를 종합하여 단일 형식의 리포트를 생성한다.
템플릿 기반 렌더링이며 LLM을 호출하지 않는다.

담당: F
Created: 2026-05-07
"""

from qapilot.tools.base_tool import BaseTool


class ReportTool(BaseTool):
    """리포트 Tool.

    역할: 테스트 결과 리포트 생성 (단일 형식)
    입력: 전체 실행 결과 (UI/API/DB/Cross-check/장애분석)
    출력: 리포트 파일 경로
    구성: 실행 요약 + 실패 케이스 + 원인 분석 + 해결 방안
    """

    async def _execute(self, params: dict) -> dict:
        raise NotImplementedError
