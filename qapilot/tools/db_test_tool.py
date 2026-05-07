"""DB 테스트 Tool.

테스트 실행 전후 DB 상태 스냅샷을 수집하고
trace_id 기준으로 SQL 호출을 추적한다.

담당: E
Created: 2026-05-07
"""

from qapilot.tools.base_tool import BaseTool


class DBTestTool(BaseTool):
    """DB 테스트 Tool.

    역할: 전후 스냅샷, SQL 추적, 시드 주입/자동 롤백
    입력: DB 접속정보, 시드 데이터
    출력: List[DBTestResult]
    보안: DB 원본 데이터는 LLM에 전송하지 않음 (요약만)
    """

    async def _execute(self, params: dict) -> dict:
        raise NotImplementedError
