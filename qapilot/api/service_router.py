"""서비스 도메인 API 라우터.

Author: C
Created: 2026-05-15
"""

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.errors import ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.notification_store import create_notification
from qapilot.shared.schemas import RunOptions
from qapilot.shared.service_store import (
    create_service,
    get_service_by_id,
    load_services,
    public_credentials,
    public_service,
    rotate_token,
    update_service,
)
from qapilot.shared.trace_store import create_trace, update_trace, update_trace_failed

router = APIRouter(prefix="/api/services", tags=["services"])
logger = get_logger("api.services")


@router.get("")
async def list_services(user: dict = Depends(get_current_user)) -> Any:
    """서비스 목록을 조회한다."""
    services = load_services()
    logger.info("services_listed", user_id=user["user_id"], count=len(services))
    return ok({"services": [public_service(s) for s in services], "count": len(services)})


@router.post("")
async def create_service_endpoint(
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스를 생성한다."""
    body = await _json_body(request)
    error = _validate_create_body(body)
    if error:
        return error

    service = create_service(
        name=str(body["name"]).strip(),
        description=str(body.get("description") or ""),
        target_root=str(body["target_root"]),
    )
    logger.info("service_created", user_id=user["user_id"], service_id=service["service_id"])
    return JSONResponse(status_code=201, content=ok({"service": public_service(service)}))


@router.get("/{service_id}")
async def get_service(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스 상세 정보를 조회한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()
    logger.info("service_fetched", user_id=user["user_id"], service_id=service_id)
    return ok({"service": public_service(service)})


@router.patch("/{service_id}")
async def patch_service(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스 이름과 설명을 수정한다."""
    body = await _json_body(request)
    updated = update_service(
        service_id=service_id,
        name=_optional_str(body, "name"),
        description=_optional_str(body, "description"),
    )
    if not updated:
        return _service_not_found()
    logger.info("service_updated", user_id=user["user_id"], service_id=service_id)
    return ok({"service": public_service(updated)})


@router.get("/{service_id}/credentials")
async def get_credentials(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스 접속 정보를 조회한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()
    logger.info("service_credentials_fetched", user_id=user["user_id"], service_id=service_id)
    return ok({"credentials": public_credentials(service)})


@router.post("/{service_id}/credentials/token")
async def rotate_credentials_token(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스 인증 토큰을 재발급한다."""
    service = rotate_token(service_id)
    if not service:
        return _service_not_found()
    logger.info("service_token_rotated", user_id=user["user_id"], service_id=service_id)
    return ok({"credentials": public_credentials(service)})


@router.post("/{service_id}/setup")
async def setup_service(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스 초기 설정을 저장하고 필수 디렉터리를 확인한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()

    body = await _json_body(request)
    description = _optional_str(body, "description")
    if description is not None:
        service = update_service(service_id, None, description) or service

    initialized = _ensure_qapilot_dirs(service)
    create_notification(
        service,
        "서비스 초기화 완료",
        f"{service['name']} 초기 설정이 완료되었습니다.",
        "success",
    )
    logger.info("service_setup_completed", user_id=user["user_id"], service_id=service_id)
    return ok({"service": public_service(service), "initialized_dirs": initialized})


@router.post("/{service_id}/scenario-generation")
async def service_scenario_generation(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """서비스 기준 시나리오 생성 파이프라인을 백그라운드로 실행한다."""
    service = get_service_by_id(service_id)
    if not service:
        return _service_not_found()

    body = await _json_body(request)
    error = _validate_generation_body(body)
    if error:
        return error

    trigger = str(body["trigger"])
    options: RunOptions = {
        "command": "generate_scenarios",
        "trigger": trigger,
        "user_input": _optional_str(body, "user_input"),
        "scenario_ids": None,
        "filter": None,
        "tags": None,
    }
    trace_id = request.state.trace_id
    create_trace(service, trace_id, "generate_scenarios", trigger)
    asyncio.create_task(_run_pipeline_task(service, trace_id, options))
    logger.info("service_scenario_generation_started", user_id=user["user_id"], trace_id=trace_id)
    return JSONResponse(status_code=202, content=ok({"trace_id": trace_id, "status": "running"}))


async def _run_pipeline_task(service: dict, trace_id: str, options: RunOptions) -> None:
    """백그라운드에서 파이프라인을 실행하고 trace를 갱신한다."""
    try:
        state = await run_pipeline(options, trace_id=trace_id)
        update_trace(service, trace_id, dict(state))
    except Exception as e:
        update_trace_failed(service, trace_id, f"{type(e).__name__}: {e}")


async def _json_body(request: Request) -> dict[str, Any]:
    """요청 JSON body를 안전하게 읽는다."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _validate_create_body(body: dict[str, Any]) -> JSONResponse | None:
    """서비스 생성 요청 body를 검증한다."""
    if not str(body.get("name") or "").strip():
        return fail("REQUEST_400", "name 필드가 필요합니다.")
    if not str(body.get("target_root") or "").strip():
        return fail("REQUEST_400", "target_root 필드가 필요합니다.")
    if not Path(str(body["target_root"])).expanduser().exists():
        return fail(ErrorCode.SERVICE_002, "대상 경로를 찾을 수 없습니다.")
    return None


def _optional_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    return str(value)


def _ensure_qapilot_dirs(service: dict) -> list[str]:
    names = [
        "scenarios",
        "generated-code",
        "results",
        "traces",
        "logs",
        "reports",
        "evidence",
        "domain",
        "cache",
        "codebase-index",
    ]
    qapilot_dir = Path(str(service["qapilot_dir"]))
    initialized = []
    for name in names:
        path = qapilot_dir / name
        path.mkdir(parents=True, exist_ok=True)
        initialized.append(str(path))
    return initialized


def _validate_generation_body(body: dict[str, Any]) -> JSONResponse | None:
    if not str(body.get("trigger") or "").strip():
        return fail("REQUEST_400", "trigger 필드가 필요합니다.")
    if body.get("trigger") == "natural_lang" and not str(body.get("user_input") or "").strip():
        return fail("REQUEST_400", "user_input 필드가 필요합니다.")
    return None


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")
