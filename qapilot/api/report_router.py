"""리포트 라우터.

담당: C
Created: 2026-05-07
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("")
async def list_reports() -> dict:
    """리포트 목록을 조회한다."""
    raise NotImplementedError


@router.get("/{trace_id}")
async def get_report(trace_id: str) -> dict:
    """실행별 리포트를 조회한다."""
    raise NotImplementedError
