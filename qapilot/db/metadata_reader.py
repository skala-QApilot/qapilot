"""metadata_indices DB+S3 read helper — PoC 4 (데이터 layer).

기존 `code_reader.load_codebase_index()` 패턴 그대로. kind/sub_kind 2축 + S3 mirror fallback.

본 모듈은 DB+S3 의 raw read 만. 메모리 LRU 캐시 + token 절감 헬퍼는
`qapilot.shared.scan_storage` 에서 wrap.

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import json
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger
from qapilot.storage import s3_client

_logger = get_logger("db.metadata_reader")


def load_metadata_index_raw(
    service_id: str,
    kind: str,
    sub_kind: str,
    commit_hash: str | None = None,
) -> dict | None:
    """metadata_indices 의 한 record 본문 (JSON dict) 반환.

    Args:
        service_id: service UUID.
        kind: "frontend" | "backend" | "sut_tests".
        sub_kind: "selectors" | "routes" | "schemas" | "patterns".
        commit_hash: 지정 시 해당 commit 의 record, 없으면 최신 scanned_at.

    Returns:
        S3 의 JSON dict (FrontendSelectorsIndex 등) 또는 None
        (DB 미존재 / S3 GET 실패 / parse 실패 시).
    """
    pool = get_pool()
    if pool is None or not (service_id and kind and sub_kind):
        return None

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if commit_hash:
                cur.execute(
                    """
                    SELECT s3_key
                    FROM metadata_indices
                    WHERE service_id = %s AND kind = %s
                          AND sub_kind = %s AND commit_hash = %s
                    ORDER BY scanned_at DESC
                    LIMIT 1
                    """,
                    (service_id, kind, sub_kind, commit_hash),
                )
            else:
                cur.execute(
                    """
                    SELECT s3_key
                    FROM metadata_indices
                    WHERE service_id = %s AND kind = %s AND sub_kind = %s
                    ORDER BY scanned_at DESC
                    LIMIT 1
                    """,
                    (service_id, kind, sub_kind),
                )
            row = cur.fetchone()
    except Exception as e:
        _logger.warning(
            "metadata_index_lookup_failed",
            service_id=service_id, kind=kind, sub_kind=sub_kind,
            commit_hash=commit_hash, error=str(e),
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
            "metadata_index_parse_failed",
            service_id=service_id, kind=kind, sub_kind=sub_kind,
            s3_key=row[0], error=str(e),
        )
        return None


def get_latest_commit_hash(
    service_id: str,
    kind: str | None = None,
    sub_kind: str | None = None,
) -> str | None:
    """service 의 최신 commit_hash 조회 — load_source 의 SHA 자동 결정용.

    kind/sub_kind 지정 시 해당 record 들의 최신, 미지정 시 service 전체 최신.
    """
    pool = get_pool()
    if pool is None or not service_id:
        return None

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if kind and sub_kind:
                cur.execute(
                    """
                    SELECT commit_hash FROM metadata_indices
                    WHERE service_id = %s AND kind = %s AND sub_kind = %s
                    ORDER BY scanned_at DESC LIMIT 1
                    """,
                    (service_id, kind, sub_kind),
                )
            else:
                cur.execute(
                    """
                    SELECT commit_hash FROM metadata_indices
                    WHERE service_id = %s
                    ORDER BY scanned_at DESC LIMIT 1
                    """,
                    (service_id,),
                )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        _logger.warning(
            "metadata_latest_commit_failed",
            service_id=service_id, error=str(e),
        )
        return None
