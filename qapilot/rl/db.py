"""RL 모듈의 DB 접근 — psycopg 동기 풀 사용 (`qapilot.db.connection.get_pool`).

- 완료된 런의 `tc_results` / `defects` / `scenarios` 를 읽어 `RunOutcome` 으로 조립.
- RL 전용 테이블(`rl_experiences`, `rl_bandit_arms`) DDL(IF NOT EXISTS).

풀이 없으면(get_pool() is None) 모든 함수가 안전하게 None/0 을 반환한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from qapilot.rl.schemas import RunOutcome

logger = logging.getLogger("qapilot.rl.db")


# 시나리오 name → coarse 도메인 영역 (domain_area 가 비었을 때의 fallback).
# 키워드는 우선순위 순서대로 검사한다.
_AREA_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("회원·인증", ("회원가입", "로그인", "로그아웃", "인증", "권한", "비밀번호", "계정")),
    ("결제·청구", ("결제", "청구", "요금", "납부", "정산", "환불")),
    ("가입·변경", ("가입", "해지", "변경", "신청", "약정")),
    ("프로모션", ("쿠폰", "할인", "프로모", "이벤트", "포인트", "멤버십", "등급")),
    ("조회·검색", ("조회", "검색", "목록", "상세")),
    ("성능", ("성능", "기동", "부하", "응답", "동시")),
    ("알림", ("알림", "메일", "문자", "푸시")),
]


def infer_area(payload: dict[str, Any]) -> str:
    """시나리오 payload 에서 도메인 영역을 해석한다.

    1) domain_area / domain / area / category 중 채워진 값 → 그대로 사용
    2) 없으면 name 키워드로 coarse 영역 추론 (`_AREA_KEYWORDS`)
    3) 그래도 없으면 'unknown'
    """
    for k in ("domain_area", "domain", "area", "category"):
        v = payload.get(k)
        if v and str(v).strip():
            return str(v).strip()
    name = str(payload.get("name") or payload.get("description") or "")
    for area, kws in _AREA_KEYWORDS:
        if any(kw in name for kw in kws):
            return area
    return "unknown"


def _get_pool():
    try:
        from qapilot.db.connection import get_pool

        return get_pool()
    except Exception as e:  # noqa: BLE001
        logger.info("RL DB: 풀 사용 불가 (%s)", e)
        return None


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS rl_experiences (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    service_id  TEXT,
    ts_id       TEXT,
    domain_area TEXT,
    action      TEXT,
    reward      DOUBLE PRECISION,
    components  JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rl_exp_service ON rl_experiences(service_id);
CREATE INDEX IF NOT EXISTS idx_rl_exp_run     ON rl_experiences(run_id);

CREATE TABLE IF NOT EXISTS rl_bandit_arms (
    service_id  TEXT NOT NULL,
    domain_area TEXT NOT NULL,
    alpha       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    beta        DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service_id, domain_area)
);
"""


def ensure_schema() -> bool:
    """RL 테이블을 생성(IF NOT EXISTS)한다. 성공 시 True."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("RL DB: ensure_schema 실패 (%s)", e)
        return False


# ---------------------------------------------------------------------------
# 런 관측 읽기
# ---------------------------------------------------------------------------


def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_run_outcome(
    run_id: str,
    service_id: str | None = None,
    domain: str | None = None,
) -> RunOutcome | None:
    """run_id 의 tc_results/defects/scenarios 를 읽어 RunOutcome 조립. 풀 없으면 None."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, ts_id, tc_id, kind, status, payload "
                    "FROM tc_results WHERE run_id = %s",
                    (run_id,),
                )
                tc_rows = _rows_as_dicts(cur)

                cur.execute(
                    "SELECT run_id, ts_id, tc_id, category, root_cause_top1, "
                    "root_cause_confidence FROM defects WHERE run_id = %s",
                    (run_id,),
                )
                defect_rows = _rows_as_dicts(cur)

                area_by_ts = _fetch_area_by_ts(cur, service_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("RL DB: fetch_run_outcome 실패 (%s)", e)
        return None

    return RunOutcome.from_rows(
        run_id=run_id,
        tc_rows=tc_rows,
        defect_rows=defect_rows,
        area_by_ts=area_by_ts,
        service_id=service_id,
        domain=domain or service_id,
    )


def _fetch_area_by_ts(cur, service_id: str | None) -> dict[str, str]:
    """scenarios.payload->>'domain_area' 로 ts_id → 영역 매핑(최신 버전)."""
    try:
        if service_id:
            cur.execute(
                "SELECT DISTINCT ON (ts_id) ts_id, payload "
                "FROM scenarios WHERE service_id = %s AND COALESCE(is_deleted, false) = false "
                "ORDER BY ts_id, version_number DESC",
                (service_id,),
            )
        else:
            cur.execute(
                "SELECT DISTINCT ON (ts_id) ts_id, payload "
                "FROM scenarios WHERE COALESCE(is_deleted, false) = false "
                "ORDER BY ts_id, version_number DESC"
            )
        out: dict[str, str] = {}
        for ts_id, payload in cur.fetchall():
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:  # noqa: BLE001
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            out[str(ts_id)] = infer_area(payload)
        return out
    except Exception as e:  # noqa: BLE001
        logger.info("RL DB: scenarios 영역 매핑 생략 (%s)", e)
        return {}
