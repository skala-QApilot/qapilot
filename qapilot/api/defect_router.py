"""결함 라우터.

담당: C
Created: 2026-05-07
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/defects", tags=["defects"])


@router.get("")
async def list_defects() -> dict:
    """결함 목록을 조회한다."""
    raise NotImplementedError


@router.get("/{defect_id}")
async def get_defect(defect_id: str) -> dict:
    """결함 상세를 조회한다."""
    raise NotImplementedError
