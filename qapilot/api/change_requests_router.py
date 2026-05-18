"""service scope 시나리오 변경 요청 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.change_request_store import load_all_requests, update_request_status
from qapilot.shared.errors import ErrorCode
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["change_requests"])
_ALLOWED_STATUS = {"approved", "deferred", "rejected"}


@router.get("/scenario-change-requests")
async def list_change_requests(
    service_id: str,
    status: str | None = None,
    trigger: str | None = None,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 변경 요청 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    requests = load_all_requests(service, status=status, trigger=trigger)
    return ok({"requests": requests, "count": len(requests)})


@router.patch("/scenario-change-requests/{request_id}")
async def patch_change_request(
    service_id: str,
    request_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 변경 요청 상태를 수정한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    status = str(body.get("status") or "")
    if status not in _ALLOWED_STATUS:
        return fail(ErrorCode.CHANGE_REQUEST_002, "허용되지 않은 status 값입니다.")
    updated = update_request_status(service, request_id, status, body.get("reviewer"))
    return ok({"request": updated}) if updated else _request_not_found()


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _request_not_found() -> JSONResponse:
    return fail(ErrorCode.CHANGE_REQUEST_001, "변경 요청을 찾을 수 없습니다.")
