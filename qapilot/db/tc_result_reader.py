"""tc_results / tc_artifacts READ helper.

Author: C
Created: 2026-06-04
"""

from __future__ import annotations

import json
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.tc_result_reader")


def _is_uuid(value: str) -> bool:
    import uuid as _uuid
    try:
        _uuid.UUID(str(value))
        return True
    except Exception:
        return False


def load_tc_results_by_run(run_id: str) -> list[dict]:
    """run_id 기준 tc_results 를 (ts_id, tc_id) 로 집계해 반환한다.

    반환 형태: [{"ts_id": ..., "tc_id": ..., "ui": {...}, "api": {...}, "db": {...}}, ...]
    """
    pool = get_pool()
    if pool is None or not run_id:
        return []
    if not _is_uuid(run_id):
        # 클라이언트 임시 id (run-retest-* 등) — uuid 컬럼 조회 불가.
        # 경고 폭주 대신 조용히 빈 응답 (UI mock 잔재 폴링 방어).
        return []

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts_id, tc_id, kind, payload
                FROM tc_results
                WHERE run_id = %s
                ORDER BY ts_id, tc_id, kind
                """,
                (run_id,),
            )
            rows = cur.fetchall()
    except Exception as e:
        _logger.warning("tc_results_load_failed", run_id=run_id, error=str(e))
        return []

    # (ts_id, tc_id) → {kind: payload} 집계
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for ts_id, tc_id, kind, payload_raw in rows:
        key = (str(ts_id), str(tc_id))
        if key not in grouped:
            grouped[key] = {"ts_id": str(ts_id), "tc_id": str(tc_id)}
        grouped[key][kind] = _parse_json(payload_raw)

    return list(grouped.values())


def load_completed_tc_ids(run_id: str) -> set[str]:
    """resume 시 이미 완료된 TC 의 tc_id 집합을 반환한다 (ui kind 기준)."""
    pool = get_pool()
    if pool is None or not run_id:
        return set()

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tc_id FROM tc_results WHERE run_id = %s AND kind = 'ui'",
                (run_id,),
            )
            rows = cur.fetchall()
    except Exception as e:
        _logger.warning("completed_tc_ids_failed", run_id=run_id, error=str(e))
        return set()

    return {str(row[0]) for row in rows}


def load_latest_screenshot_s3_key(run_id: str) -> str | None:
    """run_id 에 속한 가장 최근 PNG 아티팩트의 s3_key 를 반환한다."""
    pool = get_pool()
    if pool is None or not run_id:
        return None
    if not _is_uuid(run_id):
        return None  # 클라이언트 임시 id — 경고 폭주 방어

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ta.s3_key
                FROM tc_artifacts ta
                JOIN tc_results tr ON tr.id = ta.tc_result_id
                WHERE tr.run_id = %s AND ta.kind = 'png'
                ORDER BY ta.created_at DESC
                LIMIT 1
                """,
                (run_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        _logger.warning("latest_screenshot_failed", run_id=run_id, error=str(e))
        return None

    return str(row[0]) if row else None


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
