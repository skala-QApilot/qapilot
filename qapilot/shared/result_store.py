"""trace 기반 테스트 결과 조회 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import csv
import io

from qapilot.shared.trace_store import list_traces, load_trace


def load_result(service: dict, trace_id: str) -> dict | None:
    """trace_id에 해당하는 결과를 반환한다."""
    trace = load_trace(service, trace_id)
    return _trace_to_result(trace) if trace else None


def load_all_results(service: dict) -> list[dict]:
    """test command trace를 결과 목록으로 반환한다."""
    return [
        _trace_to_result(trace)
        for trace in list_traces(service)
        if trace.get("command") == "test"
    ]


def get_result_statistics(service: dict) -> dict:
    """결과 통계를 반환한다."""
    results = load_all_results(service)
    passed = len([r for r in results if r.get("status") == "completed"])
    failed = len([r for r in results if r.get("status") == "failed"])
    total = len(results)
    pass_rate = round((passed / total) * 100, 2) if total else None
    return {"total": total, "passed": passed, "failed": failed, "pass_rate": pass_rate}


def export_result_csv(service: dict, trace_id: str) -> str:
    """단일 결과를 CSV 문자열로 반환한다."""
    result = load_result(service, trace_id)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["trace_id", "command", "status", "started_at", "completed_at", "total_cost"],
    )
    writer.writeheader()
    if result:
        writer.writerow({field: result.get(field, "") for field in writer.fieldnames})
    return output.getvalue()


def _trace_to_result(trace: dict) -> dict:
    return {
        "trace_id": trace["trace_id"],
        "command": trace.get("command"),
        "status": trace.get("status"),
        "started_at": trace.get("started_at"),
        "completed_at": trace.get("completed_at"),
        "error": trace.get("error"),
        "confidence": trace.get("confidence"),
        "total_cost": trace.get("total_cost", 0.0),
        "result_summary": trace.get("result_summary", {}),
    }
