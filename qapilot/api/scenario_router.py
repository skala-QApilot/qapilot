"""시나리오 라우터.

담당: C
Created: 2026-05-07
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("")
async def list_scenarios() -> dict:
    """시나리오 목록을 조회한다."""
    raise NotImplementedError


@router.post("", status_code=201)
async def create_scenario(request: dict) -> dict:
    """시나리오를 생성한다."""
    raise NotImplementedError


@router.get("/{scenario_id}")
async def get_scenario(scenario_id: str) -> dict:
    """시나리오 상세를 조회한다."""
    raise NotImplementedError


@router.put("/{scenario_id}")
async def update_scenario(scenario_id: str, request: dict) -> dict:
    """시나리오를 수정한다."""
    raise NotImplementedError


@router.delete("/{scenario_id}", status_code=204)
async def delete_scenario(scenario_id: str) -> None:
    """시나리오를 삭제한다.

    generate_scenarios 와 generate_code 사이에서 사용자가 불필요한
    시나리오를 제거하기 위해 사용한다.
    """
    raise NotImplementedError
