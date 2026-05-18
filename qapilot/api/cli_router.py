"""CLI sync API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qapilot.api.response import fail, ok
from qapilot.shared.errors import AuthError, ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.service_store import get_service_by_token
from qapilot.shared.sync_store import sync_generated_code, sync_results, sync_scenarios

router = APIRouter(prefix="/api/cli", tags=["cli"])
security = HTTPBearer()
logger = get_logger("api.cli")


async def _get_service_by_auth_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Authorization: Bearer <authToken>으로 서비스를 식별한다."""
    service = get_service_by_token(credentials.credentials)
    if not service:
        raise AuthError(ErrorCode.AUTH_003, "유효하지 않은 서비스 토큰입니다.")
    return service


@router.get("/health")
async def health() -> dict:
    """CLI 전용 서버 상태를 확인한다."""
    return {"status": "ok", "version": "0.1.0"}


@router.post("/sync/scenarios")
async def sync_scenarios_endpoint(
    request: Request,
    service: dict = Depends(_get_service_by_auth_token),
) -> Any:
    """CLI에서 전달한 시나리오를 동기화한다."""
    body = await _json_body(request)
    items_or_error = _items_or_error(body)
    if isinstance(items_or_error, JSONResponse):
        return items_or_error

    result = sync_scenarios(service, items_or_error)
    logger.info("cli_scenarios_synced", service_id=service["service_id"], **result)
    return ok(result)


@router.post("/sync/generated-code")
async def sync_generated_code_endpoint(
    request: Request,
    service: dict = Depends(_get_service_by_auth_token),
) -> Any:
    """CLI에서 전달한 생성 코드를 동기화한다."""
    body = await _json_body(request)
    items_or_error = _items_or_error(body)
    if isinstance(items_or_error, JSONResponse):
        return items_or_error

    result = sync_generated_code(service, items_or_error)
    logger.info("cli_generated_code_synced", service_id=service["service_id"], **result)
    return ok(result)


@router.post("/sync/results")
async def sync_results_endpoint(
    request: Request,
    service: dict = Depends(_get_service_by_auth_token),
) -> Any:
    """CLI에서 전달한 테스트 결과를 동기화한다."""
    body = await _json_body(request)
    items_or_error = _items_or_error(body)
    if isinstance(items_or_error, JSONResponse):
        return items_or_error

    result = sync_results(service, items_or_error)
    logger.info("cli_results_synced", service_id=service["service_id"], **result)
    return ok(result)


async def _json_body(request: Request) -> dict[str, Any]:
    """요청 JSON body를 안전하게 읽는다."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _items_or_error(body: dict[str, Any]) -> list[dict] | JSONResponse:
    """sync 요청의 items 목록을 검증한다."""
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return fail(ErrorCode.SYNC_001, "items 필드가 필요합니다.")
    return [item for item in items if isinstance(item, dict)]
