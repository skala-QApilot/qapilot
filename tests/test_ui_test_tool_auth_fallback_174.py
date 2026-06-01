"""이슈 #174 — UITestTool `_ensure_authenticated` fail-safe (격차 12 D 영역 본질) 검증.

배경: 2026-05-21 e2e (trace `407d8878`) 의 1 pass / 93 fail 분석 결과 — 인증 필요
시나리오의 ActionMapping 에 로그인 step 부재 (시나리오 책임 경계 정합).
UITestTool 측 precondition fail-safe 부재로 SUT `/login` redirect 한 자리에서 멈춤.

시나리오 책임 경계 — 시나리오는 self-contained 가 아니라 pytest fixture /
Playwright `test.beforeEach` / Cucumber Background 패턴. 본 layer 가 UITestTool 책임.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.tools.ui_test_tool import UITestTool


def _make_tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool


@pytest.mark.asyncio
async def test_no_test_account_graceful_skip(mock_page):
    """test_account 미설정 → 즉시 skip + 로그 X (graceful)."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/login"
    await tool._ensure_authenticated(mock_page, [{"action": "click"}], "http://localhost:3000", None)
    # 어떤 fill/click 도 시도 안 함
    tool.logger.info.assert_not_called()


@pytest.mark.asyncio
async def test_account_missing_field_skip(mock_page):
    """email 또는 password 누락 → skip."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/login"
    await tool._ensure_authenticated(
        mock_page, [{"action": "click"}], "http://localhost:3000",
        {"email": "x@y.com"},  # password 누락
    )
    tool.logger.info.assert_not_called()


@pytest.mark.asyncio
async def test_not_login_url_skip(mock_page):
    """현재 페이지가 /login 패턴 아니면 (이미 인증) skip."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/overview"
    await tool._ensure_authenticated(
        mock_page, [{"action": "assert"}], "http://localhost:3000",
        {"email": "x@y.com", "password": "p"},
    )
    tool.logger.info.assert_not_called()


@pytest.mark.asyncio
async def test_login_url_pattern_match_login(mock_page):
    """/login 매칭 → 로그인 시도."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/login?next=/overview"
    # fill / click 모두 first locator 에서 성공
    mock_page.wait_for_load_state = AsyncMock()
    # 로그인 후 URL = /overview (인증 통과)
    type(mock_page).url = "http://localhost:3000/overview"  # PropertyMock 안 쓰고 그냥 attribute 갈아끼움
    # 실 시뮬레이션 위해 page.url 호출 시점에 따라 다르게 반환
    urls = iter(["http://localhost:3000/login?next=/overview", "http://localhost:3000/overview"])
    mock_page.url = next(urls)

    # locator 들이 모두 같은 mock_page.locator return — _build_mock_locator 가 fill/click AsyncMock
    await tool._ensure_authenticated(
        mock_page,
        [{"action": "fill", "selector": "x", "selector_type": "text"}],
        "http://localhost:3000",
        {"email": "demo@test.com", "password": "Passw0rd!"},
    )
    # 로그인 시도 로그 호출됨
    info_calls = [c.args[0] for c in tool.logger.info.call_args_list]
    assert "ui_auth_fallback_start" in info_calls


@pytest.mark.asyncio
async def test_login_signin_pattern_match(mock_page):
    """`/signin` 도 default 패턴 매칭."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/signin"
    await tool._ensure_authenticated(
        mock_page, [{"action": "click"}], "http://localhost:3000",
        {"email": "x@y.com", "password": "p"},
    )
    info_calls = [c.args[0] for c in tool.logger.info.call_args_list]
    assert "ui_auth_fallback_start" in info_calls


@pytest.mark.asyncio
async def test_custom_login_path(mock_page):
    """사용자 지정 login_path 매칭."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/custom-auth"
    await tool._ensure_authenticated(
        mock_page, [{"action": "click"}], "http://localhost:3000",
        {"email": "x@y.com", "password": "p", "login_path": "/custom-auth"},
    )
    info_calls = [c.args[0] for c in tool.logger.info.call_args_list]
    assert "ui_auth_fallback_start" in info_calls


@pytest.mark.asyncio
async def test_email_password_fill_invoked(mock_page):
    """email/password fill 이 실제로 호출됨 (first locator 성공 가정)."""
    tool = _make_tool()
    mock_page.url = "http://localhost:3000/login"
    locator = mock_page.locator.return_value  # 공유 locator (conftest)
    await tool._ensure_authenticated(
        mock_page, [], "http://localhost:3000",
        {"email": "demo@test.com", "password": "Passw0rd!"},
    )
    # fill 호출 인자에 email + password 포함되는지
    fill_calls = [c for c in locator.fill.call_args_list]
    fill_values = [c.args[0] if c.args else c.kwargs.get("value") for c in fill_calls]
    assert any("demo@test.com" in str(v) for v in fill_values)
    assert any("Passw0rd!" in str(v) for v in fill_values)
