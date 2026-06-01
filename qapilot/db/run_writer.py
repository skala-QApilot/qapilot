"""runs 테이블 UPSERT — trace_store 의 file write 와 dual-write.

전체 trace dict 를 받아 INSERT … ON CONFLICT DO UPDATE 1회로 처리.
create/update/annotate/abort 모두 같은 함수로 흡수된다.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.run_writer")

# trace dict 의 어떤 top-level 키를 runs.options JSONB 로 묶을지.
# annotate_trace 가 보존하는 실행 옵션 그룹과 일치.
_OPTIONS_KEYS = ("scenario_ids", "filter", "tags", "staging_url", "resume_from_trace")


def upsert_run(trace: dict) -> None:
    """trace dict 을 runs 테이블에 UPSERT.

    DB 미연결 / 외래키 위반 (service_id 가 DB 에 없음) 등 실패는 모두 warn 로깅 후 swallow —
    파일 기록이 source of truth 이므로 사용자 흐름은 중단되지 않는다.
    """
    pool = get_pool()
    if pool is None:
        return
    if not trace.get("trace_id"):
        return

    try:
        params = _to_params(trace)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, params)
    except Exception as e:
        _logger.warning(
            "run_db_mirror_failed",
            trace_id=trace.get("trace_id"),
            error=str(e),
        )


# ─────────────────────────────────────────────────────────────────────────────
# SQL & param 변환
# ─────────────────────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO runs (
    id, service_id, command, trigger, status,
    started_at, completed_at, error,
    confidence, total_cost, selected_total_tc_count,
    options, summary, agent_logs
) VALUES (
    %(trace_id)s, %(service_id)s, %(command)s, %(trigger)s, %(status)s,
    %(started_at)s, %(completed_at)s, %(error)s,
    %(confidence)s, %(total_cost)s, %(selected_total_tc_count)s,
    %(options)s::jsonb, %(summary)s::jsonb, %(agent_logs)s::jsonb
)
ON CONFLICT (id) DO UPDATE SET
    status = EXCLUDED.status,
    completed_at = EXCLUDED.completed_at,
    error = EXCLUDED.error,
    confidence = EXCLUDED.confidence,
    total_cost = EXCLUDED.total_cost,
    selected_total_tc_count = EXCLUDED.selected_total_tc_count,
    options = COALESCE(EXCLUDED.options, runs.options),
    summary = COALESCE(EXCLUDED.summary, runs.summary),
    agent_logs = COALESCE(EXCLUDED.agent_logs, runs.agent_logs),
    updated_at = now()
"""


def _to_params(trace: dict) -> dict:
    options = {key: trace[key] for key in _OPTIONS_KEYS if key in trace}
    return {
        "trace_id": trace["trace_id"],
        "service_id": trace.get("service_id"),
        "command": trace.get("command") or "unknown",
        "trigger": trace.get("trigger") or "unknown",
        "status": trace.get("status") or "running",
        "started_at": _parse_dt(trace.get("started_at")),
        "completed_at": _parse_dt(trace.get("completed_at")),
        "error": trace.get("error"),
        "confidence": trace.get("confidence"),
        "total_cost": trace.get("total_cost"),
        "selected_total_tc_count": trace.get("selected_total_tc_count"),
        "options": _json(options) if options else None,
        "summary": _json(trace.get("result_summary")) if trace.get("result_summary") else None,
        "agent_logs": _json(trace.get("agent_logs")) if trace.get("agent_logs") else None,
    }


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    s = value.rstrip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
