"""UITestTool 단위 테스트.

mock Playwright Page 로 핵심 케이스 검증:
- params['page'] 누락 시 ToolExecutionError
- action_mapping.steps 비어있을 때 ToolExecutionError
- 지원하지 않는 action 시 fail 상태 + 후속 skip
- selector_type 9종 → 올바른 Page 메서드 호출
- action 6종 분기 동작
- navigate target_url prefix 처리
- 스크린샷 저장 (screenshot_dir 지정 시)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.shared.errors import ToolExecutionError
from qapilot.shared.schemas import ToolInput
from qapilot.tools.ui_test_tool import UITestTool


@pytest.fixture
def tool() -> UITestTool:
    return UITestTool(trace_id="test-trace-001")


def _mock_page() -> MagicMock:
    """모든 비동기 메서드가 AsyncMock 인 Playwright Page 모킹."""
    page = MagicMock()
    page.on = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.screenshot = AsyncMock()

    # locator + get_by_* 메서드들 — 모두 같은 mock locator 반환
    mock_locator = MagicMock()
    mock_locator.fill = AsyncMock()
    mock_locator.click = AsyncMock()
    mock_locator.select_option = AsyncMock()

    page.locator = MagicMock(return_value=mock_locator)
    for name in [
        "get_by_role", "get_by_label", "get_by_placeholder", "get_by_text",
        "get_by_test_id", "get_by_alt_text", "get_by_title",
    ]:
        setattr(page, name, MagicMock(return_value=mock_locator))

    return page


def _input(params: dict) -> ToolInput:
    return ToolInput(trace_id="test-trace-001", params=params)


# ── 에러 경로 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_page_raises(tool):
    """page 누락 시 ToolExecutionError."""
    with pytest.raises(ToolExecutionError):
        await tool.run(_input({"action_mapping": {"steps": [{"action": "click"}]}}))


@pytest.mark.asyncio
async def test_empty_steps_raises(tool):
    """steps 비어있을 때 ToolExecutionError."""
    with pytest.raises(ToolExecutionError):
        await tool.run(_input({"page": _mock_page(), "action_mapping": {"steps": []}}))


# ── action 6종 분기 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_navigate_relative_url_uses_target_url(tool):
    """navigate 의 relative path 가 target_url 과 결합."""
    page = _mock_page()
    out = await tool.run(_input({
        "page": page, "tc_id": "TC-1", "target_url": "http://localhost:3000",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "navigate", "value": "/login"}
        ]},
    }))
    page.goto.assert_awaited_once_with("http://localhost:3000/login")
    assert out.result["ui_result"]["status"] == "pass"


@pytest.mark.asyncio
async def test_navigate_absolute_url_kept(tool):
    """절대 URL 은 그대로 사용."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1", "target_url": "http://localhost:3000",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "navigate", "value": "https://example.com/a"}
        ]},
    }))
    page.goto.assert_awaited_once_with("https://example.com/a")


@pytest.mark.asyncio
async def test_wait_with_digit_uses_timeout(tool):
    """wait value 가 숫자면 wait_for_timeout, 아니면 networkidle."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "wait", "value": "1500"},
            {"step_no": 2, "action": "wait", "value": None},
        ]},
    }))
    page.wait_for_timeout.assert_awaited_once_with(1500)
    page.wait_for_load_state.assert_awaited_once_with("networkidle")


@pytest.mark.asyncio
async def test_fill_click_select_actions(tool):
    """fill / click / select 가 locator 의 해당 메서드 호출."""
    page = _mock_page()
    locator = page.locator.return_value
    out = await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "fill", "selector": "#email",
             "selector_type": "css", "value": "a@b.c"},
            {"step_no": 2, "action": "click", "selector": "#submit",
             "selector_type": "css"},
            {"step_no": 3, "action": "select", "selector": "#plan",
             "selector_type": "css", "value": "pro"},
        ]},
    }))
    locator.fill.assert_awaited_once_with("a@b.c")
    locator.click.assert_awaited_once()
    locator.select_option.assert_awaited_once_with("pro")
    assert out.result["ui_result"]["status"] == "pass"


@pytest.mark.asyncio
async def test_unsupported_action_fails_and_skips(tool):
    """지원 안 하는 action → fail + 후속 skip."""
    page = _mock_page()
    out = await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "teleport"},
            {"step_no": 2, "action": "click", "selector": "#x", "selector_type": "css"},
        ]},
    }))
    ui = out.result["ui_result"]
    assert ui["status"] == "fail"
    assert ui["steps"][0]["status"] == "fail"
    assert ui["steps"][1]["status"] == "skip"


# ── selector_type 9종 매핑 ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_type,attr", [
    ("role", "get_by_role"),
    ("label", "get_by_label"),
    ("placeholder", "get_by_placeholder"),
    ("text", "get_by_text"),
    ("testid", "get_by_test_id"),
    ("alttext", "get_by_alt_text"),
    ("title", "get_by_title"),
])
async def test_selector_type_get_by_methods(tool, selector_type, attr):
    """7개 get_by_* 메서드 매핑."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "click",
             "selector": "X", "selector_type": selector_type},
        ]},
    }))
    getattr(page, attr).assert_called_once_with("X")


@pytest.mark.asyncio
async def test_selector_type_css(tool):
    """css 는 page.locator() 호출."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "click", "selector": "#btn", "selector_type": "css"},
        ]},
    }))
    page.locator.assert_called_once_with("#btn")


@pytest.mark.asyncio
async def test_selector_type_xpath_prefixed(tool):
    """xpath 는 'xpath=' prefix 자동 추가."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "click",
             "selector": "//button", "selector_type": "xpath"},
        ]},
    }))
    page.locator.assert_called_once_with("xpath=//button")


# ── 스크린샷 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_screenshot_saved_when_dir_provided(tool, tmp_path: Path):
    """screenshot_dir 지정 시 page.screenshot 호출 + 결과에 경로 포함."""
    page = _mock_page()
    out = await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "screenshot_dir": str(tmp_path),
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "wait", "value": "10"},
        ]},
    }))
    expected = str(tmp_path / "step_01.png")
    page.screenshot.assert_awaited_once_with(path=expected)
    assert out.result["ui_result"]["steps"][0]["screenshot_path"] == expected


@pytest.mark.asyncio
async def test_no_screenshot_when_dir_omitted(tool):
    """screenshot_dir 미지정 시 page.screenshot 호출 X."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "wait", "value": "10"},
        ]},
    }))
    page.screenshot.assert_not_awaited()


# ── assert (expect 모듈 사용) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assert_with_expected_calls_to_have_text(tool):
    """assert + expected → expect(locator).to_have_text(expected)."""
    page = _mock_page()
    locator = page.locator.return_value

    fake_assertion = MagicMock()
    fake_assertion.to_have_text = AsyncMock()

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion) as exp:
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert", "selector": "#msg",
                 "selector_type": "css", "expected": "OK"},
            ]},
        }))
    exp.assert_called_once_with(locator)
    fake_assertion.to_have_text.assert_awaited_once_with("OK")
