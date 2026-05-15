"""추가 대시보드 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.rtm_store import load_all_rtm_versions
from qapilot.shared.service_store import get_service_by_id
from qapilot.shared.trace_store import list_traces

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/pass-rate-history")
async def pass_rate_history(
    service_id: str,
    limit: int = 30,
    user: dict = Depends(get_current_user),
) -> Any:
    """최근 completed trace의 pass rate 시계열을 반환한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    traces = [t for t in list_traces(service) if t.get("status") == "completed"]
    traces = sorted(traces, key=lambda t: t.get("started_at", ""))[-limit:]
    return ok({"history": [_history_item(trace) for trace in traces]})


@router.get("/rtm-summary")
async def rtm_summary(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """최신 RTM 요약을 반환한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    versions = load_all_rtm_versions(service)
    latest = versions[0] if versions else None
    return ok(
        {
            "rtm_summary": latest.get("summary") if latest else None,
            "version_id": latest.get("rtm_version_id") if latest else None,
            "version_label": latest.get("label") if latest else None,
        }
    )


@router.get("/llm-usage")
async def llm_usage(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """trace total_cost 기반 LLM 사용량을 반환한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    traces = list_traces(service)
    total_cost = round(sum(float(t.get("total_cost", 0.0) or 0.0) for t in traces), 6)
    count = len(traces)
    average = round(total_cost / count, 6) if count else None
    return ok({"total_cost_usd": total_cost, "trace_count": count, "average_cost_usd": average})


def _history_item(trace: dict) -> dict:
    summary = trace.get("result_summary", {})
    total = int(summary.get("ui_results_count", 0) or 0)
    return {
        "date": str(trace.get("started_at", ""))[:10],
        "trace_id": trace.get("trace_id"),
        "pass_rate": None,
        "total": total,
    }


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")
