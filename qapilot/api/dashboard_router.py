"""대시보드 요약 API 라우터.

Author: C
Created: 2026-05-15
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.logger import get_logger
from qapilot.shared.scenario_store import load_all_scenarios
from qapilot.shared.service_store import get_service_by_id
from qapilot.shared.trace_store import list_traces

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
logger = get_logger("api.dashboard")


@router.get("/summary")
async def dashboard_summary(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """파일 기반으로 계산 가능한 대시보드 요약을 반환한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()

    traces = list_traces(service)
    summary = {
        "scenarios_count": len(load_all_scenarios(service)),
        "recent_runs": [_trace_to_run(t) for t in traces[:5]],
        "domain_files": _domain_files(service),
        "rtm_summary": None,
        "pass_rate": _pass_rate(traces),
    }
    logger.info("dashboard_summary_fetched", user_id=user["user_id"], service_id=service_id)
    return ok(summary)


def _domain_files(service: dict) -> list[str]:
    domain_dir = Path(str(service["qapilot_dir"])) / "domain"
    if not domain_dir.exists():
        return []
    return sorted(path.name for path in domain_dir.iterdir() if path.is_file())


def _pass_rate(traces: list[dict]) -> float | None:
    completed = [t for t in traces if t.get("status") == "completed"]
    usable = [t for t in completed if t.get("result_summary", {}).get("ui_results_count", 0) > 0]
    return None if not usable else None


def _trace_to_run(trace: dict) -> dict:
    trace_id = trace["trace_id"]
    return {
        "id": trace_id,
        "name": f"{trace['command']} - {trace_id[:8]}",
        "status": trace["status"],
        "startTime": trace.get("started_at"),
        "completedAt": trace.get("completed_at"),
        "error": trace.get("error"),
        "result_summary": trace.get("result_summary", {}),
    }


def _service_not_found() -> JSONResponse:
    return fail("SERVICE_001", "서비스를 찾을 수 없습니다.")
