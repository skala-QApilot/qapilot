"""defects 테이블 단건 조회/갱신 — GitHub issue 생성 흐름용.

Author: C
Created: 2026-06-15
"""

from __future__ import annotations

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.defect_reader")

_COLUMNS = (
    "id", "service_id", "run_id", "ts_id", "tc_id", "category",
    "root_cause_top1", "root_cause_confidence", "solution_guide",
    "assignee", "file_location", "status", "issue_url",
)


def load_defect(defect_id: str) -> dict | None:
    """defect_id 기준 defects row 1건을 dict로 반환한다. 없으면 None."""
    pool = get_pool()
    if pool is None or not defect_id:
        return None

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM defects WHERE id = %s",
                (defect_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip(_COLUMNS, row))
    except Exception as e:
        _logger.warning("defect_load_failed", defect_id=defect_id, error=str(e))
        return None


def update_defect_issue_url(defect_id: str, issue_url: str) -> bool:
    """defects.issue_url 을 갱신한다. 성공 시 True."""
    pool = get_pool()
    if pool is None or not defect_id:
        return False

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE defects SET issue_url = %s, updated_at = now() WHERE id = %s",
                (issue_url, defect_id),
            )
        return True
    except Exception as e:
        _logger.warning("defect_issue_url_update_failed", defect_id=defect_id, error=str(e))
        return False
