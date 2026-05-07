"""Agent 실행 라우터.

담당: C
Created: 2026-05-07
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/run", status_code=201)
async def run_test(request: dict) -> dict:
    """테스트 실행을 요청한다."""
    raise NotImplementedError
