"""시나리오 라우터.

담당: C
Created: 2026-05-07
"""

from fastapi import APIRouter, HTTPException

from qapilot.agents.scenario_generator.repository import (
    delete_scenario as delete_scenario_file,
    load_all_scenarios,
    load_scenario,
    save_scenario,
)

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("")
async def list_scenarios() -> dict:
    """시나리오 목록을 조회한다."""
    scenarios = load_all_scenarios()
    return {"scenarios": scenarios, "count": len(scenarios)}


@router.post("", status_code=201)
async def create_scenario(request: dict) -> dict:
    """시나리오를 생성한다."""
    scenario = _validate_scenario_payload(request)
    path = save_scenario(scenario)
    return {"scenario": scenario, "path": str(path)}


@router.get("/{scenario_id}")
async def get_scenario(scenario_id: str) -> dict:
    """시나리오 상세를 조회한다."""
    scenario = load_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다.")
    return {"scenario": scenario}


@router.put("/{scenario_id}")
async def update_scenario(scenario_id: str, request: dict) -> dict:
    """시나리오를 수정한다."""
    existing = load_scenario(scenario_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다.")

    scenario = _validate_scenario_payload({**request, "ts_id": scenario_id})
    path = save_scenario(scenario)
    return {"scenario": scenario, "path": str(path)}


@router.delete("/{scenario_id}", status_code=204)
async def delete_scenario(scenario_id: str) -> None:
    """시나리오를 삭제한다.

    generate_scenarios 와 generate_code 사이에서 사용자가 불필요한
    시나리오를 제거하기 위해 사용한다.
    """
    deleted = delete_scenario_file(scenario_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다.")


def _validate_scenario_payload(payload: dict) -> dict:
    """HITL CRUD에서 최소 필수 필드만 검증한다."""
    required_fields = {
        "ts_id": str,
        "name": str,
        "description": str,
        "trigger": str,
        "affected_files": list,
        "domain_rules_used": list,
        "test_cases": list,
    }
    for field, expected_type in required_fields.items():
        if field not in payload:
            raise HTTPException(status_code=400, detail=f"{field} 필드가 필요합니다.")
        if not isinstance(payload[field], expected_type):
            raise HTTPException(
                status_code=400,
                detail=f"{field} 필드 타입이 올바르지 않습니다.",
            )
    return payload
