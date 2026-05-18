"""service scope RTM API 라우터.

Author: C
Created: 2026-05-15
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.rtm_store import (
    create_rtm_version,
    get_rtm_requirement_detail,
    get_rtm_requirements,
    get_rtm_version_by_id,
    load_all_rtm_versions,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["rtm"])


@router.get("/rtm-versions")
async def list_rtm_versions(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """RTM 버전 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    versions = load_all_rtm_versions(service)
    return ok({"versions": versions, "count": len(versions)})


@router.post("/rtm-versions")
async def create_rtm(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """RTM 버전을 생성한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    label = str(body.get("label") or "").strip()
    if not label:
        return fail("REQUEST_400", "label 필드가 필요합니다.")
    requirements = body.get("requirements") if isinstance(body.get("requirements"), list) else []
    version = create_rtm_version(service, label, body.get("trace_id"), requirements)
    return JSONResponse(status_code=201, content=ok({"version": version}))


@router.get("/rtm-versions/{rtm_version_id}")
async def get_rtm(
    service_id: str,
    rtm_version_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """RTM 버전 상세를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    version = get_rtm_version_by_id(service, rtm_version_id)
    return ok({"version": version}) if version else _rtm_not_found()


@router.get("/rtm-versions/{rtm_version_id}/requirements")
async def list_requirements(
    service_id: str,
    rtm_version_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """RTM 요구사항 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    requirements = get_rtm_requirements(service, rtm_version_id)
    if requirements is None:
        return _rtm_not_found()
    return ok({"requirements": requirements, "count": len(requirements)})


@router.get("/rtm-versions/{rtm_version_id}/requirements/{fr_id}")
async def get_requirement(
    service_id: str,
    rtm_version_id: str,
    fr_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """RTM 요구사항 상세를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not get_rtm_version_by_id(service, rtm_version_id):
        return _rtm_not_found()
    requirement = get_rtm_requirement_detail(service, rtm_version_id, fr_id)
    return ok({"requirement": requirement}) if requirement else _requirement_not_found()


@router.get("/rtm-versions/{rtm_version_id}/export")
async def export_rtm(
    service_id: str,
    rtm_version_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """RTM 버전을 JSON으로 다운로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    version = get_rtm_version_by_id(service, rtm_version_id)
    if not version:
        return _rtm_not_found()
    return Response(
        content=json.dumps(version, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="rtm_{rtm_version_id}.json"'},
    )


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


def _rtm_not_found() -> JSONResponse:
    return fail(ErrorCode.RTM_001, "RTM 버전을 찾을 수 없습니다.")


def _requirement_not_found() -> JSONResponse:
    return fail(ErrorCode.RTM_002, "요구사항을 찾을 수 없습니다.")
