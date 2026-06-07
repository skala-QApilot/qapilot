"""scenarios 테이블 READ helper.

Author: C
Created: 2026-06-04
"""

from __future__ import annotations

import json
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.scenario_reader")


def load_latest_scenarios(
    service_id: str,
    scenario_ids: list[str] | None = None,
) -> list[dict]:
    """service_id 기준 최신 시나리오를 반환한다.

    ts_id 별로 version_number 가 가장 높은 row 를 선택한다 (is_deleted=false).
    scenario_ids 가 주어지면 해당 ts_id 만 반환한다.
    """
    pool = get_pool()
    if pool is None or not service_id:
        return []

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if scenario_ids:
                cur.execute(
                    """
                    SELECT DISTINCT ON (ts_id) payload
                    FROM scenarios
                    WHERE service_id = %s
                      AND is_deleted = false
                      AND ts_id = ANY(%s)
                    ORDER BY ts_id, version_number DESC
                    """,
                    (service_id, scenario_ids),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT ON (ts_id) payload
                    FROM scenarios
                    WHERE service_id = %s
                      AND is_deleted = false
                    ORDER BY ts_id, version_number DESC
                    """,
                    (service_id,),
                )
            rows = cur.fetchall()
    except Exception as e:
        _logger.warning("scenarios_load_failed", service_id=service_id, error=str(e))
        return []

    result = []
    for (payload_raw,) in rows:
        parsed = _parse_json(payload_raw)
        if isinstance(parsed, dict):
            result.append(parsed)
    return sorted(result, key=lambda x: x.get("ts_id", ""))


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None
