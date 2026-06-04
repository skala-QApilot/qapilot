"""scenarios INSERT — 파일 기반 scenarios/<ts>.json 와 dual-write.

V15 이후 scenarios 한 테이블에 (ts_id, version_number, payload, is_deleted) row 누적.
upsert_scenario_version: MAX(version_number)+1 로 새 row INSERT (한 쿼리).
DB 미연결 / service_id 미존재 시 graceful no-op — file 기록이 source of truth.

Author: C
Created: 2026-06-01, V15 통합 적용 2026-06-04
"""

from __future__ import annotations

import json

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.scenario_writer")


def upsert_scenario_version(service_id: str, ts_id: str, payload: dict) -> bool:
    """한 시나리오의 새 version row 를 기록. 반환: 성공 여부."""
    pool = get_pool()
    if pool is None:
        return False
    if not (service_id and ts_id):
        return False

    payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scenarios (service_id, ts_id, version_number, payload)
                VALUES (
                    %s, %s,
                    COALESCE(
                        (SELECT MAX(version_number) FROM scenarios
                         WHERE service_id = %s AND ts_id = %s),
                        0
                    ) + 1,
                    %s::jsonb
                )
                """,
                (service_id, ts_id, service_id, ts_id, payload_json),
            )
        return True
    except Exception as e:
        _logger.warning(
            "scenario_db_mirror_failed",
            service_id=service_id,
            ts_id=ts_id,
            error=str(e),
        )
        return False
