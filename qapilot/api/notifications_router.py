"""알림 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.notification_store import load_notifications, mark_as_read
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api", tags=["notifications"])


@router.get("/notifications")
async def list_notifications(
    service_id: str,
    is_read: bool | None = None,
    user: dict = Depends(get_current_user),
) -> Any:
    """알림 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    notifications = load_notifications(service)
    unread_count = len([item for item in notifications if not item.get("is_read")])
    if is_read is not None:
        notifications = [item for item in notifications if item.get("is_read") is is_read]
    return ok(
        {
            "notifications": notifications,
            "count": len(notifications),
            "unread_count": unread_count,
        }
    )


@router.patch("/notifications/{notification_id}")
async def patch_notification(
    notification_id: str,
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """알림을 읽음 처리한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    if body.get("is_read") is not True:
        return fail("REQUEST_400", "is_read=true만 지원합니다.")
    notification = mark_as_read(service, notification_id)
    return ok({"notification": notification}) if notification else _notification_not_found()


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


def _notification_not_found() -> JSONResponse:
    return fail(ErrorCode.NOTIFICATION_001, "알림을 찾을 수 없습니다.")
