"""domain_documents 조회 — Spring 이 업로드한 PRD/정책 파일 메타데이터.

각 service 의 최신 버전 파일만 반환 (file_id 기준 max version).
DB 미연결 시 빈 리스트.

Author: C
Created: 2026-06-02
"""

from __future__ import annotations

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.domain_reader")


def list_latest_domain_documents(service_id: str) -> list[dict]:
    """service_id 의 도메인 파일 최신 버전 목록.

    반환: [{"filename": str, "s3_key": str, "mime_type": str | None}, ...]
    """
    pool = get_pool()
    if pool is None or not service_id:
        return []

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (file_id) filename, s3_key, mime_type
                FROM domain_documents
                WHERE service_id = %s
                ORDER BY file_id, version DESC
                """,
                (service_id,),
            )
            rows = cur.fetchall()
        return [{"filename": r[0], "s3_key": r[1], "mime_type": r[2]} for r in rows]
    except Exception as e:
        _logger.warning("domain_documents_query_failed", service_id=service_id, error=str(e))
        return []
