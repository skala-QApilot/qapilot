"""service scope 결과 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.result_store import (
    export_result_csv,
    get_result_statistics,
    load_all_results,
    load_result,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["results"])


@router.get("/results")
async def list_results(
    service_id: str,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
) -> Any:
    """결과 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    results = load_all_results(service)
    if status:
        results = [r for r in results if r.get("status") == status]
    total = len(results)
    page = results[offset : offset + limit]
    return ok({"results": page, "count": len(page), "total": total})


@router.get("/results/statistics")
async def result_statistics(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """결과 통계를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    return ok({"statistics": get_result_statistics(service)})


@router.get("/results/{trace_id}")
async def get_result(
    service_id: str,
    trace_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """결과 상세를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    result = load_result(service, trace_id)
    return ok({"result": result}) if result else _result_not_found()


@router.get("/results/{trace_id}/export")
async def export_result(
    service_id: str,
    trace_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """결과를 CSV로 다운로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not load_result(service, trace_id):
        return _result_not_found()
    return Response(
        content=export_result_csv(service, trace_id),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{trace_id}.csv"'},
    )


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _result_not_found() -> JSONResponse:
    return fail(ErrorCode.RESULT_001, "결과를 찾을 수 없습니다.")
