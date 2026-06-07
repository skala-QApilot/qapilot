"""codebase_indices DB+S3 read helper.

snapshot_time 기준 코드 복원 함수(load_code_at_snapshot)도 포함.


frontend.json 등 runtime index 를 ActionMapper 가 로컬 디스크에만 의존하지 않고
복구할 수 있도록 가장 최근 mirror 를 읽는다.
"""

from __future__ import annotations

import json
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger
from qapilot.storage import s3_client

_logger = get_logger("db.code_reader")


def load_codebase_index(
    service_id: str,
    kind: str,
    commit_hash: str | None = None,
) -> Any | None:
    """service/kind 에 대응하는 최신 codebase index payload 를 반환한다."""
    pool = get_pool()
    if pool is None or not (service_id and kind):
        return None

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if commit_hash:
                cur.execute(
                    """
                    SELECT s3_key
                    FROM codebase_indices
                    WHERE service_id = %s AND kind = %s AND commit_hash = %s
                    ORDER BY scanned_at DESC
                    LIMIT 1
                    """,
                    (service_id, kind, commit_hash),
                )
            else:
                cur.execute(
                    """
                    SELECT s3_key
                    FROM codebase_indices
                    WHERE service_id = %s AND kind = %s
                    ORDER BY scanned_at DESC
                    LIMIT 1
                    """,
                    (service_id, kind),
                )
            row = cur.fetchone()
    except Exception as e:
        _logger.warning(
            "codebase_index_lookup_failed",
            service_id=service_id,
            kind=kind,
            commit_hash=commit_hash,
            error=str(e),
        )
        return None

    if not row:
        return None

    data = s3_client.get_object(row[0])
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        _logger.warning(
            "codebase_index_parse_failed",
            service_id=service_id,
            kind=kind,
            s3_key=row[0],
            error=str(e),
        )
        return None


def load_latest_generated_code(service_id: str, tc_id: str) -> dict[str, Any] | None:
    """TC 의 최신 generated code 를 S3 latest key 기준으로 로드한다."""
    if not (service_id and tc_id):
        return None
    s3_key = f"services/{service_id}/generated-code/{tc_id}/latest.js"
    data = s3_client.get_object(s3_key)
    if data is None:
        return None
    try:
        return {
            "tc_id": tc_id,
            "code": data.decode("utf-8"),
            "syntax_valid": True,
            "self_fix_count": 0,
        }
    except Exception as e:
        _logger.warning(
            "generated_code_parse_failed",
            service_id=service_id,
            tc_id=tc_id,
            error=str(e),
        )
        return None


def load_latest_action_mapping(service_id: str, tc_id: str) -> dict[str, Any] | None:
    """TC 의 최신 action mapping 을 S3 latest key 우선, 없으면 DB inline 으로 로드."""
    if not (service_id and tc_id):
        return None

    s3_key = f"services/{service_id}/action-mappings/{tc_id}/latest.json"
    data = s3_client.get_object(s3_key)
    if data is not None:
        try:
            payload = json.loads(data.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            _logger.warning(
                "action_mapping_s3_parse_failed",
                service_id=service_id,
                tc_id=tc_id,
                error=str(e),
            )

    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM action_mappings
                WHERE service_id = %s AND tc_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (service_id, tc_id),
            )
            row = cur.fetchone()
    except Exception as e:
        _logger.warning(
            "action_mapping_lookup_failed",
            service_id=service_id,
            tc_id=tc_id,
            error=str(e),
        )
        return None
    if not row:
        return None
    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    return payload if isinstance(payload, dict) else None


def load_code_at_snapshot(
    service_id: str,
    snapshot_time: str,
) -> list[dict]:
    """스냅샷 시점(snapshot_time) 이전 최신 generated_code 를 TC별로 반환한다.

    버전 복원 시 scenario_version.created_at 을 기준으로
    그 시점에 유효하던 코드를 복원하는 데 사용한다.

    Args:
        service_id: 서비스 UUID.
        snapshot_time: ISO 8601 타임스탬프 문자열 (예: 2026-06-05T10:30:00Z).

    Returns:
        [{"tc_id": ..., "code": ..., "s3_key": ...}, ...] — TC별 최신 코드.
    """
    pool = get_pool()
    if pool is None or not (service_id and snapshot_time):
        return []
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (tc_id)
                    tc_id, s3_key, bytes
                FROM generated_code
                WHERE service_id = %s
                  AND created_at <= %s::timestamptz
                ORDER BY tc_id, version DESC
                """,
                (service_id, snapshot_time),
            )
            rows = cur.fetchall()
    except Exception as e:
        _logger.warning("load_code_at_snapshot_failed", service_id=service_id, error=str(e))
        return []

    results = []
    for tc_id, s3_key, _ in rows:
        # S3에서 실제 코드 읽기 (bytes 컬럼은 길이만 저장)
        data = s3_client.get_object(s3_key) if s3_key else None
        if data is None:
            continue
        try:
            results.append({"tc_id": tc_id, "code": data.decode("utf-8"), "s3_key": s3_key})
        except Exception:
            pass
    return results
