"""Agent 실행 전용 FastAPI 라우터.

Author: C
Created: 2026-05-15
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from qapilot.api.internal_deps import verify_internal_token
from qapilot.api.response import fail, ok
from qapilot.messaging.redis_pubsub import subscribe_run_events
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.errors import ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import RunOptions
from qapilot.shared.trace_store import (
    annotate_trace,
    create_trace,
    load_trace,
    update_trace,
    update_trace_aborted,
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

# 실행 중인 파이프라인 task 레지스트리 — trace_id → asyncio.Task.
# 정지 요청 시 task.cancel() 호출하여 즉시 중단할 수 있도록 추적한다.
# CELERY_ENABLED=true 면 사용 안 함 — Celery broker 의 broadcast revoke 로 대체.
_running_tasks: dict[str, asyncio.Task] = {}

# Celery 워커 모드 — 멀티 인스턴스 SaaS 환경에서 유일하게 동작.
# 기본 false (로컬 단일 프로세스 흐름 유지). 운영에선 환경변수로 true.
_CELERY_ENABLED = os.environ.get("CELERY_ENABLED", "false").lower() == "true"


def _submit_pipeline(
    qapilot_dir: Path,
    trace_id: str,
    options: RunOptions,
    staging_url: str | None,
) -> None:
    """파이프라인 실행 제출 — Celery 모드면 send_task, 아니면 asyncio.create_task.

    Celery 모드: runs.task_id 에 Celery task id 기록 → stop 시 lookup 키로 사용.
    """
    if _CELERY_ENABLED:
        from qapilot.db.run_writer import set_task_id
        from qapilot.worker.tasks import run_pipeline_task

        result = run_pipeline_task.delay(str(qapilot_dir), trace_id, dict(options), staging_url)
        set_task_id(trace_id, result.id)
        logger.info("agent_pipeline_submitted_celery", trace_id=trace_id, task_id=result.id)
    else:
        task = asyncio.create_task(_run_pipeline_task(qapilot_dir, trace_id, options, staging_url))
        _running_tasks[trace_id] = task


def _revoke_pipeline(trace_id: str) -> bool:
    """진행 중 파이프라인 중단. 반환: 실제로 중단 시그널 보냈는지.

    Celery 모드: runs.task_id 로 revoke 브로드캐스트 (어느 워커가 갖고 있든 도달).
    asyncio 모드: 같은 프로세스의 _running_tasks 에서 cancel.
    """
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
    _inject_git_options(body, options)
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
    _inject_git_options(body, options)
    return _start_pipeline(request, body, options)


@router.post("/code-generation")
async def code_generation(request: Request) -> Any:
    """코드 생성 파이프라인(Layer 1B)을 백그라운드로 실행한다."""
    body = await _json_body(request)
    error = _validate_common_body(body)
    if error:
        return error

    options: RunOptions = {
        "command": "generate_code",
        "trigger": None,
        "user_input": None,
        "scenario_ids": _optional_list(body, "scenario_ids"),
        "filter": None,
        "tags": None,
    }
    _inject_git_options(body, options)
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
        "resume_from_trace": _optional_str(body, "resume_from_trace"),
    }
    _inject_git_options(body, options)
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


@router.get("/runs/{trace_id}/progress")
async def get_run_progress(
    trace_id: str,
    qapilot_dir: str = Query(...),
) -> Any:
    """test-run 진행 상황 — `.qapilot/results/{trace_id}/` 디스크 스캔 결과.

    Layer 2 가 각 TC 마다 ui_result.json / api_result.json / db_result.json 을
    실시간으로 디스크에 쓰므로, 폴링 호출자는 이 endpoint 로 부분 결과를 받을 수 있다.
    """
    qapilot_path = _valid_qapilot_dir(qapilot_dir)
    if not qapilot_path:
        return _invalid_qapilot_dir()

    results_root = qapilot_path / "results" / trace_id
    items: list[dict[str, Any]] = []
    if results_root.exists():
        for tc_dir in sorted(results_root.glob("*/*")):
            if not tc_dir.is_dir():
                continue
            ts_id = tc_dir.parent.name
            tc_id = tc_dir.name
            item: dict[str, Any] = {"ts_id": ts_id, "tc_id": tc_id}
            for kind in ("ui", "api", "db"):
                f = tc_dir / f"{kind}_result.json"
                if f.exists():
                    try:
                        item[kind] = json.loads(f.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        item[kind] = None
            items.append(item)
    return ok({"trace_id": trace_id, "items": items, "count": len(items)})


@router.get("/runs/{trace_id}/stream")
async def stream_run_events(trace_id: str, request: Request) -> StreamingResponse:
    """SSE — Redis pub/sub 채널 `run:<trace_id>` 의 이벤트를 클라이언트로 push.

    이벤트 종류:
      - status     : run 시작/완료/중단
      - annotate   : selected_total_tc_count, scenario_ids 등 부분 갱신
      - tc_result  : 한 TC 의 ui/api/db 결과 1건 (이전 1초 폴링 대체)
      - artifact   : 스크린샷 등 binary 1건 (s3_key 포함)

    클라이언트가 끊으면 (request.is_disconnected()) generator 종료.
    REDIS_URL 미설정 시 즉시 close — UI 는 기존 1초 폴링 fallback 사용.
    """

    async def event_gen():
        yield ": connected\n\n"  # SSE 초기 코멘트 — 일부 프록시가 첫 byte 받아야 stream 인식
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
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # nginx buffering 끄기 (있을 경우)
        },
    )


@router.get("/runs/{trace_id}/screenshot/latest")
async def get_latest_screenshot(
    trace_id: str,
    qapilot_dir: str = Query(...),
) -> Any:
    """가장 최근에 캡쳐된 PNG 스크린샷을 반환. 없으면 204."""
    qapilot_path = _valid_qapilot_dir(qapilot_dir)
    if not qapilot_path:
        return _invalid_qapilot_dir()

    results_root = qapilot_path / "results" / trace_id
    if not results_root.exists():
        return Response(status_code=204)

    pngs = list(results_root.rglob("*.png"))
    if not pngs:
        return Response(status_code=204)

    latest = max(pngs, key=lambda p: p.stat().st_mtime)
    return FileResponse(latest, media_type="image/png")


@router.post("/runs/{trace_id}/resume")
async def resume_run(
    trace_id: str,
    qapilot_dir: str = Query(...),
) -> Any:
    """중단된 trace 를 같은 trace_id 로 재개한다.

    fresh start 와 달리 새 trace_id 를 만들지 않고 기존 trace 를 이어서 진행한다.
    - 디스크의 results/{trace_id}/ 에 이미 있는 TC 는 자동 skip (resume_from_trace=self)
    - 옵션 (scenario_ids, filter, tags, staging_url) 은 trace.json 에서 읽어 재사용
    - 같은 trace_id 로 task 등록 → 새 stop 요청도 그대로 동작
    """
    qapilot_path = _valid_qapilot_dir(qapilot_dir)
    if not qapilot_path:
        return _invalid_qapilot_dir()

    trace = load_trace(qapilot_path, trace_id)
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
        "resume_from_trace": trace_id,  # 자기 자신 — 디스크 스캔으로 완료 TC skip
    }
    staging_url = str(trace.get("staging_url") or "").strip() or None

    # 다시 running 으로 전환 — 이전 error 정보도 정리.
    annotate_trace(qapilot_path, trace_id, status="running", error=None, completed_at=None)

    _submit_pipeline(qapilot_path, trace_id, options, staging_url)
    logger.info("agent_pipeline_resumed", trace_id=trace_id, mode="celery" if _CELERY_ENABLED else "asyncio")
    return ok({"trace_id": trace_id, "status": "running"})


def _optional_list_value(value: Any) -> list[str] | None:
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return None


def _optional_filter_value(value: Any) -> str | None:
    if isinstance(value, str) and value in {"all", "failed", "affected"}:
        return value
    return None


@router.post("/runs/{trace_id}/stop")
async def stop_run(
    trace_id: str,
    qapilot_dir: str = Query(...),
) -> Any:
    """진행 중인 파이프라인을 즉시 중단한다. trace.status → "aborted".

    이미 끝났거나 등록되지 않은 trace 면 idempotent 하게 ok 반환 (UI 가 race 안 만들도록).
    """
    qapilot_path = _valid_qapilot_dir(qapilot_dir)
    if not qapilot_path:
        return _invalid_qapilot_dir()

    if _revoke_pipeline(trace_id):
        logger.info("agent_pipeline_stop_requested", trace_id=trace_id, mode="celery" if _CELERY_ENABLED else "asyncio")
        return ok({"trace_id": trace_id, "status": "aborting"})

    # 이미 끝났거나 등록되지 않은 trace — 디스크/DB status 가 ground truth.
    logger.warning(
        "agent_pipeline_stop_not_tracked",
        trace_id=trace_id,
        mode="celery" if _CELERY_ENABLED else "asyncio",
    )
    return ok({"trace_id": trace_id, "status": "not_running"})


async def _run_pipeline_task(
    qapilot_dir: Path,
    trace_id: str,
    options: RunOptions,
    staging_url: str | None = None,
) -> None:
    """백그라운드에서 파이프라인을 실행하고 trace를 갱신한다.

    CancelledError (정지 요청) 와 일반 Exception (파이프라인 크래시) 모두
    trace.status="aborted" 로 처리한다. "completed" 와 분리해 "이어서 실행" 가능.
    """
    try:
        state = await run_pipeline(options, qapilot_dir, trace_id=trace_id, staging_url=staging_url)
        update_trace(qapilot_dir, trace_id, dict(state))
    except asyncio.CancelledError:
        # 중단 시점까지 디스크에 쌓인 부분 결과 (results/{trace_id}/*) 를 집계해서 trace.json 에 보존.
        # ResultResponse 가 trace.json 의 tc_results 로 P/F 도출하므로, aborted 라도 진척 사항이
        # UI 에 반영됨.
        _persist_partial_tc_results_from_disk(qapilot_dir, trace_id)
        update_trace_aborted(qapilot_dir, trace_id, "사용자 요청으로 중단됨")
        logger.info("agent_pipeline_task_cancelled", trace_id=trace_id)
        raise  # cancel 전파 (asyncio 가 task 상태를 cancelled 로 마크)
    except Exception as e:
        _persist_partial_tc_results_from_disk(qapilot_dir, trace_id)
        error = f"{type(e).__name__}: {e}"
        update_trace_aborted(qapilot_dir, trace_id, error)
        logger.error("agent_pipeline_task_failed", trace_id=trace_id, error=error)
    finally:
        _running_tasks.pop(trace_id, None)


def _persist_partial_tc_results_from_disk(qapilot_dir: Path, trace_id: str) -> None:
    """중단/실패 시점까지 results 디스크에 쌓인 ui/api/db 결과로 tc_results 집계해 trace 에 보존."""
    try:
        from qapilot.orchestrator.pipeline import (
            _aggregate_tc_results,
            _load_all_tc_results_from_disk,
        )
        results_root = qapilot_dir / "results" / trace_id
        if not results_root.exists():
            return
        disk_ui, disk_api, disk_db = _load_all_tc_results_from_disk(results_root)
        if not disk_ui:
            return
        tc_results = _aggregate_tc_results(disk_ui, disk_api, disk_db)
        if tc_results:
            annotate_trace(qapilot_dir, trace_id, tc_results=tc_results)
    except Exception as e:
        logger.warning("partial_tc_results_persist_failed", trace_id=trace_id, error=str(e))


def _start_pipeline(request: Request, body: dict[str, Any], options: RunOptions) -> JSONResponse:
    qapilot_dir = _valid_qapilot_dir(str(body.get("qapilot_dir") or ""))
    if not qapilot_dir:
        return _invalid_qapilot_dir()

    trace_id = request.state.trace_id
    service_id = str(body.get("service_id") or "")
    staging_url = str(body.get("staging_url") or "").strip() or None
    create_trace(
        qapilot_dir,
        trace_id,
        options["command"],
        options["trigger"],
        service_id=service_id,
    )
    # 옵션을 trace.json 에 보존 — resume endpoint 가 같은 trace_id 로 재개할 때 읽어 쓴다.
    annotate_trace(
        qapilot_dir,
        trace_id,
        scenario_ids=options.get("scenario_ids"),
        filter=options.get("filter"),
        tags=options.get("tags"),
        staging_url=staging_url,
    )
    _submit_pipeline(qapilot_dir, trace_id, options, staging_url)
    logger.info(
        "agent_pipeline_started",
        service_id=service_id,
        trace_id=trace_id,
        mode="celery" if _CELERY_ENABLED else "asyncio",
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


def _inject_git_options(body: dict[str, Any], options: RunOptions) -> RunOptions:
    """Body 의 GitHub/GitLab 스캔 필드를 RunOptions 에 주입한다.

    pipeline._codebase_scan 은 ``run_options.repo_url`` 또는 ``run_options.repos`` 가 있으면
    GitCodebaseScannerTool(REST API 스캔) 로 분기한다. 둘 다 없으면 로컬 CodebaseScannerTool
    로 fallback. 본 helper 는 body 에 해당 필드가 들어왔을 때만 options 에 추가하여
    분기를 활성화한다.

    필드:
        - ``repo_url`` (str): 단일 레포 URL
        - ``token`` (str): read-only 액세스 토큰 (PAT)
        - ``branch`` (str): 스캔 대상 브랜치 (기본 main)
        - ``local_path`` (str): 로컬 디렉토리 경로 (file:// 어댑터용, 선택)
        - ``repos`` (list[dict]): 멀티 레포 형태
    """
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
