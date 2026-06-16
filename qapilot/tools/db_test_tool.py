"""DB 테스트 Tool.

테스트 실행 전후 DB 상태 스냅샷을 수집하고
trace_id 기준으로 SQL 호출을 추적한다.

담당: E
Created: 2026-05-07
"""

import json
import os
from typing import Any

import httpx

from qapilot.shared.errors import ErrorCode, ToolExecutionError
from qapilot.shared.schemas import DBSnapshot, DBTestResult
from qapilot.tools.base_tool import BaseTool

# PR #235 (#232) 후속: import 시점 eager capture 격차 본질 fix (#242).
# 이전: `MODULE_URL = os.getenv(...)` module-level → main.py 의 load_dotenv 타이밍 의존 +
# `load_dotenv(override=True)` 적용/revert 에도 취약. e2e trace `40fce3fa` / `c8aadf83` 모두
# DBTestTool fail 잔존의 직접 원인.
# 본 fix: 호출 시점 평가 (lazy) + .env 강제 로드 (1회 캐시) — main.py 의 load_dotenv 동작
# (override 적용/revert) 와 완전히 무관.
_DOTENV_LOADED = False

# UI 상세 표시용 변경 행 보존 상한 (테이블당). diff 결과는 보통 작지만 안전장치.
_MAX_DIFF_ROWS = 50


def _ensure_dotenv() -> None:
    """`.env` 의 값을 강제 로드 (override=True) + 1회 캐시.

    main.py 의 module-level load_dotenv() 가 default override=False 로 shell stale env
    를 덮어쓰지 않거나, 사용자가 override=True 를 revert 한 환경에서도 DBTestTool 호출
    시점에 .env 의 값을 강제 적용. process 당 1회만 실행.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except Exception:
        pass  # dotenv 미설치 환경 graceful
    _DOTENV_LOADED = True


def _module_url() -> str:
    """QAPILOT_SUT_DB_URL 을 호출 시점 평가. .env 강제 로드 보장."""
    _ensure_dotenv()
    return os.getenv("QAPILOT_SUT_DB_URL", "")


def _api_token() -> str:
    """QAPILOT_SUT_DB_TOKEN 을 호출 시점 평가."""
    _ensure_dotenv()
    return os.getenv("QAPILOT_SUT_DB_TOKEN", "")


# 하위 호환 — 다른 module 이 MODULE_URL 을 import 하는 경우 대비 (pipeline._run_db_test_safe
# 가 import 후 환경변수 사전 점검). 단 호출 시점이 import 후이고 .env 가 그 사이에 로드되면
# stale 값. 따라서 신규 코드는 _module_url() 함수 사용 권장.
MODULE_URL = _module_url()
API_TOKEN = _api_token()


def _auth_headers() -> dict:
    """인증 헤더 반환. API_TOKEN 호출 시점 평가."""
    token = _api_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


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
                response = await client.get(f"{_module_url()}/db/tables", headers=_auth_headers())
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
                    f"{_module_url()}/db/snapshot", params={"table": table}, headers=_auth_headers()
                )
                response.raise_for_status()
                return response.json()["data"]
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"스냅샷 조회 실패: {e.response.status_code}")

    async def _create_restore_point(self) -> dict | None:
        """rollback 복원 기준점 캡처 — 서버가 복원용 native 상태를 보관한다.

        이 호출 이후의 변경(시드/테스트 액션)은 _rollback() 의 스냅샷 복원으로 되돌려진다.
        반환: diff 용 {table: {table, row_count, rows}} (서버 응답) 또는 None.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{_module_url()}/db/restore-point", headers=_auth_headers()
                )
                response.raise_for_status()
                return response.json().get("data", {}).get("snapshots")
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"restore-point 생성 실패: {e.response.status_code}")

    async def _check_db(self, check_sql: str) -> bool:
        """precondition 충족 여부 — check_sql(SELECT) 이 1행 이상 반환하면 True."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{_module_url()}/db/check", json={"sql": check_sql}, headers=_auth_headers()
                )
                response.raise_for_status()
                return bool(response.json()["data"]["matched"])
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"DB 상태 확인 실패: {e.response.status_code}")

    async def _inject_seed(self, seed_sql: str) -> None:
        """시드 데이터 주입."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{_module_url()}/db/seed", json={"sql": seed_sql}, headers=_auth_headers()
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
                response = await client.get(f"{_module_url()}/db/sql-logs", headers=_auth_headers())
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
                response = await client.post(f"{_module_url()}/db/rollback", headers=_auth_headers())
                response.raise_for_status()
        except httpx.ConnectError as e:
            raise ToolExecutionError(ErrorCode.TOOL_004, f"DB 스캔 모듈 연결 실패: {e}")
        except httpx.HTTPStatusError as e:
            raise ToolExecutionError(ErrorCode.TOOL_003, f"롤백 실패: {e.response.status_code}")

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """테스트 전후 DB 스냅샷 비교.

        before_snapshot 가 주어지면(파이프라인이 TC 액션 *전* 캡처한 기준점) 이를 before 로
        사용해 실제 변경을 diff 한다. 없으면 호출 시점에 before 를 캡처한다(단위 테스트 등).
        """
        tc_id = params.get("tc_id", "unknown")
        seed_sql = params.get("seed_sql")
        pre = params.get("before_snapshot")

        if not _module_url():
            raise ValueError("QAPILOT_SUT_DB_URL 환경변수가 설정되지 않았습니다.")

        if pre and pre.get("snapshots"):
            # 액션 전 상태가 주어짐 → before 재캡처 없이 사용 (seed 는 이 경로에서 미사용).
            tables = pre.get("tables") or list(pre["snapshots"].keys())
            before = pre["snapshots"]
        else:
            tables = await self._get_tables()

            # seed 주입을 before 스냅샷 이전에 수행 → 시드 SQL이 테스트 SQL 로그에 섞이지 않도록.
            # 시드 주입 직전에 복원 기준점을 캡처해 _rollback() 이 시드를 확실히 되돌리게 한다.
            if seed_sql:
                await self._create_restore_point()
                await self._inject_seed(seed_sql)

            before = {}
            for table in tables:
                before[table] = await self._get_snapshot(table)

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
            before_data = before[table]
            after_data = after[table]
            before_count = before_data.get("row_count", 0)
            after_count = after_data.get("row_count", 0)
            diff = after_count - before_count

            # before/after rows 데이터가 있으면 실제 행 변경 감지
            modified = 0
            rows_added: list[dict] = []
            rows_removed: list[dict] = []
            before_rows = before_data.get("rows")
            after_rows = after_data.get("rows")
            if before_rows is not None and after_rows is not None:
                # 행 전체를 canonical JSON 으로 직렬화해 집합 diff. before 에만 있던 행 /
                # after 에만 있던 행을 실제 값과 함께 보존 (UI 상세 표시용).
                before_map = {json.dumps(row, sort_keys=True, default=str): row for row in before_rows}
                after_map = {json.dumps(row, sort_keys=True, default=str): row for row in after_rows}
                before_keys = set(before_map)
                after_keys = set(after_map)
                rows_removed = [before_map[k] for k in before_keys - after_keys]
                rows_added = [after_map[k] for k in after_keys - before_keys]
                # 행 수는 같지만 내용이 다른 경우 modified 계산
                if before_count == after_count:
                    modified = len(before_keys - after_keys)

            snap = DBSnapshot(
                table=table,
                row_count_before=before_count,
                row_count_after=after_count,
                added=max(diff, 0),
                deleted=max(-diff, 0),
                modified=modified,
            )
            # 변경 행만 보존 (cap) — 미변경 테이블은 필드 자체를 생략해 payload 비대화 방지.
            if rows_added:
                snap["rows_added"] = rows_added[:_MAX_DIFF_ROWS]
            if rows_removed:
                snap["rows_removed"] = rows_removed[:_MAX_DIFF_ROWS]
            snapshots.append(snap)

        # 복원은 diff 산출 이후. 권한/FK 등으로 복원이 실패해도 변경 diff 결과는 보존한다
        # (복원 실패 시 변경이 DB 에 잔존 — 경고만 남기고 다음 TC 가 새 복원점을 잡는다).
        try:
            await self._rollback()
        except Exception as e:
            self.logger.warning(f"DB 롤백(복원) 실패 — 변경 잔존: {e}")

        result = DBTestResult(
            tc_id=tc_id,
            snapshots=snapshots,
            summary=f"{len(snapshots)}개 테이블 스냅샷 완료",
        )

        return {
            "db_test": result,
            "sql_logs": sql_logs,
        }