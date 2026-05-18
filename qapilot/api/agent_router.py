"""Agent 실행 전용 FastAPI 라우터.

Author: C
Created: 2026-05-15
"""

import asyncio
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from qapilot.api.internal_deps import verify_internal_token
from qapilot.api.response import fail, ok
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.errors import ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import RunOptions
from qapilot.shared.trace_store import (
    create_trace,
    load_trace,
    update_trace,
    update_trace_failed,
)

router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    dependencies=[Depends(verify_internal_token)],
)
logger = get_logger("api.agent")

_SCENARIO_TRIGGERS = {"init", "natural_lang", "doc_update"}
ScenarioTrigger = Literal["init", "natural_lang", "doc_update"]
RunFilter = Literal["all", "failed", "affected"]


@router.post("/scenario-generation")
async def scenario_generation(request: Request) -> Any:
    """시나리오 생성 파이프라인을 백그라운드로 실행한다."""
    body = await _json_body(request)
    error = _validate_scenario_generation(body)
    if error:
        return error

    options: RunOptions = {
        "command": "generate_scenarios",
        "trigger": _scenario_trigger(body),
        "user_input": _optional_str(body, "user_input"),
        "scenario_ids": _optional_list(body, "scenario_ids"),
        "filter": _optional_filter(body),
        "tags": _optional_list(body, "tags"),
    }
    return _start_pipeline(request, body, options)


@router.post("/code-change-detection")
async def code_change_detection(request: Request) -> Any:
    """코드 변경 감지 기반 시나리오 생성 파이프라인을 실행한다."""
    body = await _json_body(request)
    error = _validate_common_body(body)
    if error:
        return error

    options: RunOptions = {
        "command": "generate_scenarios",
        "trigger": "code_change",
        "user_input": None,
        "scenario_ids": None,
        "filter": None,
        "tags": None,
    }
    return _start_pipeline(request, body, options)


@router.post("/test-run")
async def test_run(request: Request) -> Any:
    """테스트 실행 파이프라인을 백그라운드로 실행한다."""
    body = await _json_body(request)
    error = _validate_common_body(body)
    if error:
        return error

    options: RunOptions = {
        "command": "test",
        "trigger": None,
        "user_input": None,
        "scenario_ids": _optional_list(body, "scenario_ids"),
        "filter": _optional_filter(body) or "all",
        "tags": _optional_list(body, "tags"),
    }
    return _start_pipeline(request, body, options)


@router.post("/scenario-chat")
async def scenario_chat(request: Request) -> Any:
    """MVP용 시나리오 채팅 안내 응답을 반환한다."""
    body = await _json_body(request)
    error = _validate_common_body(body)
    if error:
        return error

    message = str(body.get("message") or "").strip()
    if not message:
        return fail(ErrorCode.AGENT_API_003, "message 필드가 필요합니다.")

    quick_action = _optional_str(body, "quick_action")
    reply = _chat_reply(message, quick_action)
    logger.info(
        "scenario_chat_replied",
        service_id=body.get("service_id"),
        trace_id=request.state.trace_id,
        quick_action=quick_action,
    )
    return ok(
        {
            "reply": reply,
            "trace_id": request.state.trace_id,
            "quick_action": quick_action,
            "suggestions": [],
        }
    )


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    qapilot_dir: str = Query(...),
) -> Any:
    """trace_id 기준 Agent 실행 상태를 조회한다."""
    qapilot_path = _valid_qapilot_dir(qapilot_dir)
    if not qapilot_path:
        return _invalid_qapilot_dir()

    trace = load_trace(qapilot_path, trace_id)
    if not trace:
        return fail(ErrorCode.AGENT_API_002, "trace를 찾을 수 없습니다.")

    logger.info("agent_trace_fetched", trace_id=trace_id)
    return ok({"trace": trace})


async def _run_pipeline_task(
    qapilot_dir: Path,
    trace_id: str,
    options: RunOptions,
) -> None:
    """백그라운드에서 파이프라인을 실행하고 trace를 갱신한다."""
    try:
        state = await run_pipeline(options, trace_id=trace_id)
        update_trace(qapilot_dir, trace_id, dict(state))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        update_trace_failed(qapilot_dir, trace_id, error)
        logger.error("agent_pipeline_task_failed", trace_id=trace_id, error=error)


def _start_pipeline(request: Request, body: dict[str, Any], options: RunOptions) -> JSONResponse:
    qapilot_dir = _valid_qapilot_dir(str(body.get("qapilot_dir") or ""))
    if not qapilot_dir:
        return _invalid_qapilot_dir()

    trace_id = request.state.trace_id
    service_id = str(body.get("service_id") or "")
    create_trace(
        qapilot_dir,
        trace_id,
        options["command"],
        options["trigger"],
        service_id=service_id,
    )
    asyncio.create_task(_run_pipeline_task(qapilot_dir, trace_id, options))
    logger.info(
        "agent_pipeline_started",
        service_id=service_id,
        trace_id=trace_id,
        command=options["command"],
    )
    content = {"success": True, "data": {"trace_id": trace_id, "status": "running"}}
    content["trace_id"] = trace_id
    return JSONResponse(status_code=202, content=content)


def _validate_scenario_generation(body: dict[str, Any]) -> JSONResponse | None:
    error = _validate_common_body(body)
    if error:
        return error

    trigger = str(body.get("trigger") or "").strip()
    if not trigger:
        return fail(ErrorCode.AGENT_API_003, "trigger 필드가 필요합니다.")
    if trigger not in _SCENARIO_TRIGGERS:
        return fail(ErrorCode.AGENT_API_003, "지원하지 않는 trigger 값입니다.")
    if trigger == "natural_lang" and not str(body.get("user_input") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "user_input 필드가 필요합니다.")
    return None


def _validate_common_body(body: dict[str, Any]) -> JSONResponse | None:
    if not str(body.get("service_id") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "service_id 필드가 필요합니다.")
    if not str(body.get("qapilot_dir") or "").strip():
        return fail(ErrorCode.AGENT_API_003, "qapilot_dir 필드가 필요합니다.")
    return None


async def _json_body(request: Request) -> dict[str, Any]:
    """요청 JSON body를 안전하게 읽는다."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _valid_qapilot_dir(qapilot_dir: str) -> Path | None:
    path = Path(qapilot_dir).expanduser()
    if not path.exists() or not path.is_dir():
        return None
    return path


def _invalid_qapilot_dir() -> JSONResponse:
    return fail(ErrorCode.AGENT_API_003, "qapilot_dir 경로를 찾을 수 없습니다.")


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


def _scenario_trigger(body: dict[str, Any]) -> ScenarioTrigger:
    return cast(ScenarioTrigger, str(body["trigger"]))


def _optional_filter(body: dict[str, Any]) -> RunFilter | None:
    value = _optional_str(body, "filter")
    if value in {"all", "failed", "affected"}:
        return cast(RunFilter, value)
    return None


def _chat_reply(message: str, quick_action: str | None) -> str:
    if quick_action == "generate":
        return "시나리오 생성을 시작합니다. /api/agent/scenario-generation을 호출하세요."
    if quick_action == "detect":
        return "코드 변경 감지를 시작합니다. /api/agent/code-change-detection을 호출하세요."
    if quick_action:
        return "요청을 처리할 수 없습니다."
    return f"'{message}'를 받았습니다. 현재 채팅 기반 시나리오 수정은 준비 중입니다."
