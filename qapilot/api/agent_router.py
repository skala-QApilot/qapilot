"""Agent 실행 API 라우터.

Author: C
Created: 2026-05-15
"""

import asyncio
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
    load_trace,
    update_trace,
    update_trace_failed,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])
logger = get_logger("api.agent")


@router.post("/scenario-generation")
async def scenario_generation(
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 생성 파이프라인을 백그라운드로 실행한다."""
    body = await _json_body(request)
    error = _validate_scenario_generation(body)
    if error:
        return error

    service = get_service_by_id(str(body["service_id"]))
    if not service:
        return _service_not_found()

    trigger = str(body["trigger"])
    options: RunOptions = _run_options(body, trigger)
    return _start_pipeline(request, service, options, trigger, user["user_id"])


@router.post("/code-change-detection")
async def code_change_detection(
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """코드 변경 감지 기반 시나리오 생성 파이프라인을 백그라운드로 실행한다."""
    body = await _json_body(request)
    if not str(body.get("service_id") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "service_id 필드가 필요합니다.")

    service = get_service_by_id(str(body["service_id"]))
    if not service:
        return _service_not_found()

    options: RunOptions = {
        "command": "generate_scenarios",
        "trigger": "code_change",
        "user_input": None,
        "scenario_ids": None,
        "filter": None,
        "tags": None,
    }
    return _start_pipeline(request, service, options, "code_change", user["user_id"])


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """trace_id 기준 Agent 실행 상태를 조회한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()

    trace = load_trace(service, trace_id)
    if not trace:
        return fail(ErrorCode.AGENT_API_002, "trace를 찾을 수 없습니다.")

    logger.info("agent_trace_fetched", user_id=user["user_id"], trace_id=trace_id)
    return ok({"trace": trace})


@router.post("/scenario-chat")
async def scenario_chat(
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """MVP용 시나리오 채팅 안내 응답을 반환한다."""
    body = await _json_body(request)
    service_id = str(body.get("service_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not service_id:
        return fail(ErrorCode.AGENT_API_003, "service_id 필드가 필요합니다.")
    if not message:
        return fail(ErrorCode.AGENT_API_003, "message 필드가 필요합니다.")
    if not get_service_by_id(service_id):
        return _service_not_found()

    quick_action = _optional_str(body, "quick_action")
    reply = _chat_reply(message, quick_action)
    logger.info("scenario_chat_replied", user_id=user["user_id"], quick_action=quick_action)
    return ok(
        {
            "reply": reply,
            "trace_id": request.state.trace_id,
            "quick_action": quick_action,
            "suggestions": [],
        }
    )


async def _run_pipeline_task(
    service: dict,
    trace_id: str,
    options: RunOptions,
) -> None:
    """백그라운드에서 파이프라인을 실행하고 trace를 갱신한다."""
    try:
        state = await run_pipeline(options, trace_id=trace_id)
        update_trace(service, trace_id, dict(state))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        update_trace_failed(service, trace_id, error)
        logger.error("agent_pipeline_task_failed", trace_id=trace_id, error=error)


def _start_pipeline(
    request: Request,
    service: dict,
    options: RunOptions,
    trigger: str | None,
    user_id: str,
) -> JSONResponse:
    trace_id = request.state.trace_id
    create_trace(service, trace_id, options["command"], trigger)
    asyncio.create_task(_run_pipeline_task(service, trace_id, options))
    logger.info("agent_pipeline_started", user_id=user_id, trace_id=trace_id)
    content = {"success": True, "data": {"trace_id": trace_id, "status": "running"}}
    content["trace_id"] = trace_id
    return JSONResponse(status_code=202, content=content)


def _run_options(body: dict[str, Any], trigger: str) -> RunOptions:
    return {
        "command": "generate_scenarios",
        "trigger": trigger,
        "user_input": _optional_str(body, "user_input"),
        "scenario_ids": _optional_list(body, "scenario_ids"),
        "filter": _optional_str(body, "filter"),
        "tags": _optional_list(body, "tags"),
    }


def _validate_scenario_generation(body: dict[str, Any]) -> JSONResponse | None:
    if not str(body.get("service_id") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "service_id 필드가 필요합니다.")
    if not str(body.get("trigger") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "trigger 필드가 필요합니다.")
    if body.get("trigger") == "natural_lang" and not str(body.get("user_input") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "user_input 필드가 필요합니다.")
    return None


async def _json_body(request: Request) -> dict[str, Any]:
    """요청 JSON body를 안전하게 읽는다."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _optional_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _optional_list(body: dict[str, Any], key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    return [str(item) for item in value] if isinstance(value, list) else None


def _chat_reply(message: str, quick_action: str | None) -> str:
    if quick_action == "generate":
        return "시나리오 생성을 시작합니다. /api/agent/scenario-generation을 호출하세요."
    if quick_action == "detect":
        return "코드 변경 감지를 시작합니다. /api/agent/code-change-detection을 호출하세요."
    if quick_action:
        return "요청을 처리할 수 없습니다."
    return f"'{message}'를 받았습니다. 현재 채팅 기반 시나리오 수정은 준비 중입니다."


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")
