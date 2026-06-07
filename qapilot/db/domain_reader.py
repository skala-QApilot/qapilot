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

    반환: [{"id": str, "filename": str, "s3_key": str, "mime_type": str | None}, ...]
    """
    pool = get_pool()
    if pool is None or not service_id:
        return []

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (file_id) id, filename, s3_key, mime_type
                FROM domain_documents
                WHERE service_id = %s
                ORDER BY file_id, version DESC
                """,
                (service_id,),
            )
            rows = cur.fetchall()
        return [{"id": str(r[0]), "filename": r[1], "s3_key": r[2], "mime_type": r[3]} for r in rows]
    except Exception as e:
        _logger.warning("domain_documents_query_failed", service_id=service_id, error=str(e))
        return []


def mark_reflected(document_id: str) -> None:
    """문서를 '시나리오 반영됨' 상태로 표시.

    도메인 지식 임포트(임베딩)가 성공적으로 끝나 해당 문서 내용이 시나리오 생성에
    실제로 사용된 시점에 호출한다. UI 의 '미반영' 태그가 그대로 남는 문제(파일
    업로드 시점에는 아직 반영 여부를 알 수 없으므로 reflected 기본값은 false)를
    임포트 성공 시점에 갱신해 해소한다.
    """
    pool = get_pool()
    if pool is None or not document_id:
        return
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE domain_documents SET reflected = true WHERE id = %s AND reflected = false",
                (document_id,),
            )
            conn.commit()
    except Exception as e:
        _logger.warning("domain_document_mark_reflected_failed", document_id=document_id, error=str(e))
