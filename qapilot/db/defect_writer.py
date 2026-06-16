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

# defects.category CHECK 제약과 동기 (qapilot-server V15 migration).
# 신규 4종은 Layer 3 결정 분류 (_classify_failure) — V15 미적용 DB 에서는
# CHECK 위반으로 batch 전체가 유실되므로, 허용 집합 밖이면 legacy 추론으로
# 강등해서라도 기록한다 (전량 유실 < 분류 정밀도 손실).
_ALLOWED_CATEGORIES = {
    "UI_ERROR", "API_ERROR", "DATA_MISMATCH", "INFRA", "DOMAIN_RULE",
    "TEST_DEFECT_MAPPING", "TEST_DEFECT_UNVERIFIABLE", "ENV_TIMEOUT", "ENV_UNVERIFIED", "PRODUCT_DEFECT_CANDIDATE",
}

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


# ②결정분류 중 '제품 결함'을 가리키는 값 — 이 경우에만 ①장애유형을 산출한다.
# TEST_DEFECT_*/ENV_* 는 제품 장애가 아니므로 ①장애유형이 없다(None).
_PRODUCT_DECISIONS = {
    "PRODUCT_DEFECT_CANDIDATE",
    "UI_ERROR", "API_ERROR", "DATA_MISMATCH", "INFRA", "DOMAIN_RULE",
}


def _infer_defect_type(error_code: str | None) -> str | None:
    """①장애유형 — cross_check error_code prefix 로 결정적 산출.

    _infer_category 와 달리 단서(error_code/매핑) 가 없으면 None 을 반환한다.
    측정에서 ①을 임의 기본값(UI_ERROR)으로 채워 정확도를 부풀리지 않기 위함.
    """
    if not error_code:
        return None
    head = error_code.upper().split("_")[0]
    return _CATEGORY_PREFIX.get(head)


def _ts_from_tc(tc_id: str) -> str:
    """'TS-002-TC-05' → 'TS-002'."""
    parts = tc_id.split("-TC-")
    return parts[0] if len(parts) >= 2 else tc_id


def _build_blame_map(blame: list[dict]) -> dict[str, str]:
    """GitDiff.blame → {file: 추천 담당자(author_email 우선, 없으면 author 이름)}."""
    mapping: dict[str, str] = {}
    for entry in blame:
        file = entry.get("file")
        who = entry.get("author_email") or entry.get("author")
        if file and who:
            mapping[file] = who
    return mapping


def _resolve_assignee(
    blame_map: dict[str, str], file_path: str | None, blame_author: str | None
) -> str | None:
    """fix suggestion 의 blame_author 우선, 없으면 file_path 로 blame_map 조회.

    blame_map 은 정확한 경로로 먼저 조회하고, 매칭이 없으면 basename 으로 재시도한다
    (LLM 이 생성한 file_path 와 git_diff.changed_files 경로의 prefix 가 다를 수 있음).
    """
    if blame_author:
        return blame_author
    if not file_path:
        return None
    if file_path in blame_map:
        return blame_map[file_path]
    base = file_path.rsplit("/", 1)[-1]
    for f, who in blame_map.items():
        if f.rsplit("/", 1)[-1] == base:
            return who
    return None


def insert_defects(
    service_id: str,
    run_id: str,
    cross_check_results: list[dict],
    root_cause_results: list[dict],
    fix_results: list[dict],
    blame: list[dict] | None = None,
) -> int:
    """mismatch 가 있는 각 TC 마다 defect row INSERT. 반환: 박힌 row 수."""
    pool = get_pool()
    if pool is None or not (service_id and run_id):
        return 0

    # tc_id → cross_check (error_code 추출용)
    cc_by_tc = {cc.get("tc_id"): cc for cc in cross_check_results if cc.get("tc_id")}
    # tc_id → fix_result (suggestion 추출용)
    fix_by_tc = {fr.get("tc_id"): fr for fr in fix_results if fr.get("tc_id")}
    # file → 추천 담당자 (git 이력 기준)
    blame_map = _build_blame_map(blame or [])

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
        if category not in _ALLOWED_CATEGORIES:
            category = _infer_category(cc.get("error_code"))

        # ①장애유형 — 파이프라인 root_cause 가 채운 defect_type(상태코드+도메인규칙 신호)
        # 우선, 없으면 error_code prefix 폴백. 제품 결함(②결정분류)일 때만.
        defect_type = (
            (rc.get("defect_type") or _infer_defect_type(cc.get("error_code")))
            if category in _PRODUCT_DECISIONS else None
        )

        fr = fix_by_tc.get(tc_id) or {}
        suggestions = fr.get("suggestions") or []
        top_fix = suggestions[0] if suggestions else {}
        solution_guide = top_fix.get("description")
        file_path = top_fix.get("file_path")
        assignee = _resolve_assignee(blame_map, file_path, top_fix.get("blame_author"))
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
            defect_type,
            top.get("cause"),
            float(top.get("confidence") or 0.0),
            solution_guide,
            assignee,
            file_location,
        ))

    if not rows:
        return 0

    _INSERT_SQL = """
        INSERT INTO defects (
            id, service_id, run_id, ts_id, tc_id, category, defect_type,
            root_cause_top1, root_cause_confidence,
            solution_guide, assignee, file_location
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, rows)
        _logger.info("defects_persisted", service_id=service_id, run_id=run_id, count=len(rows))
        return len(rows)
    except Exception as e:
        # batch 실패 시 row 단위 재시도 — 한 row 의 제약 위반 (run 544ab04d:
        # defects_category_check) 이 전체 run 의 defect 기록을 유실시키면 안 된다.
        _logger.warning(
            "defects_persist_batch_failed_retrying_rows",
            service_id=service_id, run_id=run_id, error=str(e),
        )
        inserted = 0
        for row in rows:
            try:
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, row)
                inserted += 1
            except Exception as row_e:
                _logger.warning(
                    "defect_row_persist_failed",
                    tc_id=row[4], category=row[5], error=str(row_e)[:120],
                )
        _logger.info("defects_persisted", service_id=service_id, run_id=run_id, count=inserted)
        return inserted
