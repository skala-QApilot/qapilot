"""PR #197 후속 3건 검증 — TS-002 trace e9567245 진단 결과 보강.

A. navigate value 가 backend API URL (/api/...) 일 때 frontend route 추론
B. _should_auto_navigate 발동 조건 확장 — wait/assert + api_endpoint
C. chain timeout 단축 — 5s + 3s*3 = 14s (이전 10+5*3=25s)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.tools.ui_test_tool import (
    _CHAIN_FALLBACK_TIMEOUT_MS,
    _CHAIN_PRIMARY_TIMEOUT_MS,
    UITestTool,
)


def _make_tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool


# ── A. _infer_route_from_path + navigate /api/ 보정 ──


@pytest.mark.parametrize("api_path, expected", [
    ("/api/plans/-1", "/plans"),
    ("/api/plans/9999", "/plans"),
    ("/api/auth/signup", "/signup"),
    ("/api/auth/login", "/login"),
    ("/api/contracts/1/cancel", "/contracts"),
    ("/api/family-group/join", "/family-group"),
    ("/plans/{id}", "/plans"),
    ("/login", "/login"),
])
def test_infer_route_from_path(api_path, expected):
    tool = _make_tool()
    assert tool._infer_route_from_path(api_path) == expected


def test_infer_route_from_path_returns_none_for_empty():
    tool = _make_tool()
    assert tool._infer_route_from_path("") is None
    assert tool._infer_route_from_path("/api") is None
    assert tool._infer_route_from_path(None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_navigate_coerces_api_url_to_frontend_route(mock_page):
    """ActionMapper 환각 — navigate value=/api/plans/-1 → /plans 로 보정 후 goto."""
    tool = _make_tool()
    step = {"action": "navigate", "value": "/api/plans/-1"}

    await tool._run_page_action(mock_page, "navigate", step, "http://sut.local")

    # backend URL 그대로 가지 않고 보정된 /plans 로
    mock_page.goto.assert_awaited_once_with("http://sut.local/plans")
    warnings = [c for c in tool.logger.warning.call_args_list
                if c.args and c.args[0] == "ui_navigate_api_url_coerced"]
    assert len(warnings) == 1
    assert warnings[0].kwargs["original"] == "/api/plans/-1"
    assert warnings[0].kwargs["inferred"] == "/plans"


@pytest.mark.asyncio
async def test_navigate_passes_through_non_api_path(mock_page):
    """frontend route (/signup 등) 은 보정 없이 그대로 통과."""
    tool = _make_tool()
    step = {"action": "navigate", "value": "/signup"}

    await tool._run_page_action(mock_page, "navigate", step, "http://sut.local")

    mock_page.goto.assert_awaited_once_with("http://sut.local/signup")
    # 보정 발동 안 함
    coerce_warnings = [c for c in tool.logger.warning.call_args_list
                       if c.args and c.args[0] == "ui_navigate_api_url_coerced"]
    assert len(coerce_warnings) == 0


@pytest.mark.asyncio
async def test_navigate_absolute_url_unchanged(mock_page):
    """absolute URL 은 보정 X (외부 URL — 의도 존중)."""
    tool = _make_tool()
    step = {"action": "navigate", "value": "https://example.com/api/foo"}

    await tool._run_page_action(mock_page, "navigate", step, "http://sut.local")

    mock_page.goto.assert_awaited_once_with("https://example.com/api/foo")


# ── B. _should_auto_navigate 발동 조건 ──


def test_should_auto_navigate_dom_first_step():
    """첫 step 이 DOM action — PR #122 원본 조건 발동."""
    tool = _make_tool()
    steps = [{"action": "fill"}, {"action": "click", "api_endpoint": "POST /api/login"}]
    assert tool._should_auto_navigate(steps) is True


def test_should_auto_navigate_explicit_navigate_skip():
    """첫 step 이 navigate — 시나리오 의도 존중, 발동 X."""
    tool = _make_tool()
    steps = [{"action": "navigate", "value": "/plans"}, {"action": "assert"}]
    assert tool._should_auto_navigate(steps) is False


def test_should_auto_navigate_wait_with_endpoint_triggers():
    """#197 보강 — 첫 step wait + 후속 api_endpoint 있으면 발동."""
    tool = _make_tool()
    steps = [
        {"action": "wait", "value": 1000},
        {"action": "assert", "selector": "요금제 상세", "api_endpoint": "GET /api/plans/1"},
    ]
    assert tool._should_auto_navigate(steps) is True


def test_should_auto_navigate_wait_without_endpoint_no_trigger():
    """첫 step wait + api_endpoint 추론 불가 → 발동 X (기존 동작 보존)."""
    tool = _make_tool()
    steps = [
        {"action": "wait", "value": 1000},
        {"action": "assert", "selector": "x", "api_endpoint": None},
    ]
    assert tool._should_auto_navigate(steps) is False


def test_should_auto_navigate_empty_steps():
    tool = _make_tool()
    assert tool._should_auto_navigate([]) is False


# ── C. chain timeout 단축 — 회귀 (상수 값 자체) ──


def test_chain_timeout_constants_v3():
    """#197 후속 v3 (run 1ead19b7 병목) — navigate 가 load state 를 기다린 뒤라
    3.5s 안에 안 나타나는 요소는 사실상 부재. 절감은 부재(fail 운명) 케이스에만."""
    assert _CHAIN_PRIMARY_TIMEOUT_MS == 3_500
    assert _CHAIN_FALLBACK_TIMEOUT_MS == 1_500
