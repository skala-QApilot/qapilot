"""service scope 시나리오 그래프 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.graph_store import build_scenario_flow, build_scenario_graph
from qapilot.shared.scenario_store import load_all_scenarios
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["graph"])


@router.get("/scenario-graph")
async def scenario_graph(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """현재 시나리오 기반 의존성 그래프를 반환한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    graph = build_scenario_graph(load_all_scenarios(service))
    return ok({"graph": graph})


@router.get("/scenario-flow")
async def scenario_flow(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """현재 시나리오 기반 TC 흐름 그래프를 반환한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    flow = build_scenario_flow(load_all_scenarios(service))
    return ok({"flow": flow})


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")
