"""service scope 멤버 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.member_store import (
    export_members_csv,
    invite_member,
    load_members,
    remove_member,
    update_member,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["members"])


@router.get("/members")
async def list_members(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """멤버 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    members = load_members(service)
    return ok({"members": members, "count": len(members)})


@router.post("/members/invitations")
async def invite_member_endpoint(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """멤버를 초대한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    if not body.get("email") or not body.get("name"):
        return fail("REQUEST_400", "email, name 필드가 필요합니다.")
    member = invite_member(
        service,
        str(body["email"]),
        str(body["name"]),
        str(body.get("role") or "member"),
        body.get("team"),
    )
    return JSONResponse(status_code=201, content=ok({"member": member}))


@router.get("/members/export")
async def export_members(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """멤버 목록을 CSV로 다운로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    return Response(
        content=export_members_csv(service),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="members.csv"'},
    )


@router.patch("/members/{member_id}")
async def patch_member(
    service_id: str,
    member_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """멤버 role/team을 수정한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    member = update_member(service, member_id, body.get("role"), body.get("team"))
    return ok({"member": member}) if member else _member_not_found()


@router.delete("/members/{member_id}", status_code=204, response_class=Response)
async def delete_member(
    service_id: str,
    member_id: str,
    user: dict = Depends(get_current_user),
):
    """멤버를 삭제한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not remove_member(service, member_id):
        return _member_not_found()
    return Response(status_code=204)


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


def _member_not_found() -> JSONResponse:
    return fail(ErrorCode.MEMBER_001, "멤버를 찾을 수 없습니다.")
