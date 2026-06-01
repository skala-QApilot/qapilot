"""SUT DB Agent.

QApilot이 대상 시스템(minibss) PostgreSQL에 접근하기 위한 FastAPI 에이전트.
클러스터 안에서 실행되어 ClusterIP 서비스로 minibss-postgres에 접근.

엔드포인트:
    GET  /db/tables      - 테이블 목록 조회
    GET  /db/snapshot    - 테이블 스냅샷 조회
    POST /db/seed        - 시드 데이터 주입
    GET  /db/sql-logs    - SQL 쿼리 로그 조회
    POST /db/rollback    - cleanup SQL 기반 롤백

인증: Authorization 헤더 토큰 검증
"""

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ── 환경변수 ──
DB_HOST = os.getenv("POSTGRES_HOST", "minibss-postgres")
DB_PORT = int(os.getenv("PORT", "5432"))
DB_USER = os.getenv("USER", "minibss")
DB_NAME = os.getenv("DB", "minibss")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
API_TOKEN = os.getenv("API_TOKEN", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── DB 커넥션 풀 ──
pool: asyncpg.Pool | None = None

# SQL 로그 버퍼 (인메모리)
sql_log_buffer: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        min_size=2,
        max_size=10,
    )
    logger.info(f"DB 커넥션 풀 생성 완료: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    yield
    await pool.close()
    logger.info("DB 커넥션 풀 종료")


app = FastAPI(
    title="SUT DB Agent",
    description="QApilot 대상 시스템 DB 접근 에이전트",
    version="1.0.0",
    root_path="/sut-db",
    lifespan=lifespan,
)

security = HTTPBearer()


# ── 인증 ──
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not API_TOKEN:
        raise HTTPException(status_code=500, detail="API_TOKEN 환경변수 미설정")
    if credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials.credentials


# ── 응답 포맷 ──
def ok(data: Any) -> dict:
    return {"status": "ok", "data": data}


def err(message: str, code: int = 500) -> HTTPException:
    return HTTPException(status_code=code, detail={"status": "error", "message": message})


# ── 엔드포인트 ──

@app.get("/db/tables", dependencies=[Depends(verify_token)])
async def get_tables():
    """테이블 목록 조회."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
        tables = [row["table_name"] for row in rows]
        return ok({"tables": tables})
    except Exception as e:
        raise err(f"테이블 목록 조회 실패: {e}")


@app.get("/db/snapshot", dependencies=[Depends(verify_token)])
async def get_snapshot(table: str):
    """테이블 스냅샷 조회 (row_count + rows)."""
    try:
        async with pool.acquire() as conn:
            # 테이블 존재 여부 확인
            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = $1
                )
                """,
                table,
            )
            if not exists:
                raise err(f"테이블 '{table}' 없음", 404)

            count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
            rows = await conn.fetch(f'SELECT * FROM "{table}"')
            rows_data = [dict(row) for row in rows]

        return ok({
            "table": table,
            "row_count": count,
            "rows": rows_data,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise err(f"스냅샷 조회 실패: {e}")


class SeedRequest(BaseModel):
    sql: str


@app.post("/db/seed", dependencies=[Depends(verify_token)])
async def inject_seed(body: SeedRequest):
    """시드 데이터 주입."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(body.sql)
            sql_log_buffer.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "seed",
                "sql": body.sql,
            })
        return ok({"message": "시드 주입 완료"})
    except Exception as e:
        raise err(f"시드 주입 실패: {e}")


@app.get("/db/sql-logs", dependencies=[Depends(verify_token)])
async def get_sql_logs():
    """SQL 쿼리 로그 조회 후 버퍼 초기화."""
    logs = list(sql_log_buffer)
    sql_log_buffer.clear()
    return ok({"logs": logs})


class RollbackRequest(BaseModel):
    cleanup_sql: str | None = None


@app.post("/db/rollback", dependencies=[Depends(verify_token)])
async def rollback(body: RollbackRequest = RollbackRequest()):
    """cleanup SQL 기반 롤백.

    트랜잭션 롤백 불가(별도 커넥션) → cleanup SQL로 데이터 정리.
    cleanup_sql 없으면 로그 버퍼만 초기화.
    """
    try:
        if body.cleanup_sql:
            async with pool.acquire() as conn:
                await conn.execute(body.cleanup_sql)
        sql_log_buffer.clear()
        return ok({"message": "롤백 완료"})
    except Exception as e:
        raise err(f"롤백 실패: {e}")


@app.get("/health")
async def health():
    """헬스체크."""
    return {"status": "ok"}
