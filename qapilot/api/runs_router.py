"""service scope 테스트 실행 API 라우터.

Author: C
Created: 2026-05-15
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.errors import ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import RunOptions
from qapilot.shared.service_store import get_service_by_id
from qapilot.shared.trace_store import (
    create_trace,
    list_traces,
    load_trace,
    update_trace,
    update_trace_failed,
)

router = APIRouter(prefix="/api/services/{service_id}", tags=["runs"])
logger = get_logger("api.runs")


@router.post("/runs")
async def create_run(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """테스트 실행을 생성하고 백그라운드로 파이프라인을 실행한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    trace_id = request.state.trace_id
    options = _run_options(body)
    create_trace(service, trace_id, "test", None)
    asyncio.create_task(_run_pipeline_task(service, trace_id, options))
    logger.info("run_created", user_id=user["user_id"], run_id=trace_id)
    return JSONResponse(
        status_code=202,
        content=ok({"run_id": trace_id, "status": "running"}),
    )


@router.get("/runs/active")
async def list_active_runs(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """실행 중인 run 목록을 조회한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()
    runs = [_trace_to_run(t) for t in list_traces(service) if t.get("status") == "running"]
    logger.info("active_runs_listed", user_id=user["user_id"], count=len(runs))
    return ok({"runs": runs, "count": len(runs)})


@router.get("/runs/{run_id}")
async def get_run(
    service_id: str,
    run_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """run 상세를 조회한다."""
    trace = _load_trace_or_none(service_id, run_id)
    if isinstance(trace, JSONResponse):
        return trace
    logger.info("run_fetched", user_id=user["user_id"], run_id=run_id)
    return ok({"run": _trace_to_run(trace)})


@router.patch("/runs/{run_id}")
async def patch_run(
    service_id: str,
    run_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """run 상태를 변경한다. MVP에서는 stopped만 지원한다."""
    loaded = _load_service_and_trace(service_id, run_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, trace = loaded
    body = await _json_body(request)
    if body.get("status") != "stopped":
        return fail("REQUEST_400", "status는 stopped만 지원합니다.")
    trace["status"] = "stopped"
    _save_trace(service, run_id, trace)
    logger.info("run_stopped", user_id=user["user_id"], run_id=run_id)
    return ok({"run": _trace_to_run(trace)})


@router.get("/runs/{run_id}/agent-progress")
async def get_agent_progress(
    service_id: str,
    run_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """run의 Agent 실행 진행 상태를 조회한다."""
    trace = _load_trace_or_none(service_id, run_id)
    if isinstance(trace, JSONResponse):
        return trace
    logger.info("run_agent_progress_fetched", user_id=user["user_id"], run_id=run_id)
    return ok(
        {
            "stages": _extract_stages(trace),
            "pass": _count_pass(trace),
            "fail": _count_fail(trace),
            "hitl_pending": 0,
        }
    )


async def _run_pipeline_task(service: dict, trace_id: str, options: RunOptions) -> None:
    """백그라운드에서 파이프라인을 실행하고 trace를 갱신한다."""
    try:
        state = await run_pipeline(options, trace_id=trace_id)
        update_trace(service, trace_id, dict(state))
    except Exception as e:
        update_trace_failed(service, trace_id, f"{type(e).__name__}: {e}")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _run_options(body: dict[str, Any]) -> RunOptions:
    return {
        "command": "test",
        "trigger": None,
        "user_input": None,
        "scenario_ids": _optional_list(body, "scenario_ids"),
        "filter": str(body.get("filter") or "all"),
        "tags": _optional_list(body, "tags"),
    }


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


def _extract_stages(trace: dict) -> list[dict]:
    stages_map = {
        "ui_test": "UI",
        "api_trace": "API",
        "db_test": "DB",
        "cross_check": "Cross-check",
        "defect_classifier": "원인 분석",
        "report": "Report 생성",
    }
    stages = []
    for log in trace.get("agent_logs", []):
        name = str(log.get("agent_name", ""))
        if name in stages_map:
            stages.append({"key": name, "name": stages_map[name], "status": "completed"})
    return stages


def _count_pass(trace: dict) -> int:
    return int(trace.get("result_summary", {}).get("pass_count", 0) or 0)


def _count_fail(trace: dict) -> int:
    return int(trace.get("result_summary", {}).get("fail_count", 0) or 0)


def _optional_list(body: dict[str, Any], key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    return [str(item) for item in value] if isinstance(value, list) else None


def _load_trace_or_none(service_id: str, run_id: str) -> dict | JSONResponse:
    loaded = _load_service_and_trace(service_id, run_id)
    return loaded if isinstance(loaded, JSONResponse) else loaded[1]


def _load_service_and_trace(service_id: str, run_id: str) -> tuple[dict, dict] | JSONResponse:
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()
    trace = load_trace(service, run_id)
    return (service, trace) if trace else _run_not_found()


def _save_trace(service: dict, run_id: str, trace: dict) -> None:
    path = Path(str(service["qapilot_dir"])) / "traces" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _run_not_found() -> JSONResponse:
    return fail(ErrorCode.RUN_001, "실행을 찾을 수 없습니다.")
