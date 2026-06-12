"""이슈 #121 — UITestTool auto-navigate (옵션 C) 검증.

배경: e2e trace `e1796b43` 의 73/73 UI fail 분석 결과 — ActionMapping 의 첫 step
이 DOM action 인데 navigate step 부재로 SUT 의 잘못된 페이지 (vue-router redirect
결과) 에서 시작되어 옵션 A chain / 옵션 B DOM scan 모두 fail. 본 PR 은 첫 step 이
DOM action 인 경우 `api_endpoint` 힌트로 frontend route 추론 + page.goto 자동 호출.

상세 배경: docs/e2e-navigate-gap-analysis.md
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.tools.ui_test_tool import UITestTool


def _make_tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool



# ── _infer_target_route — 순수 함수 단위 검증 ────────────────────────────────


@pytest.mark.parametrize("api_endpoint, expected", [
    ("POST /login", "/login"),
    ("GET /signup", "/signup"),
    ("/plans", "/plans"),  # HTTP method 없는 형식
    ("GET /plans/{id}", "/plans"),  # {id} 제거
    ("DELETE /orders/:id", "/orders"),  # :id 제거
    # 이슈 #123 v2: 깊은 path → 1차 segment 만
    ("GET /family/members/{member_id}", "/family"),  # 1-segment 휴리스틱
    ("POST  /signup", "/signup"),  # 공백 여러 칸
    ("PATCH /profile", "/profile"),
    # 이슈 #123 v2: /api/ prefix 제거
    ("POST /api/login", "/login"),
    ("GET /api/plans", "/plans"),
    ("DELETE /api/contracts/1/cancel", "/contracts"),
    ("GET /api/family-group/join", "/family-group"),
    ("PATCH /api/orders/{id}/status", "/orders"),
])
def test_infer_target_route_various_formats(api_endpoint, expected):
    tool = _make_tool()
    steps = [{"action": "fill", "selector": "x", "selector_type": "css",
              "api_endpoint": api_endpoint}]
    assert tool._infer_target_route(steps) == expected


def test_infer_target_route_api_only_skips():
    """`/api` 단독은 의미 없는 prefix — skip 후 다음 step 시도."""
    tool = _make_tool()
    steps = [
        {"action": "fill", "api_endpoint": "GET /api"},  # 의미 없음 → skip
        {"action": "click", "api_endpoint": "POST /api/login"},  # 다음 사용
    ]
    assert tool._infer_target_route(steps) == "/login"


def test_infer_target_route_uses_first_endpoint():
    """여러 step 의 api_endpoint 중 첫 발견을 사용."""
    tool = _make_tool()
    steps = [
        {"action": "fill", "api_endpoint": None},
        {"action": "fill", "api_endpoint": "POST /login"},   # 첫 발견
        {"action": "click", "api_endpoint": "GET /dashboard"},
    ]
    assert tool._infer_target_route(steps) == "/login"


def test_infer_target_route_returns_none_when_no_endpoint():
    """모든 step 의 api_endpoint 가 None 또는 부재 → None."""
    tool = _make_tool()
    steps = [
        {"action": "fill", "selector": "이메일"},
        {"action": "click", "selector": "버튼", "api_endpoint": None},
    ]
    assert tool._infer_target_route(steps) is None


def test_infer_target_route_returns_none_for_empty_steps():
    tool = _make_tool()
    assert tool._infer_target_route([]) is None


def test_infer_target_route_handles_non_string_endpoint():
    """api_endpoint 값이 str 이 아니면 skip."""
    tool = _make_tool()
    steps = [
        {"action": "fill", "api_endpoint": 123},
        {"action": "fill", "api_endpoint": "POST /login"},
    ]
    assert tool._infer_target_route(steps) == "/login"


def test_infer_target_route_malformed_endpoint_returns_none():
    """경로 형식 아닌 api_endpoint 는 skip."""
    tool = _make_tool()
    steps = [{"api_endpoint": "garbage no slash"}]
    assert tool._infer_target_route(steps) is None


# ── _try_auto_navigate — page.goto 호출 + 로깅 ──────────────────────────────


@pytest.mark.asyncio
async def test_try_auto_navigate_calls_page_goto_on_inferred_route(mock_page):
    """api_endpoint 있음 → page.goto(target_url + route) + info log."""
    tool = _make_tool()
    page = mock_page
    steps = [
        {"action": "fill", "selector": "이메일", "api_endpoint": None},
        {"action": "click", "selector": "로그인", "api_endpoint": "POST /login"},
    ]
    await tool._try_auto_navigate(page, steps, "http://localhost:3000")

    # PR #122 정합: SPA hydrate 보장을 위해 networkidle + 15s timeout 사용.
    page.goto.assert_awaited_once_with("http://localhost:3000/login", wait_until="networkidle", timeout=8000)
    info_calls = [c for c in tool.logger.info.call_args_list if c.args and c.args[0] == "ui_auto_navigate"]
    assert len(info_calls) == 1
    assert info_calls[0].kwargs["route"] == "/login"
    assert info_calls[0].kwargs["full_url"] == "http://localhost:3000/login"


@pytest.mark.asyncio
async def test_try_auto_navigate_strips_trailing_slash_from_target_url(mock_page):
    """target_url 의 trailing slash 제거 후 route prefix 부착."""
    tool = _make_tool()
    page = mock_page
    steps = [{"action": "fill", "api_endpoint": "POST /login"}]
    await tool._try_auto_navigate(page, steps, "http://localhost:3000/")
    page.goto.assert_awaited_once_with("http://localhost:3000/login", wait_until="networkidle", timeout=8000)


@pytest.mark.asyncio
async def test_try_auto_navigate_skips_when_no_api_endpoint(mock_page):
    """api_endpoint 부재 → page.goto 호출 X + debug log."""
    tool = _make_tool()
    page = mock_page
    steps = [{"action": "fill", "selector": "이메일"}]

    await tool._try_auto_navigate(page, steps, "http://localhost:3000")

    page.goto.assert_not_called()
    debug_calls = [c for c in tool.logger.debug.call_args_list if c.args and c.args[0] == "ui_auto_navigate_skipped"]
    assert len(debug_calls) == 1
    assert debug_calls[0].kwargs["reason"] == "no_api_endpoint_hint"


@pytest.mark.asyncio
async def test_try_auto_navigate_graceful_on_goto_failure(mock_page):
    """page.goto 가 raise 해도 후속 진행 — warning log + 예외 흡수."""
    tool = _make_tool()
    page = mock_page
    page.goto = AsyncMock(side_effect=RuntimeError("network unreachable"))
    steps = [{"action": "fill", "api_endpoint": "POST /login"}]

    # raise 안 함 (graceful)
    await tool._try_auto_navigate(page, steps, "http://localhost:3000")

    warning_calls = [c for c in tool.logger.warning.call_args_list if c.args and c.args[0] == "ui_auto_navigate_failed"]
    assert len(warning_calls) == 1
    assert warning_calls[0].kwargs["route"] == "/login"


# ── _run_steps 진입 시 auto-navigate 호출 시점 ──────────────────────────────


@pytest.mark.asyncio
async def test_run_steps_triggers_auto_navigate_for_dom_first_step():
    """첫 step 이 DOM action → _try_auto_navigate 호출."""
    tool = _make_tool()
    tool._try_auto_navigate = AsyncMock()
    tool._run_step = AsyncMock()  # step loop 도 mock — 실제 실행 안 함
    tool._capture_screenshot = AsyncMock(return_value=None)

    page = MagicMock()
    steps = [
        {"step_no": 1, "action": "fill", "selector": "x", "selector_type": "css",
         "api_endpoint": "POST /login"},
    ]
    await tool._run_steps(page, steps, "http://localhost:3000", None, [])

    tool._try_auto_navigate.assert_awaited_once()
    # 첫 인자는 page, 두 번째는 steps, 세 번째는 target_url
    call_args = tool._try_auto_navigate.call_args.args
    assert call_args[0] is page
    assert call_args[1] is steps
    assert call_args[2] == "http://localhost:3000"


@pytest.mark.asyncio
async def test_run_steps_skips_auto_navigate_when_first_step_is_navigate():
    """첫 step 이 navigate → auto-navigate 호출 X (기존 동작 유지)."""
    tool = _make_tool()
    tool._try_auto_navigate = AsyncMock()
    tool._run_step = AsyncMock()
    tool._capture_screenshot = AsyncMock(return_value=None)

    page = MagicMock()
    steps = [
        {"step_no": 1, "action": "navigate", "value": "/login"},
    ]
    await tool._run_steps(page, steps, "http://localhost:3000", None, [])

    tool._try_auto_navigate.assert_not_called()


@pytest.mark.asyncio
async def test_run_steps_skips_auto_navigate_when_first_step_is_wait():
    """첫 step 이 page-level action (wait 등) → auto-navigate 안 함."""
    tool = _make_tool()
    tool._try_auto_navigate = AsyncMock()
    tool._run_step = AsyncMock()
    tool._capture_screenshot = AsyncMock(return_value=None)

    page = MagicMock()
    steps = [{"step_no": 1, "action": "wait", "value": "1000"}]
    await tool._run_steps(page, steps, "http://localhost:3000", None, [])

    tool._try_auto_navigate.assert_not_called()


@pytest.mark.asyncio
async def test_run_steps_skips_auto_navigate_for_empty_steps():
    """steps 비어있음 → auto-navigate 안 함."""
    tool = _make_tool()
    tool._try_auto_navigate = AsyncMock()
    tool._capture_screenshot = AsyncMock(return_value=None)

    page = MagicMock()
    await tool._run_steps(page, [], "http://localhost:3000", None, [])

    tool._try_auto_navigate.assert_not_called()


# ── 이슈 #123 P2 — invalid URL 방어 ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("target_url", [None, "", "/login", "localhost:3000", "//foo.com"])
async def test_try_auto_navigate_skips_invalid_target_url(mock_page, target_url):
    """target_url 가 None / empty / path-only / scheme 부재 → page.goto 안 함 + warning."""
    tool = _make_tool()
    page = mock_page
    steps = [{"action": "fill", "api_endpoint": "POST /login"}]

    await tool._try_auto_navigate(page, steps, target_url)

    page.goto.assert_not_called()
    warning_calls = [c for c in tool.logger.warning.call_args_list if c.args and c.args[0] == "ui_auto_navigate_skipped"]
    assert len(warning_calls) == 1
    assert warning_calls[0].kwargs["reason"] == "target_url_missing_or_not_absolute"


@pytest.mark.asyncio
async def test_try_auto_navigate_accepts_https_target_url(mock_page):
    """https://... target_url 도 정상 처리."""
    tool = _make_tool()
    page = mock_page
    steps = [{"action": "fill", "api_endpoint": "POST /login"}]

    await tool._try_auto_navigate(page, steps, "https://staging.example.com")

    page.goto.assert_awaited_once_with("https://staging.example.com/login", wait_until="networkidle", timeout=8000)


# ── 이슈 #147: AUTO_NAVIGATE / TARGET_UNREACHABLE 메타 코드 발행 검증 ─────


@pytest.mark.asyncio
async def test_auto_navigate_emits_auto_navigate_code(mock_page):
    """이슈 #147: auto-navigate 성공 시 TOOL_UI_AUTO_NAVIGATE 메타 코드 발행."""
    from qapilot.shared.errors import ErrorCode
    tool = _make_tool()
    tool.logger = MagicMock()
    page = mock_page

    steps = [
        {"step_no": 1, "action": "fill", "selector": "x", "selector_type": "css",
         "api_endpoint": "POST /api/login"},
    ]
    await tool._try_auto_navigate(page, steps, "http://localhost:3000")

    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_auto_navigate"]
    assert len(info_calls) == 1
    assert info_calls[0].kwargs.get("code") == ErrorCode.TOOL_UI_AUTO_NAVIGATE


@pytest.mark.asyncio
async def test_auto_navigate_invalid_target_url_emits_target_unreachable(mock_page):
    """이슈 #147: target_url 부재/scheme 부재 → TOOL_UI_TARGET_UNREACHABLE 발행."""
    from qapilot.shared.errors import ErrorCode
    tool = _make_tool()
    tool.logger = MagicMock()
    page = mock_page
    steps = [
        {"step_no": 1, "action": "fill", "selector": "x", "selector_type": "css",
         "api_endpoint": "POST /login"},
    ]
    # target_url 빈 값
    await tool._try_auto_navigate(page, steps, "")

    warning_calls = [c for c in tool.logger.warning.call_args_list
                     if c.args and c.args[0] == "ui_auto_navigate_skipped"]
    assert len(warning_calls) == 1
    assert warning_calls[0].kwargs.get("code") == ErrorCode.TOOL_UI_TARGET_UNREACHABLE


@pytest.mark.asyncio
async def test_auto_navigate_page_goto_failure_emits_target_unreachable(mock_page):
    """이슈 #147: page.goto 실패 → TOOL_UI_TARGET_UNREACHABLE 발행."""
    from qapilot.shared.errors import ErrorCode
    tool = _make_tool()
    tool.logger = MagicMock()
    page = mock_page
    page.goto = AsyncMock(side_effect=Exception("net::ERR_CONNECTION_REFUSED"))
    steps = [
        {"step_no": 1, "action": "fill", "selector": "x", "selector_type": "css",
         "api_endpoint": "POST /login"},
    ]
    await tool._try_auto_navigate(page, steps, "http://localhost:3000")

    warning_calls = [c for c in tool.logger.warning.call_args_list
                     if c.args and c.args[0] == "ui_auto_navigate_failed"]
    assert len(warning_calls) == 1
    assert warning_calls[0].kwargs.get("code") == ErrorCode.TOOL_UI_TARGET_UNREACHABLE
