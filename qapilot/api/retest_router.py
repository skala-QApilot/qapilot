"""service scope 재테스트 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.retest_store import create_retest_group, load_all_retest_groups
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["retest"])


@router.get("/retest-groups")
async def list_retest_groups(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """재테스트 그룹 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    groups = load_all_retest_groups(service)
    return ok({"retest_groups": groups, "count": len(groups)})


@router.post("/retest-groups")
async def create_retest_group_endpoint(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """재테스트 그룹을 생성한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    source_trace_id = str(body.get("source_trace_id") or "").strip()
    failed_tc_ids = body.get("failed_tc_ids")
    if not source_trace_id:
        return fail("REQUEST_400", "source_trace_id 필드가 필요합니다.")
    if not isinstance(failed_tc_ids, list) or not failed_tc_ids:
        return fail(ErrorCode.RETEST_002, "failed_tc_ids 필드가 필요합니다.")
    group = create_retest_group(service, source_trace_id, [str(tc) for tc in failed_tc_ids])
    return JSONResponse(status_code=201, content=ok({"retest_group": group}))


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
