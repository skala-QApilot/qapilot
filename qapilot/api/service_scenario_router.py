"""service scope 시나리오 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.logger import get_logger
from qapilot.shared.scenario_store import (
    delete_scenario,
    export_scenarios_csv,
    load_all_scenarios,
    load_scenario,
    save_scenario,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["scenarios"])
logger = get_logger("api.service_scenarios")


@router.get("/scenarios")
async def list_scenarios(
    service_id: str,
    search: str | None = None,
    trigger: str | None = None,
    tags: str | None = None,
    user: dict = Depends(get_current_user),
) -> Any:
    """service scope 시나리오 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    scenarios = _filter_scenarios(load_all_scenarios(service), search, trigger, tags)
    logger.info("service_scenarios_listed", user_id=user["user_id"], count=len(scenarios))
    return ok({"scenarios": scenarios, "count": len(scenarios)})


@router.post("/scenarios")
async def create_scenario(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """service scope 시나리오를 생성한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    error = _validate_scenario(body)
    if error:
        return error
    path = save_scenario(service, body)
    logger.info("service_scenario_created", user_id=user["user_id"], ts_id=body["ts_id"])
    return JSONResponse(status_code=201, content=ok({"scenario": body, "path": str(path)}))


@router.get("/scenarios/export")
async def export_scenarios(
    service_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """service scope 시나리오 목록을 CSV로 다운로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    logger.info("service_scenarios_exported", user_id=user["user_id"], service_id=service_id)
    return Response(
        content=export_scenarios_csv(service),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scenarios.csv"'},
    )


@router.get("/scenarios/{scenario_id}")
async def get_scenario(
    service_id: str,
    scenario_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """service scope 시나리오 상세를 조회한다."""
    scenario_or_error = _scenario_or_error(service_id, scenario_id)
    if isinstance(scenario_or_error, JSONResponse):
        return scenario_or_error
    logger.info("service_scenario_fetched", user_id=user["user_id"], ts_id=scenario_id)
    return ok({"scenario": scenario_or_error})


@router.patch("/scenarios/{scenario_id}")
async def patch_scenario(
    service_id: str,
    scenario_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """service scope 시나리오를 부분 수정한다."""
    service = _service_or_none(service_id)
    scenario = load_scenario(service, scenario_id) if service else None
    if not service:
        return _service_not_found()
    if not scenario:
        return _scenario_not_found()
    body = await _json_body(request)
    scenario.update({k: v for k, v in body.items() if k != "ts_id"})
    scenario["ts_id"] = scenario_id
    save_scenario(service, scenario)
    logger.info("service_scenario_updated", user_id=user["user_id"], ts_id=scenario_id)
    return ok({"scenario": scenario})


@router.delete("/scenarios/{scenario_id}", status_code=204, response_class=Response)
async def remove_scenario(
    service_id: str,
    scenario_id: str,
    user: dict = Depends(get_current_user),
):
    """service scope 시나리오를 삭제한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not delete_scenario(service, scenario_id):
        return _scenario_not_found()
    logger.info("service_scenario_deleted", user_id=user["user_id"], ts_id=scenario_id)
    return Response(status_code=204)


@router.get("/scenarios/{scenario_id}/test-cases")
async def list_test_cases(
    service_id: str,
    scenario_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오의 테스트케이스 목록을 조회한다."""
    scenario_or_error = _scenario_or_error(service_id, scenario_id)
    if isinstance(scenario_or_error, JSONResponse):
        return scenario_or_error
    test_cases = scenario_or_error.get("test_cases", [])
    logger.info("test_cases_listed", user_id=user["user_id"], count=len(test_cases))
    return ok({"test_cases": test_cases, "count": len(test_cases)})


@router.post("/scenarios/{scenario_id}/test-cases")
async def create_test_case(
    service_id: str,
    scenario_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """시나리오에 테스트케이스를 추가한다."""
    loaded = _load_service_and_scenario(service_id, scenario_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario = loaded
    body = await _json_body(request)
    error = _validate_test_case(body)
    if error:
        return error
    scenario.setdefault("test_cases", []).append(body)
    save_scenario(service, scenario)
    logger.info("test_case_created", user_id=user["user_id"], tc_id=body["tc_id"])
    return JSONResponse(status_code=201, content=ok({"test_case": body}))


@router.patch("/scenarios/{scenario_id}/test-cases/{tc_id}")
async def patch_test_case(
    service_id: str,
    scenario_id: str,
    tc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """테스트케이스를 부분 수정한다."""
    loaded = _load_service_and_scenario(service_id, scenario_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario = loaded
    body = await _json_body(request)
    test_case = _find_test_case(scenario, tc_id)
    if not test_case:
        return _tc_not_found()
    test_case.update({k: v for k, v in body.items() if k != "tc_id"})
    test_case["tc_id"] = tc_id
    save_scenario(service, scenario)
    logger.info("test_case_updated", user_id=user["user_id"], tc_id=tc_id)
    return ok({"test_case": test_case})


@router.delete(
    "/scenarios/{scenario_id}/test-cases/{tc_id}",
    status_code=204,
    response_class=Response,
)
async def remove_test_case(
    service_id: str,
    scenario_id: str,
    tc_id: str,
    user: dict = Depends(get_current_user),
):
    """테스트케이스를 삭제한다."""
    loaded = _load_service_and_scenario(service_id, scenario_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario = loaded
    original = len(scenario.get("test_cases", []))
    scenario["test_cases"] = [tc for tc in scenario.get("test_cases", []) if tc.get("tc_id") != tc_id]
    if len(scenario["test_cases"]) == original:
        return _tc_not_found()
    save_scenario(service, scenario)
    logger.info("test_case_deleted", user_id=user["user_id"], tc_id=tc_id)
    return Response(status_code=204)


@router.post("/scenarios/{scenario_id}/test-cases/{tc_id}/test-variables")
async def create_test_variable(
    service_id: str,
    scenario_id: str,
    tc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """테스트케이스에 테스트 변수를 추가한다."""
    loaded = _load_service_scenario_tc(service_id, scenario_id, tc_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario, test_case = loaded
    body = await _json_body(request)
    if not str(body.get("name") or "").strip():
        return fail("REQUEST_400", "name 필드가 필요합니다.")
    tv = _new_test_variable(test_case, body)
    test_case.setdefault("values", []).append(tv)
    save_scenario(service, scenario)
    logger.info("test_variable_created", user_id=user["user_id"], tv_id=tv["tv_id"])
    return JSONResponse(status_code=201, content=ok({"test_variable": tv}))


@router.patch("/scenarios/{scenario_id}/test-cases/{tc_id}/test-variables/{tv_id}")
async def patch_test_variable(
    service_id: str,
    scenario_id: str,
    tc_id: str,
    tv_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """테스트 변수를 부분 수정한다."""
    loaded = _load_service_scenario_tc(service_id, scenario_id, tc_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario, test_case = loaded
    tv = _find_test_variable(test_case, tv_id)
    if not tv:
        return _tv_not_found()
    body = await _json_body(request)
    _update_test_variable(tv, body)
    save_scenario(service, scenario)
    logger.info("test_variable_updated", user_id=user["user_id"], tv_id=tv_id)
    return ok({"test_variable": tv})


@router.delete(
    "/scenarios/{scenario_id}/test-cases/{tc_id}/test-variables/{tv_id}",
    status_code=204,
    response_class=Response,
)
async def remove_test_variable(
    service_id: str,
    scenario_id: str,
    tc_id: str,
    tv_id: str,
    user: dict = Depends(get_current_user),
):
    """테스트 변수를 삭제한다."""
    loaded = _load_service_scenario_tc(service_id, scenario_id, tc_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario, test_case = loaded
    original = len(test_case.get("values", []))
    test_case["values"] = [tv for tv in test_case.get("values", []) if tv.get("tv_id") != tv_id]
    if len(test_case["values"]) == original:
        return _tv_not_found()
    save_scenario(service, scenario)
    logger.info("test_variable_deleted", user_id=user["user_id"], tv_id=tv_id)
    return Response(status_code=204)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _scenario_or_error(service_id: str, scenario_id: str) -> dict | JSONResponse:
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    scenario = load_scenario(service, scenario_id)
    return scenario if scenario else _scenario_not_found()


def _load_service_and_scenario(service_id: str, scenario_id: str) -> tuple[dict, dict] | JSONResponse:
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    scenario = load_scenario(service, scenario_id)
    return (service, scenario) if scenario else _scenario_not_found()


def _load_service_scenario_tc(
    service_id: str,
    scenario_id: str,
    tc_id: str,
) -> tuple[dict, dict, dict] | JSONResponse:
    loaded = _load_service_and_scenario(service_id, scenario_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    service, scenario = loaded
    test_case = _find_test_case(scenario, tc_id)
    if not test_case:
        return _tc_not_found()
    return service, scenario, test_case


def _filter_scenarios(
    scenarios: list[dict],
    search: str | None,
    trigger: str | None,
    tags: str | None,
) -> list[dict]:
    if search:
        q = search.lower()
        scenarios = [s for s in scenarios if q in f"{s.get('name', '')} {s.get('description', '')}".lower()]
    if trigger:
        scenarios = [s for s in scenarios if s.get("trigger") == trigger]
    if tags:
        wanted = {tag.strip() for tag in tags.split(",") if tag.strip()}
        scenarios = [s for s in scenarios if _has_tags(s, wanted)]
    return scenarios


def _has_tags(scenario: dict, wanted: set[str]) -> bool:
    return any(wanted.intersection(set(tc.get("tags", []))) for tc in scenario.get("test_cases", []))


def _find_test_case(scenario: dict, tc_id: str) -> dict | None:
    return next((tc for tc in scenario.get("test_cases", []) if tc.get("tc_id") == tc_id), None)


def _find_test_variable(test_case: dict, tv_id: str) -> dict | None:
    return next((tv for tv in test_case.get("values", []) if tv.get("tv_id") == tv_id), None)


def _new_test_variable(test_case: dict, body: dict[str, Any]) -> dict:
    values = test_case.get("values", [])
    return {
        "tv_id": f"TV-{len(values) + 1:03d}",
        "name": str(body["name"]),
        "status": body.get("status") or "pending",
        "validation_conditions": body.get("validation_conditions") or [],
        "endpoint": body.get("endpoint"),
    }


def _update_test_variable(tv: dict, body: dict[str, Any]) -> None:
    for key in ("name", "status", "validation_conditions", "endpoint"):
        if key in body:
            tv[key] = body[key]


def _validate_scenario(body: dict[str, Any]) -> JSONResponse | None:
    required = ["ts_id", "name", "description", "trigger", "affected_files", "domain_rules_used", "test_cases"]
    missing = [field for field in required if field not in body]
    return fail("REQUEST_400", f"{', '.join(missing)} 필드가 필요합니다.") if missing else None


def _validate_test_case(body: dict[str, Any]) -> JSONResponse | None:
    required = ["tc_id", "name", "given", "when", "then"]
    missing = [field for field in required if field not in body]
    return fail("REQUEST_400", f"{', '.join(missing)} 필드가 필요합니다.") if missing else None


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _scenario_not_found() -> JSONResponse:
    return fail(ErrorCode.SCENARIO_001, "시나리오를 찾을 수 없습니다.")


def _tc_not_found() -> JSONResponse:
    return fail(ErrorCode.TC_001, "테스트케이스를 찾을 수 없습니다.")


def _tv_not_found() -> JSONResponse:
    return fail(ErrorCode.TV_001, "테스트변수를 찾을 수 없습니다.")
