"""PR #277: SaaS UI 의 init trigger 는 prd_only_experiment 로 자동 진입.

기존: tc_target_ts_ids 명시 제공 시에만 prd_only_experiment.
신규: tc_target_ts_ids 미제공 + trigger="init" → prd_only_experiment default
       (본인 PR #269 의 TV codebase-aware + 5 public API 실 활용).

code_change / doc_update / natural_lang 은 기존 generate_scenarios 유지 (회귀 0).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from starlette.datastructures import State

from qapilot.api import agent_router as router_module
from qapilot.api.agent_router import (
    ScenarioGenerationRequestBody,
    scenario_generation,
)


def _make_request(trace_id: str = "trace-z") -> Request:
    req = MagicMock(spec=Request)
    st = State()
    st.trace_id = trace_id
    req.state = st
    return req


def _capture_start(captured: dict[str, Any]):
    def _fake(request, body_dict, options):  # noqa: ARG001
        captured["command"] = options["command"]
        captured["trigger"] = options.get("trigger")
        captured["tc_target_ts_ids"] = options.get("tc_target_ts_ids", "MISSING")
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content={"success": True, "data": {"trace_id": "t-x", "status": "running"}})
    return _fake


@pytest.mark.asyncio
async def test_init_trigger_defaults_to_prd_only_experiment():
    """trigger=init + tc_target_ts_ids 미제공 → prd_only_experiment default 진입."""
    captured: dict[str, Any] = {}
    body = ScenarioGenerationRequestBody(service_id="svc-1", trigger="init")  # tc_target_ts_ids 안 줌

    with patch.object(router_module, "_start_pipeline", side_effect=_capture_start(captured)):
        await scenario_generation(_make_request(), body)

    assert captured["command"] == "prd_only_experiment"
    assert captured["trigger"] == "init"
    assert captured["tc_target_ts_ids"] == []  # 빈 리스트 = 전체 TS 대상


@pytest.mark.asyncio
async def test_explicit_tc_target_ts_ids_still_uses_prd_only_experiment():
    """tc_target_ts_ids 명시 제공 — 기존 분기 그대로 prd_only_experiment."""
    captured: dict[str, Any] = {}
    body = ScenarioGenerationRequestBody(service_id="svc-1", trigger="init", tc_target_ts_ids=["TS-001", "TS-002"])

    with patch.object(router_module, "_start_pipeline", side_effect=_capture_start(captured)):
        await scenario_generation(_make_request(), body)

    assert captured["command"] == "prd_only_experiment"
    assert captured["tc_target_ts_ids"] == ["TS-001", "TS-002"]


@pytest.mark.asyncio
async def test_doc_update_trigger_uses_generate_scenarios():
    """trigger=doc_update → 기존 generate_scenarios 흐름 유지."""
    captured: dict[str, Any] = {}
    body = ScenarioGenerationRequestBody(service_id="svc-1", trigger="doc_update")

    with patch.object(router_module, "_start_pipeline", side_effect=_capture_start(captured)):
        await scenario_generation(_make_request(), body)

    assert captured["command"] == "generate_scenarios"
    assert captured["trigger"] == "doc_update"


@pytest.mark.asyncio
async def test_natural_lang_trigger_uses_generate_scenarios():
    """trigger=natural_lang → 기존 generate_scenarios 흐름 유지 (챗봇 응답)."""
    captured: dict[str, Any] = {}
    body = ScenarioGenerationRequestBody(
        service_id="svc-1", trigger="natural_lang", user_input="회원가입 시나리오 만들어줘",
    )

    with patch.object(router_module, "_start_pipeline", side_effect=_capture_start(captured)):
        await scenario_generation(_make_request(), body)

    assert captured["command"] == "generate_scenarios"
    assert captured["trigger"] == "natural_lang"
