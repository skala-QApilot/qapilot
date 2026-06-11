"""run eb5145b7 심층 검증에서 발견된 도구 버그 2건의 회귀 테스트.

버그 A: `expect(page).to_have_url("**/dashboard")` — Playwright 의 to_have_url 은
문자열을 base_url join 후 exact 비교 (glob 미지원) → 로그인 성공 + 대시보드
도달인데 assert_url 이 항상 fail. glob 문자는 wait_for_url 로 검증해야 한다.

버그 B: TC 1개의 Tool 60s 타임아웃이 pipeline_failed 로 run 전체를 abort —
per-TC 흡수 후 failed 기록 + 다음 TC 계속이 정답. (B 는 e2e 재실행으로 검증,
여기서는 A 만 단위 테스트.)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.tools.ui_test_tool import UITestTool


def _tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool


@pytest.mark.asyncio
async def test_assert_url_glob_uses_wait_for_url():
    page = MagicMock()
    page.wait_for_url = AsyncMock()
    step = {"action": "assert_url", "expected": "**/dashboard", "value": None}
    await _tool()._run_step(page, step, "")
    page.wait_for_url.assert_awaited_once()
    assert page.wait_for_url.await_args.args[0] == "**/dashboard"


@pytest.mark.asyncio
async def test_assert_url_glob_timeout_raises_assertion_error():
    from playwright.async_api import TimeoutError as PWTimeoutError
    page = MagicMock()
    page.url = "http://sut/login"
    page.wait_for_url = AsyncMock(side_effect=PWTimeoutError("timeout"))
    step = {"action": "assert_url", "expected": "**/dashboard", "value": None}
    with pytest.raises(AssertionError):
        await _tool()._run_step(page, step, "")
