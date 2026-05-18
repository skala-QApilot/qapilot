"""service scope 시나리오 그룹 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.scenario_group_store import (
    create_group,
    delete_group,
    load_all_groups,
    set_schedule,
    update_group,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["scenario_groups"])


@router.get("/scenario-groups")
async def list_groups(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """시나리오 그룹 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    groups = load_all_groups(service)
    return ok({"groups": groups, "count": len(groups)})


@router.post("/scenario-groups")
async def create_group_endpoint(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 그룹을 생성한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return fail("REQUEST_400", "name 필드가 필요합니다.")
    group = create_group(
        service,
        name,
        _optional_list(body, "scenario_ids") or [],
        _optional_list(body, "tc_ids") or [],
    )
    return JSONResponse(status_code=201, content=ok({"group": group}))


@router.patch("/scenario-groups/{group_id}")
async def patch_group(
    service_id: str,
    group_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 그룹 이름을 수정한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    group = update_group(service, group_id, body.get("name"))
    return ok({"group": group}) if group else _group_not_found()


@router.delete("/scenario-groups/{group_id}", status_code=204, response_class=Response)
async def remove_group(
    service_id: str,
    group_id: str,
    user: dict = Depends(get_current_user),
):
    """시나리오 그룹을 삭제한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not delete_group(service, group_id):
        return _group_not_found()
    return Response(status_code=204)


@router.post("/scenario-groups/{group_id}/schedule")
async def create_schedule(
    service_id: str,
    group_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오 그룹 스케줄을 설정한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    cron = str(body.get("cron") or "").strip()
    if not cron:
        return fail("REQUEST_400", "cron 필드가 필요합니다.")
    group = set_schedule(
        service,
        group_id,
        cron,
        str(body.get("timezone") or "Asia/Seoul"),
        bool(body.get("enabled", True)),
    )
    return ok({"group": group}) if group else _group_not_found()


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _optional_list(body: dict[str, Any], key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    return [str(item) for item in value] if isinstance(value, list) else None


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _group_not_found() -> JSONResponse:
    return fail(ErrorCode.GROUP_001, "그룹을 찾을 수 없습니다.")
