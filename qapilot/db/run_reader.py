"""runs 테이블 READ helper.

Author: C
Created: 2026-06-04
"""

from __future__ import annotations

import json
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.run_reader")


def load_run(trace_id: str) -> dict | None:
    """runs 테이블에서 trace_id 로 조회해 trace dict 형태로 반환한다."""
    pool = get_pool()
    if pool is None or not trace_id:
        return None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, service_id, command, trigger, status,
                       started_at, completed_at, error,
                       confidence, total_cost, selected_total_tc_count,
                       options, summary, agent_logs
                FROM runs WHERE id = %s
                """,
                (trace_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        _logger.warning("run_load_failed", trace_id=trace_id, error=str(e))
        return None

    if not row:
        return None

    (
        run_id, service_id, command, trigger, status,
        started_at, completed_at, error,
        confidence, total_cost, selected_total_tc_count,
        options_raw, summary_raw, agent_logs_raw,
    ) = row

    options = _parse_json(options_raw) or {}
    summary = _parse_json(summary_raw) or {}
    agent_logs = _parse_json(agent_logs_raw) or []

    trace: dict[str, Any] = {
        "trace_id": str(run_id),
        "service_id": str(service_id) if service_id else None,
        "command": command,
        "trigger": trigger,
        "status": status,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "error": error,
        "confidence": confidence,
        "total_cost": total_cost,
        "selected_total_tc_count": selected_total_tc_count,
        "result_summary": summary,
        "agent_logs": agent_logs if isinstance(agent_logs, list) else [],
    }
    # options JSONB 에 저장된 실행 옵션을 최상위 키로 풀어준다.
    for key in (
        "scenario_ids", "filter", "tags", "staging_url",
        "resume_from_trace", "test_account", "domain_files",
    ):
        if key in options:
            trace[key] = options[key]

    # summary 에 tc_results / scenario_results 가 있으면 복원.
    for key in ("tc_results", "scenario_results"):
        if key in summary:
            trace[key] = summary[key]

    return trace


def load_runs_by_service(service_id: str) -> list[dict]:
    """service_id 기준 runs 목록을 started_at DESC 로 반환한다."""
    pool = get_pool()
    if pool is None or not service_id:
        return []
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM runs
                WHERE service_id = %s
                ORDER BY started_at DESC
                LIMIT 200
                """,
                (service_id,),
            )
            rows = cur.fetchall()
    except Exception as e:
        _logger.warning("runs_list_failed", service_id=service_id, error=str(e))
        return []

    result = []
    for (run_id,) in rows:
        trace = load_run(str(run_id))
        if trace:
            result.append(trace)
    return result


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
