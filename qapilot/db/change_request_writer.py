"""change_requests 테이블 INSERT — natural_lang 시나리오 생성/수정 후 dual-write.

AI(챗봇)가 생성·수정한 시나리오를 UI에서 노란 하이라이트와 리뷰 버튼으로 표시하려면
change_requests 행이 있어야 한다. _save_scenarios(natural_lang)에서 호출한다.

DB 미연결 / 외래키 위반 등 실패는 warn 로깅 후 swallow —
파일·시나리오 저장이 source of truth 이므로 흐름을 중단하지 않는다.

Author: C
Created: 2026-06-04
"""

from __future__ import annotations

import uuid

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.change_request_writer")


def upsert_change_request(
    service_id: str,
    scenario_id: str,
    trigger: str = "chatbot",
    reason: str = "",
    status: str = "pending",
) -> None:
    """change_requests 테이블에 행을 INSERT한다.

    같은 (service_id, scenario_id)의 pending 행이 이미 있으면 삭제 후 재삽입한다.
    DB 미연결 시 graceful no-op.

    Args:
        service_id: Spring services.id (UUID).
        scenario_id: ts_id (예: "TS-002").
        trigger: "chatbot" | "file" | "code".
        reason: UI에 표시할 변경 이유 요약.
        status: "pending" (기본값).
    """
    pool = get_pool()
    if pool is None:
        return
    if not (service_id and scenario_id):
        return

    row_id = str(uuid.uuid4())

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            # 같은 시나리오의 기존 pending 요청 제거 — 최신 챗봇 요청으로 교체
            cur.execute(
                """
                DELETE FROM change_requests
                WHERE service_id = %s AND scenario_id = %s AND status = 'pending'
                """,
                (service_id, scenario_id),
            )
            cur.execute(
                """
                INSERT INTO change_requests (id, service_id, scenario_id, trigger, reason, status, type)
                VALUES (%s, %s, %s, %s, %s, %s, 'ai_generated')
                """,
                (row_id, service_id, scenario_id, trigger, reason, status),
            )
    except Exception as e:
        _logger.warning(
            "change_request_db_write_failed",
            service_id=service_id,
            scenario_id=scenario_id,
            error=str(e),
        )
