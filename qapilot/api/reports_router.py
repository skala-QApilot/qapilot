"""service scope 리포트 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.report_store import export_report, load_all_reports, load_report
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["reports"])


@router.get("/reports")
async def list_reports(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """리포트 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    reports = load_all_reports(service)
    return ok({"reports": reports, "count": len(reports)})


@router.get("/reports/{trace_id}")
async def get_report(
    service_id: str,
    trace_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """trace_id 기준 리포트를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    report = load_report(service, trace_id)
    return ok({"report": report}) if report else _report_not_found()


@router.get("/reports/{trace_id}/export")
async def export_report_endpoint(
    service_id: str,
    trace_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """리포트를 JSON 파일로 다운로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    report = load_report(service, trace_id)
    if not report:
        return _report_not_found()
    return Response(
        content=export_report(service, trace_id),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="report_{trace_id}.json"'},
    )


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _report_not_found() -> JSONResponse:
    return fail(ErrorCode.REPORT_001, "리포트를 찾을 수 없습니다.")
