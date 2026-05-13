"""DB 테스트 Tool.

테스트 실행 전후 DB 상태 스냅샷을 수집하고
trace_id 기준으로 SQL 호출을 추적한다.

담당: E
Created: 2026-05-07
"""

import os
from typing import Any
from qapilot.tools.base_tool import BaseTool

MODULE_URL = os.getenv("QAPILOT_MODULE_URL", "")


class DBTestTool(BaseTool):
    """DB 테스트 Tool.

    역할: 전후 스냅샷, SQL 추적, 시드 주입/자동 롤백
    입력: DB 접속정보, 시드 데이터
    출력: List[DBTestResult]
    보안: DB 원본 데이터는 LLM에 전송하지 않음 (요약만)
    """

    def __init__(self, trace_id: str | None = None, **kwargs):
        super().__init__(trace_id=trace_id, **kwargs)

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        # TODO: DB 스캔 모듈 스펙 확정 후 구현 예정
        raise NotImplementedError("DB 스캔 모듈 스펙 확정 후 구현 예정")



