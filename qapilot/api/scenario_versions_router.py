"""service scope 시나리오 버전 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.scenario_version_store import (
    create_current_version,
    delete_version,
    get_version_by_id,
    get_version_diff,
    load_all_versions,
    update_version,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["scenario_versions"])


@router.get("/scenario-versions")
async def list_versions(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """시나리오 버전 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    versions = load_all_versions(service)
    return ok({"versions": versions, "count": len(versions)})


@router.post("/scenario-versions")
async def create_version_endpoint(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """현재 시나리오 snapshot으로 버전을 생성한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    label = str(body.get("label") or "").strip()
    if not label:
        return fail("REQUEST_400", "label 필드가 필요합니다.")
    version = create_current_version(service, label, str(body.get("description") or ""))
    return JSONResponse(status_code=201, content=ok({"version": version}))


@router.patch("/scenario-versions/{version_id}")
async def patch_version(
    service_id: str,
    version_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 버전을 수정한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    version = update_version(service, version_id, body.get("is_favorite"), body.get("label"))
    return ok({"version": version}) if version else _version_not_found()


@router.delete("/scenario-versions/{version_id}", status_code=204, response_class=Response)
async def remove_version(
    service_id: str,
    version_id: str,
    user: dict = Depends(get_current_user),
):
    """시나리오 버전을 삭제한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not delete_version(service, version_id):
        return _version_not_found()
    return Response(status_code=204)


@router.get("/scenario-versions/{version_id}/diff")
async def version_diff(
    service_id: str,
    version_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 버전 diff를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not get_version_by_id(service, version_id):
        return _version_not_found()
    return ok({"diff": get_version_diff(service, version_id)})


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


def _version_not_found() -> JSONResponse:
    return fail(ErrorCode.VERSION_001, "버전을 찾을 수 없습니다.")
