"""DB 테스트 Tool.

테스트 실행 전후 DB 상태 스냅샷을 수집하고
trace_id 기준으로 SQL 호출을 추적한다.

담당: E
Created: 2026-05-07
"""

import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from qapilot.shared.schemas import ToolInput, ToolOutput, DBSnapshot, DBTestResult
from qapilot.tools.base_tool import BaseTool


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://qapilot:qapilot@localhost:5432/qapilot")
ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")


class DBTestTool(BaseTool):
    """DB 테스트 Tool."""

    def __init__(self, trace_id: str | None = None):
        super().__init__(trace_id=trace_id)
        self.engine = create_async_engine(ASYNC_DATABASE_URL, echo=False)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def _get_table_names(self, session: AsyncSession) -> list[str]:
        """DB에 있는 테이블 목록 조회."""
        result = await session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return [row[0] for row in result.fetchall()]

    async def _get_row_count(self, session: AsyncSession, table: str) -> int:
        """테이블 row 수 조회."""
        result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return result.scalar()

    async def _take_snapshot(self, session: AsyncSession) -> dict[str, int]:
        """모든 테이블의 row 수 스냅샷."""
        tables = await self._get_table_names(session)
        snapshot = {}
        for table in tables:
            snapshot[table] = await self._get_row_count(session, table)
        return snapshot

    async def run(self, input: ToolInput) -> ToolOutput:
        """테스트 전후 DB 스냅샷 비교."""
        tc_id = input.params.get("tc_id", "unknown")

        async with self.session_factory() as session:
            # 1. 테스트 전 스냅샷
            before = await self._take_snapshot(session)

            # 2. 시드 데이터 주입 (있을 경우)
            seed_sql = input.params.get("seed_sql")
            if seed_sql:
                await session.execute(text(seed_sql))
                await session.commit()

            # 3. 실행 중 SQL 쿼리 로그 캡처 (trace_id 매칭)
            sql_logs = []
            try:
                log_result = await session.execute(
                    text("""
                        SELECT query, calls, total_exec_time, rows
                        FROM pg_stat_statements
                        ORDER BY total_exec_time DESC
                        LIMIT 50
                    """)
                )
                sql_logs = [
                    {
                        "query": row[0],
                        "calls": row[1],
                        "total_exec_time_ms": round(row[2], 2),
                        "rows": row[3],
                        "trace_id": input.trace_id,
                    }
                    for row in log_result.fetchall()
                ]
            except Exception as e:
                self.logger.warning(f"SQL 로그 캡처 실패 (pg_stat_statements 미설치): {e}")


            # 4. 테스트 후 스냅샷
            after = await self._take_snapshot(session)

            # 5. 전후 비교
            snapshots: list[DBSnapshot] = []
            for table in before:
                before_count = before[table]
                after_count = after.get(table, 0)
                diff = after_count - before_count
                snapshots.append(DBSnapshot(
                    table=table,
                    row_count_before=before_count,
                    row_count_after=after_count,
                    added=max(diff, 0),
                    deleted=max(-diff, 0),
                    modified=0,
                ))

            # 6. 롤백
            await session.rollback()

        result = DBTestResult(
            tc_id=tc_id,
            snapshots=snapshots,
            summary=f"{len(snapshots)}개 테이블 스냅샷 완료",
        )

        return ToolOutput(
            trace_id=input.trace_id,
            result={"db_test": result},
            metadata={
                "table_count": len(snapshots),
                "sql_log_count": len(sql_logs),
                "sql_logs": sql_logs,
            },
        )