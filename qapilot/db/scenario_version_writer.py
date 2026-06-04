"""scenario_versions (사용자 마일스톤) INSERT — 자동 v1.0 박제용.

파이프라인이 init 트리거로 시나리오를 처음 생성한 직후 호출.
service_id 의 v1.0 이 이미 있으면 UNIQUE 제약(V16) 으로 충돌 → ON CONFLICT DO NOTHING 으로 skip.

Author: C
Created: 2026-06-04
"""

from __future__ import annotations

import json
import uuid

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.scenario_version_writer")


def insert_initial_milestone(
    service_id: str,
    scenarios: list[dict],
    label: str = "v1.0",
    description: str = "초기 자동 생성",
) -> bool:
    """첫 마일스톤 박제. 같은 (service_id, label) 이미 있으면 skip (멱등).

    Returns:
        True 면 새 row 박힘, False 면 skip 또는 실패.
    """
    pool = get_pool()
    if pool is None or not service_id or not scenarios:
        return False

    payload_json = json.dumps(scenarios, ensure_ascii=False, default=str)
    new_id = str(uuid.uuid4())

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scenario_versions
                    (id, service_id, label, description, scenarios_payload, is_favorite)
                VALUES (%s, %s, %s, %s, %s::jsonb, false)
                ON CONFLICT (service_id, label) DO NOTHING
                RETURNING id
                """,
                (new_id, service_id, label, description, payload_json),
            )
            row = cur.fetchone()
        return row is not None
    except Exception as e:
        _logger.warning(
            "scenario_version_initial_failed",
            service_id=service_id,
            label=label,
            error=str(e),
        )
        return False
