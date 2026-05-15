"""service scope 증적 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.evidence_store import load_evidence_index, load_tc_evidence
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["evidences"])


@router.get("/evidences/{trace_id}")
async def get_evidence(
    service_id: str,
    trace_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """trace_id 기준 증적 index를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    evidence = load_evidence_index(service, trace_id)
    return ok({"evidence": evidence}) if evidence else _evidence_not_found()


@router.get("/evidences/{trace_id}/{tc_id}")
async def get_tc_evidence(
    service_id: str,
    trace_id: str,
    tc_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """TC 단위 증적을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    evidence = load_tc_evidence(service, trace_id, tc_id)
    return ok({"evidence": evidence}) if evidence else _evidence_not_found()


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _evidence_not_found() -> JSONResponse:
    return fail(ErrorCode.EVIDENCE_001, "증적을 찾을 수 없습니다.")
