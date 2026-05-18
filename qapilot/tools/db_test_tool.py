"""DB 테스트 Tool.

테스트 실행 전후 DB 상태 스냅샷을 수집하고
trace_id 기준으로 SQL 호출을 추적한다.

담당: E
Created: 2026-05-07
"""

import os
from typing import Any

import httpx

from qapilot.shared.errors import ErrorCode, ToolExecutionError
from qapilot.shared.schemas import DBSnapshot, DBTestResult
from qapilot.tools.base_tool import BaseTool

MODULE_URL = os.getenv("QAPILOT_MODULE_URL", "")


class DBTestTool(BaseTool):
    """DB 테스트 Tool.

    역할: 전후 스냅샷, SQL 추적, 시드 주입/자동 롤백
    입력: DB 접속정보, 시드 데이터
    출력: DBTestResult
    보안: DB 원본 데이터는 LLM에 전송하지 않음 (요약만)
    """

    def __init__(self, trace_id: str | None = None, **kwargs):
        super().__init__(trace_id=trace_id, **kwargs)

    async def _get_tables(self) -> list[str]:
        """DB 테이블 목록 조회."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MODULE_URL}/db/tables")
                response.raise_for_status()
                return response.json()["data"]["tables"]
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"DB 테이블 조회 실패: {e.response.status_code}")

    async def _get_snapshot(self, table: str) -> dict:
        """테이블 스냅샷 조회."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{MODULE_URL}/db/snapshot", params={"table": table}
                )
                response.raise_for_status()
                return response.json()["data"]
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"스냅샷 조회 실패: {e.response.status_code}")

    async def _inject_seed(self, seed_sql: str) -> None:
        """시드 데이터 주입."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{MODULE_URL}/db/seed", json={"sql": seed_sql}
                )
                response.raise_for_status()
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"시드 주입 실패: {e.response.status_code}")

    async def _get_sql_logs(self) -> list[dict]:
        """SQL 쿼리 로그 조회."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MODULE_URL}/db/sql-logs")
                response.raise_for_status()
                return response.json()["data"]["logs"]
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"SQL 로그 조회 실패: {e.response.status_code}")

    async def _rollback(self) -> None:
        """롤백 수행."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{MODULE_URL}/db/rollback")
                response.raise_for_status()
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"롤백 실패: {e.response.status_code}")

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """테스트 전후 DB 스냅샷 비교."""
        tc_id = params.get("tc_id", "unknown")
        seed_sql = params.get("seed_sql")

        if not MODULE_URL:
            raise ValueError("QAPILOT_MODULE_URL 환경변수가 설정되지 않았습니다.")

        tables = await self._get_tables()

        before = {}
        for table in tables:
            before[table] = await self._get_snapshot(table)

        if seed_sql:
            await self._inject_seed(seed_sql)

        sql_logs = []
        try:
            sql_logs = await self._get_sql_logs()
        except Exception as e:
            self.logger.warning(f"SQL 로그 캡처 실패: {e}")

        after = {}
        for table in tables:
            after[table] = await self._get_snapshot(table)

        snapshots: list[DBSnapshot] = []
        for table in tables:
            before_count = before[table].get("row_count", 0)
            after_count = after[table].get("row_count", 0)
            diff = after_count - before_count
            snapshots.append(DBSnapshot(
                table=table,
                row_count_before=before_count,
                row_count_after=after_count,
                added=max(diff, 0),
                deleted=max(-diff, 0),
                modified=0,
            ))

        await self._rollback()

        result = DBTestResult(
            tc_id=tc_id,
            snapshots=snapshots,
            summary=f"{len(snapshots)}개 테이블 스냅샷 완료",
        )

        return {
            "db_test": result,
            "sql_logs": sql_logs,
        }