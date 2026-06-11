"""defects 테이블 INSERT — root_cause_results + fix_results 를 join 해서 row 생성.

파이프라인의 _report 노드 끝에서 호출. mismatch 있는 TC 별로 한 row.
DB 미연결 / service_id 미존재 시 graceful no-op.

Author: C
Created: 2026-06-05
"""

from __future__ import annotations

import uuid

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.defect_writer")

# cross_check error_code 의 prefix → defects.category 매핑.
_CATEGORY_PREFIX = {
    "UI": "UI_ERROR",
    "API": "API_ERROR",
    "DATA": "DATA_MISMATCH",
    "DB": "DATA_MISMATCH",
    "INFRA": "INFRA",
    "DOMAIN": "DOMAIN_RULE",
    "RULE": "DOMAIN_RULE",
}


def _infer_category(error_code: str | None) -> str:
    if not error_code:
        return "UI_ERROR"
    head = error_code.upper().split("_")[0]
    return _CATEGORY_PREFIX.get(head, "UI_ERROR")


def _ts_from_tc(tc_id: str) -> str:
    """'TS-002-TC-05' → 'TS-002'."""
    parts = tc_id.split("-TC-")
    return parts[0] if len(parts) >= 2 else tc_id


def insert_defects(
    service_id: str,
    run_id: str,
    cross_check_results: list[dict],
    root_cause_results: list[dict],
    fix_results: list[dict],
) -> int:
    """mismatch 가 있는 각 TC 마다 defect row INSERT. 반환: 박힌 row 수."""
    pool = get_pool()
    if pool is None or not (service_id and run_id):
        return 0

    # tc_id → cross_check (error_code 추출용)
    cc_by_tc = {cc.get("tc_id"): cc for cc in cross_check_results if cc.get("tc_id")}
    # tc_id → fix_result (suggestion 추출용)
    fix_by_tc = {fr.get("tc_id"): fr for fr in fix_results if fr.get("tc_id")}

    rows: list[tuple] = []
    for rc in root_cause_results:
        tc_id = rc.get("tc_id")
        if not tc_id:
            continue
        candidates = rc.get("candidates") or []
        if not candidates:
            continue
        top = candidates[0]

        cc = cc_by_tc.get(tc_id) or {}
        # 결정적 1차 분류 (pipeline._classify_failure) 가 있으면 그것이 진실 —
        # TEST_DEFECT/ENV 계열을 SUT defect (UI_ERROR) 로 오기록하던 격차 해소.
        category = rc.get("category") or _infer_category(cc.get("error_code"))

        fr = fix_by_tc.get(tc_id) or {}
        suggestions = fr.get("suggestions") or []
        top_fix = suggestions[0] if suggestions else {}
        solution_guide = top_fix.get("description")
        assignee = top_fix.get("blame_author")
        file_path = top_fix.get("file_path")
        line_no = top_fix.get("line_number")
        file_location = (
            f"{file_path}:{line_no}" if file_path and line_no is not None
            else file_path
        )

        rows.append((
            str(uuid.uuid4()),
            service_id,
            run_id,
            _ts_from_tc(tc_id),
            tc_id,
            category,
            top.get("cause"),
            float(top.get("confidence") or 0.0),
            solution_guide,
            assignee,
            file_location,
        ))

    if not rows:
        return 0

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO defects (
                    id, service_id, run_id, ts_id, tc_id, category,
                    root_cause_top1, root_cause_confidence,
                    solution_guide, assignee, file_location
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
        _logger.info("defects_persisted", service_id=service_id, run_id=run_id, count=len(rows))
        return len(rows)
    except Exception as e:
        _logger.warning(
            "defects_persist_failed",
            service_id=service_id,
            run_id=run_id,
            error=str(e),
        )
        return 0
