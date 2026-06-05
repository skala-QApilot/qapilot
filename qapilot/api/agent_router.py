"""Agent 실행 전용 FastAPI 라우터.

Author: C
Created: 2026-05-15
"""

import asyncio
import json
import os
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from qapilot.api.internal_deps import verify_internal_token
from qapilot.api.response import fail, ok
from qapilot.db.tc_result_reader import load_latest_screenshot_s3_key, load_tc_results_by_run
from qapilot.messaging.redis_pubsub import subscribe_run_events
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.errors import ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import RunOptions
from qapilot.shared.session_store import new_session_id
from qapilot.shared.trace_store import (
    annotate_trace,
    create_trace,
    load_trace,
    update_trace,
    update_trace_aborted,
)
from qapilot.storage import s3_client

router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    dependencies=[Depends(verify_internal_token)],
)
logger = get_logger("api.agent")

_SCENARIO_TRIGGERS = {"init", "natural_lang", "doc_update"}
ScenarioTrigger = Literal["init", "natural_lang", "doc_update"]
RunFilter = Literal["all", "failed", "affected"]

_running_tasks: dict[str, asyncio.Task] = {}

_CELERY_ENABLED = os.environ.get("CELERY_ENABLED", "false").lower() == "true"


class RepoPayload(BaseModel):
    repo_url: str
    token: str | None = None
    branch: str | None = None
    role: str | None = None


class ScenarioGenerationRequestBody(BaseModel):
    service_id: str = Field(..., description="서비스 ID")
    trigger: ScenarioTrigger = Field(..., description="시나리오 생성 트리거")
    user_input: str | None = Field(None, description="natural_lang 트리거용 사용자 입력")
    scenario_ids: list[str] | None = Field(None, description="대상 시나리오 ID 목록")
    filter: RunFilter | None = Field(None, description="실행 필터")
    tags: list[str] | None = Field(None, description="태그 필터")
    session_id: str | None = Field(None, description="natural_lang 세션 ID")
    staging_url: str | None = Field(None, description="SUT base URL")
    test_account: dict[str, Any] | None = Field(None, description="테스트 계정 정보")
    domain_files: list[dict[str, Any]] | None = Field(None, description="도메인 문서 메타 목록")
    repo_url: str | None = Field(None, description="단일 Git 저장소 URL")
    token: str | None = Field(None, description="단일 Git 저장소 접근 토큰")
    branch: str | None = Field(None, description="단일 Git 저장소 브랜치")
    local_path: str | None = Field(None, description="로컬 스캔 경로")
    repos: list[RepoPayload] | None = Field(None, description="멀티 Git 저장소 설정")
    qapilot_dir: str | None = Field(None, description="레거시 호환용 qapilot 작업 디렉터리")


class CodeGenerationRequestBody(BaseModel):
    service_id: str = Field(..., description="서비스 ID")
    scenario_ids: list[str] | None = Field(None, description="대상 시나리오 ID 목록")
    staging_url: str | None = Field(None, description="SUT base URL")
    test_account: dict[str, Any] | None = Field(None, description="테스트 계정 정보")
    domain_files: list[dict[str, Any]] | None = Field(None, description="도메인 문서 메타 목록")
    repo_url: str | None = Field(None, description="단일 Git 저장소 URL")
    token: str | None = Field(None, description="단일 Git 저장소 접근 토큰")
    branch: str | None = Field(None, description="단일 Git 저장소 브랜치")
    local_path: str | None = Field(None, description="로컬 스캔 경로")
    repos: list[RepoPayload] | None = Field(None, description="멀티 Git 저장소 설정")
    qapilot_dir: str | None = Field(None, description="레거시 호환용 qapilot 작업 디렉터리")


class TestRunRequestBody(BaseModel):
    service_id: str = Field(..., description="서비스 ID")
    scenario_ids: list[str] | None = Field(None, description="대상 시나리오 ID 목록")
    filter: RunFilter | None = Field("all", description="실행 필터")
    tags: list[str] | None = Field(None, description="태그 필터")
    resume_from_trace: str | None = Field(None, description="이어 실행할 이전 trace ID")
    staging_url: str | None = Field(None, description="SUT base URL")
    test_account: dict[str, Any] | None = Field(None, description="테스트 계정 정보")
    domain_files: list[dict[str, Any]] | None = Field(None, description="도메인 문서 메타 목록")
    repo_url: str | None = Field(None, description="단일 Git 저장소 URL")
    token: str | None = Field(None, description="단일 Git 저장소 접근 토큰")
    branch: str | None = Field(None, description="단일 Git 저장소 브랜치")
    local_path: str | None = Field(None, description="로컬 스캔 경로")
    repos: list[RepoPayload] | None = Field(None, description="멀티 Git 저장소 설정")
    qapilot_dir: str | None = Field(None, description="레거시 호환용 qapilot 작업 디렉터리")


def _submit_pipeline(
    service_id: str,
    trace_id: str,
    options: RunOptions,
    staging_url: str | None,
    test_account: dict | None = None,
    domain_files: list[dict] | None = None,
) -> None:
    if _CELERY_ENABLED:
        from qapilot.db.run_writer import set_task_id
        from qapilot.worker.tasks import run_pipeline_task

        result = run_pipeline_task.delay(
            service_id, trace_id, dict(options), staging_url, test_account, domain_files
        )
        set_task_id(trace_id, result.id)
        logger.info("agent_pipeline_submitted_celery", trace_id=trace_id, task_id=result.id)
    else:
        task = asyncio.create_task(
            _run_pipeline_task(service_id, trace_id, options, staging_url, test_account, domain_files)
        )
        _running_tasks[trace_id] = task


def _revoke_pipeline(trace_id: str) -> bool:
    if _CELERY_ENABLED:
        from qapilot.db.run_writer import get_task_id
        from qapilot.worker.tasks import revoke_pipeline as celery_revoke

        task_id = get_task_id(trace_id)
        if not task_id:
            return False
        return celery_revoke(task_id)

    task = _running_tasks.get(trace_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


@router.post("/scenario-generation")
async def scenario_generation(request: Request, body: ScenarioGenerationRequestBody) -> Any:
    body_dict = body.model_dump(exclude_none=True)
    error = _validate_scenario_generation(body_dict)
    if error:
        return error

    trigger = _scenario_trigger(body_dict)
    session_id = _optional_str(body_dict, "session_id")
    if trigger == "natural_lang" and not session_id:
        session_id = new_session_id()

    options: RunOptions = {
        "command": "generate_scenarios",
        "trigger": trigger,
        "user_input": _optional_str(body_dict, "user_input"),
        "scenario_ids": _optional_list(body_dict, "scenario_ids"),
        "filter": _optional_filter(body_dict),
        "tags": _optional_list(body_dict, "tags"),
        "session_id": session_id,
    }
    _inject_git_options(body_dict, options)
    return _start_pipeline(request, body_dict, options)


@router.post("/code-change-detection")
async def code_change_detection(request: Request) -> Any:
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
    _inject_git_options(body, options)
    return _start_pipeline(request, body, options)


@router.post("/code-generation")
async def code_generation(request: Request, body: CodeGenerationRequestBody) -> Any:
    body_dict = body.model_dump(exclude_none=True)
    error = _validate_common_body(body_dict)
    if error:
        return error

    options: RunOptions = {
        "command": "generate_code",
        "trigger": None,
        "user_input": None,
        "scenario_ids": _optional_list(body_dict, "scenario_ids"),
        "filter": None,
        "tags": None,
    }
    _inject_git_options(body_dict, options)
    return _start_pipeline(request, body_dict, options)


@router.post("/test-run")
async def test_run(request: Request, body: TestRunRequestBody) -> Any:
    body_dict = body.model_dump(exclude_none=True)
    error = _validate_common_body(body_dict)
    if error:
        return error

    options: RunOptions = {
        "command": "test",
        "trigger": None,
        "user_input": None,
        "scenario_ids": _optional_list(body_dict, "scenario_ids"),
        "filter": _optional_filter(body_dict) or "all",
        "tags": _optional_list(body_dict, "tags"),
        "resume_from_trace": _optional_str(body_dict, "resume_from_trace"),
    }
    _inject_git_options(body_dict, options)
    return _start_pipeline(request, body_dict, options)


@router.post("/scenario-chat")
async def scenario_chat(request: Request) -> Any:
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
async def get_trace(trace_id: str) -> Any:
    """trace_id 기준 Agent 실행 상태를 DB 에서 조회한다."""
    trace = load_trace(trace_id)
    if not trace:
        return fail(ErrorCode.AGENT_API_002, "trace를 찾을 수 없습니다.")
    logger.info("agent_trace_fetched", trace_id=trace_id)
    return ok({"trace": trace})


@router.get("/runs/{trace_id}/progress")
async def get_run_progress(trace_id: str) -> Any:
    """test-run 진행 상황 — DB tc_results 조회 결과."""
    items = load_tc_results_by_run(trace_id)
    return ok({"trace_id": trace_id, "items": items, "count": len(items)})


@router.get("/runs/{trace_id}/stream")
async def stream_run_events(trace_id: str, request: Request) -> StreamingResponse:
    """SSE — Redis pub/sub 채널 `run:<trace_id>` 의 이벤트를 클라이언트로 push."""

    async def event_gen():
        yield ": connected\n\n"
        try:
            async for event in subscribe_run_events(trace_id):
                if await request.is_disconnected():
                    break
                payload = json.dumps(event, ensure_ascii=False, default=str)
                event_type = event.get("type") if isinstance(event, dict) else None
                if event_type:
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                else:
                    yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{trace_id}/screenshot/latest")
async def get_latest_screenshot(trace_id: str) -> Any:
    """가장 최근에 캡쳐된 PNG 스크린샷을 S3 에서 반환. 없으면 204."""
    s3_key = load_latest_screenshot_s3_key(trace_id)
    if not s3_key:
        return Response(status_code=204)
    data = s3_client.get_object(s3_key)
    if not data:
        return Response(status_code=204)
    return Response(content=data, media_type="image/png")


@router.post("/runs/{trace_id}/resume")
async def resume_run(trace_id: str) -> Any:
    """중단된 trace 를 같은 trace_id 로 재개한다."""
    trace = load_trace(trace_id)
    if not trace:
        return fail(ErrorCode.AGENT_API_002, "trace를 찾을 수 없습니다.")

    status = str(trace.get("status") or "").lower()
    if status != "aborted":
        return fail(
            ErrorCode.AGENT_API_003,
            f"resume 는 aborted 상태에서만 가능합니다. 현재 status={status}",
        )

    if trace_id in _running_tasks and not _running_tasks[trace_id].done():
        return fail(ErrorCode.AGENT_API_003, "이미 실행 중인 trace 입니다.")

    options: RunOptions = {
        "command": "test",
        "trigger": None,
        "user_input": None,
        "scenario_ids": _optional_list_value(trace.get("scenario_ids")),
        "filter": _optional_filter_value(trace.get("filter")) or "all",
        "tags": _optional_list_value(trace.get("tags")),
        "resume_from_trace": trace_id,
    }
    service_id = str(trace.get("service_id") or "")
    staging_url = str(trace.get("staging_url") or "").strip() or None
    test_account_saved = trace.get("test_account")
    test_account = test_account_saved if isinstance(test_account_saved, dict) else None
    domain_files_saved = trace.get("domain_files")
    domain_files = domain_files_saved if isinstance(domain_files_saved, list) else None

    annotate_trace(trace_id, status="running", error=None, completed_at=None)
    _submit_pipeline(service_id, trace_id, options, staging_url, test_account, domain_files)
    logger.info("agent_pipeline_resumed", trace_id=trace_id, mode="celery" if _CELERY_ENABLED else "asyncio")
    return ok({"trace_id": trace_id, "status": "running"})


@router.post("/runs/{trace_id}/stop")
async def stop_run(trace_id: str) -> Any:
    """진행 중인 파이프라인을 즉시 중단한다."""
    if _revoke_pipeline(trace_id):
        logger.info("agent_pipeline_stop_requested", trace_id=trace_id, mode="celery" if _CELERY_ENABLED else "asyncio")
        return ok({"trace_id": trace_id, "status": "aborting"})

    logger.warning("agent_pipeline_stop_not_tracked", trace_id=trace_id, mode="celery" if _CELERY_ENABLED else "asyncio")
    return ok({"trace_id": trace_id, "status": "not_running"})


async def _run_pipeline_task(
    service_id: str,
    trace_id: str,
    options: RunOptions,
    staging_url: str | None = None,
    test_account: dict | None = None,
    domain_files: list[dict] | None = None,
) -> None:
    try:
        state = await run_pipeline(
            options,
            service_id=service_id,
            trace_id=trace_id,
            staging_url=staging_url,
            test_account=test_account,
            domain_files=domain_files,
        )
        update_trace(trace_id, dict(state))
    except asyncio.CancelledError:
        update_trace_aborted(trace_id, "사용자 요청으로 중단됨")
        logger.info("agent_pipeline_task_cancelled", trace_id=trace_id)
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        update_trace_aborted(trace_id, error)
        logger.error("agent_pipeline_task_failed", trace_id=trace_id, error=error)
    finally:
        _running_tasks.pop(trace_id, None)


def _start_pipeline(request: Request, body: dict[str, Any], options: RunOptions) -> JSONResponse:
    service_id = str(body.get("service_id") or "")
    if not service_id:
        return fail(ErrorCode.AGENT_API_003, "service_id 필드가 필요합니다.")

    trace_id = request.state.trace_id
    staging_url = str(body.get("staging_url") or "").strip() or None
    test_account_body = body.get("test_account")
    test_account = test_account_body if isinstance(test_account_body, dict) else None
    domain_files_body = body.get("domain_files")
    domain_files = domain_files_body if isinstance(domain_files_body, list) else None

    create_trace(trace_id, options["command"], options["trigger"], service_id=service_id)
    annotate_trace(
        trace_id,
        scenario_ids=options.get("scenario_ids"),
        filter=options.get("filter"),
        tags=options.get("tags"),
        staging_url=staging_url,
        test_account=test_account,
        domain_files=domain_files,
    )
    _submit_pipeline(service_id, trace_id, options, staging_url, test_account, domain_files)
    logger.info(
        "agent_pipeline_started",
        service_id=service_id,
        trace_id=trace_id,
        mode="celery" if _CELERY_ENABLED else "asyncio",
        command=options["command"],
    )
    content: dict[str, Any] = {"success": True, "data": {"trace_id": trace_id, "status": "running"}}
    content["trace_id"] = trace_id
    if options.get("session_id"):
        content["session_id"] = options["session_id"]
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
    return None


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _optional_list_value(value: Any) -> list[str] | None:
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return None


def _optional_filter_value(value: Any) -> str | None:
    if isinstance(value, str) and value in {"all", "failed", "affected"}:
        return value
    return None


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


def _inject_git_options(body: dict[str, Any], options: RunOptions) -> RunOptions:
    repo_url = _optional_str(body, "repo_url")
    if repo_url:
        options["repo_url"] = repo_url
    token = _optional_str(body, "token")
    if token:
        options["token"] = token
    branch = _optional_str(body, "branch")
    if branch:
        options["branch"] = branch
    local_path = _optional_str(body, "local_path")
    if local_path:
        options["local_path"] = local_path
    repos = body.get("repos")
    if isinstance(repos, list) and repos:
        options["repos"] = repos
    return options


def _chat_reply(message: str, quick_action: str | None) -> str:
    if quick_action == "generate":
        return "시나리오 생성을 시작합니다. /api/agent/scenario-generation을 호출하세요."
    if quick_action == "detect":
        return "코드 변경 감지를 시작합니다. /api/agent/code-change-detection을 호출하세요."
    if quick_action:
        return "요청을 처리할 수 없습니다."
    return f"'{message}'를 받았습니다. 현재 채팅 기반 시나리오 수정은 준비 중입니다."
