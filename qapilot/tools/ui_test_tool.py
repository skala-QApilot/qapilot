"""UI 테스트 Tool.

Playwright 코드를 입력받아 브라우저에서 실행하고
스크린샷과 실행 결과를 수집한다.

담당: D
Created: 2026-05-07
"""

from qapilot.tools.base_tool import BaseTool


class UITestTool(BaseTool):
    """UI 테스트 Tool.

    역할: Playwright 코드 실행, 스크린샷 캡처, headed/headless
    입력: GeneratedCode, 대상 URL
    출력: List[UITestResult]
    설정: 페이지 로드 30초, 스텝 30초 타임아웃
    스크린샷: 성공=JPEG 80%, 실패=PNG 원본
    """

    async def _execute(self, params: dict) -> dict:
        raise NotImplementedError
