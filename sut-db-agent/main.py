"""SUT DB Agent.

QApilot이 대상 시스템(minibss) PostgreSQL에 접근하기 위한 FastAPI 에이전트.
클러스터 안에서 실행되어 ClusterIP 서비스로 minibss-postgres에 접근.

엔드포인트:
    GET  /db/tables        - 테이블 목록 조회
    GET  /db/snapshot      - 테이블 스냅샷 조회
    POST /db/check         - precondition 상태 확인 (SELECT 매칭 행 수)
    POST /db/seed          - 시드 데이터 주입
    GET  /db/sql-logs      - SQL 쿼리 로그 조회
    POST /db/restore-point - 복원 기준점 캡처 (rollback 시 복원)
    POST /db/rollback      - 스냅샷 기반 롤백 (restore-point 로 복원)

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

# 스냅샷 기반 롤백 기준점 (인메모리). restore-point 캡처 시 전체 public 테이블의
# 행 데이터를 보관, rollback 시 이 상태로 복원한다. 단일 SUT · TC 순차 실행 가정
# (db_test_tool 의 _module_url 단일 URL 전제와 정합).
restore_snapshot: dict[str, list[dict]] | None = None


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


class CheckRequest(BaseModel):
    sql: str


@app.post("/db/check", dependencies=[Depends(verify_token)])
async def check_state(body: CheckRequest):
    """precondition 상태 확인 — SELECT 실행 후 매칭 행 수 반환. count>=1 이면 충족."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(body.sql)
        return ok({"count": len(rows), "matched": len(rows) > 0})
    except Exception as e:
        raise err(f"상태 확인 실패: {e}")


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


async def _list_public_tables(conn) -> list[str]:
    """public 스키마의 BASE TABLE 목록."""
    rows = await conn.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    return [row["table_name"] for row in rows]


@app.post("/db/restore-point", dependencies=[Depends(verify_token)])
async def create_restore_point():
    """현재 전체 public 테이블 상태를 메모리에 캡처 — rollback 복원 기준점.

    시드 주입 직전에 호출하면 시드(및 이후 변경)를 rollback 으로 되돌릴 수 있다.
    """
    global restore_snapshot
    try:
        snapshot: dict[str, list[dict]] = {}
        async with pool.acquire() as conn:
            for table in await _list_public_tables(conn):
                rows = await conn.fetch(f'SELECT * FROM "{table}"')
                snapshot[table] = [dict(row) for row in rows]
        restore_snapshot = snapshot
        total_rows = sum(len(v) for v in snapshot.values())
        logger.info(f"restore-point 캡처: {len(snapshot)}개 테이블, {total_rows}행")
        # diff 용 snapshots 동봉 — 클라이언트가 before 로 사용. 서버는 native 객체를
        # restore_snapshot 에 그대로 보관(복원 시 타입 손실 없음), 응답만 JSON 직렬화된다.
        snapshots = {
            t: {"table": t, "row_count": len(rows), "rows": rows}
            for t, rows in snapshot.items()
        }
        return ok({"tables": len(snapshot), "rows": total_rows, "snapshots": snapshots})
    except Exception as e:
        raise err(f"restore-point 생성 실패: {e}")


async def _restore_from_snapshot(snapshot: dict[str, list[dict]]) -> None:
    """캡처된 스냅샷으로 전체 복원.

    FK 순서 의존을 피하려 session_replication_role=replica 로 트리거(FK 포함) 를
    우회한다. 권한 부족 시 TRUNCATE … CASCADE 로 fallback (이 경우 FK 순서로 인해
    재삽입이 일부 실패할 수 있어 경고 로그만 남기고 진행).
    """
    tables = list(snapshot.keys())
    async with pool.acquire() as conn:
        async with conn.transaction():
            replica_ok = True
            try:
                await conn.execute("SET session_replication_role = replica")
            except Exception as e:
                replica_ok = False
                logger.warning(f"session_replication_role 설정 실패(권한?) — FK 순서 의존 잔존: {e}")

            if tables:
                quoted = ", ".join(f'"{t}"' for t in tables)
                await conn.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")

            for table, rows in snapshot.items():
                if not rows:
                    continue
                cols = list(rows[0].keys())
                await conn.copy_records_to_table(
                    table,
                    records=[tuple(row[c] for c in cols) for row in rows],
                    columns=cols,
                )

            # TRUNCATE RESTART IDENTITY 로 1 로 리셋된 시퀀스를 재삽입 최대값 기준 보정
            # — 다음 app insert 의 PK 충돌 방지.
            for table, rows in snapshot.items():
                if not rows:
                    continue
                for col in rows[0].keys():
                    seq = await conn.fetchval("SELECT pg_get_serial_sequence($1, $2)", table, col)
                    if not seq:
                        continue
                    maxv = await conn.fetchval(f'SELECT MAX("{col}") FROM "{table}"')
                    if maxv is not None:
                        await conn.execute("SELECT setval($1, $2, true)", seq, int(maxv))

            if replica_ok:
                await conn.execute("SET session_replication_role = DEFAULT")


class RollbackRequest(BaseModel):
    cleanup_sql: str | None = None


@app.post("/db/rollback", dependencies=[Depends(verify_token)])
async def rollback(body: RollbackRequest = RollbackRequest()):
    """스냅샷 기반 롤백.

    우선순위:
      1) restore-point 존재 → 전체 TRUNCATE + 재삽입으로 그 상태 복원 (시드 포함 되돌림)
      2) cleanup_sql 명시 → 해당 SQL 실행 (하위 호환)
      3) 둘 다 없으면 로그 버퍼만 초기화
    """
    global restore_snapshot
    try:
        if restore_snapshot is not None:
            await _restore_from_snapshot(restore_snapshot)
            restore_snapshot = None
        elif body.cleanup_sql:
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
