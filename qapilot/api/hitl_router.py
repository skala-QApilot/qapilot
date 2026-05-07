"""HITL 라우터.

담당: C
Created: 2026-05-07
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/hitl", tags=["hitl"])


@router.get("/pending")
async def get_pending() -> dict:
    """HITL 대기 목록을 조회한다."""
    raise NotImplementedError


@router.post("/{review_id}/approve")
async def approve(review_id: str) -> dict:
    """HITL 승인 처리한다."""
    raise NotImplementedError


@router.post("/{review_id}/reject")
async def reject(review_id: str) -> dict:
    """HITL 반려 처리한다."""
    raise NotImplementedError
