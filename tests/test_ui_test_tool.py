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
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
    # 이슈 #111: chain timeout 인자 추가됨 — 인자 검증은 첫 인자만, timeout 은 ANY
    locator.fill.assert_awaited_once_with("a@b.c", timeout=ANY)
    locator.click.assert_awaited_once()
    locator.select_option.assert_awaited_once_with("pro", timeout=ANY)
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


# ── assert 8종 (expect 모듈 사용) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assert_text_calls_to_have_text(tool):
    """assert_text + expected → expect(locator).to_have_text(expected)."""
    page = _mock_page()
    locator = page.locator.return_value

    fake_assertion = MagicMock()
    fake_assertion.to_have_text = AsyncMock()

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion) as exp:
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_text", "selector": "#msg",
                 "selector_type": "css", "expected": "OK"},
            ]},
        }))
    exp.assert_called_once_with(locator)
    fake_assertion.to_have_text.assert_awaited_once_with("OK", timeout=ANY)


@pytest.mark.asyncio
async def test_assert_alias_uses_to_be_visible(tool):
    """assert (별칭) → expect(locator).to_be_visible() — selector 단순 존재 검증."""
    page = _mock_page()
    locator = page.locator.return_value

    fake_assertion = MagicMock()
    fake_assertion.to_be_visible = AsyncMock()

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert", "selector": "#msg",
                 "selector_type": "css", "expected": "OK"},
            ]},
        }))
    fake_assertion.to_be_visible.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expect_method,arg", [
    ("assert_visible", "to_be_visible", None),
    ("assert_hidden", "to_be_hidden", None),
    ("assert_enabled", "to_be_enabled", None),
    ("assert_disabled", "to_be_disabled", None),
    ("assert_value", "to_have_value", "abc"),
])
async def test_assert_simple_variants(tool, action, expect_method, arg):
    """assert_visible/hidden/enabled/disabled/value 5종."""
    page = _mock_page()
    fake_assertion = MagicMock()
    setattr(fake_assertion, expect_method, AsyncMock())

    step = {"step_no": 1, "action": action, "selector": "#x", "selector_type": "css"}
    if arg is not None:
        step["expected"] = arg

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [step]},
        }))
    method = getattr(fake_assertion, expect_method)
    if arg is None:
        method.assert_awaited_once()
    else:
        method.assert_awaited_once_with(arg, timeout=ANY)


@pytest.mark.asyncio
async def test_assert_count_converts_to_int(tool):
    """assert_count expected 문자열 '3' → to_have_count(3)."""
    page = _mock_page()
    fake_assertion = MagicMock()
    fake_assertion.to_have_count = AsyncMock()

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_count", "selector": ".item",
                 "selector_type": "css", "expected": "3"},
            ]},
        }))
    fake_assertion.to_have_count.assert_awaited_once_with(3, timeout=ANY)


@pytest.mark.asyncio
async def test_assert_count_fallback_to_zero_on_bad_value(tool):
    """assert_count expected 가 비숫자/None → fallback 0."""
    page = _mock_page()
    fake_assertion = MagicMock()
    fake_assertion.to_have_count = AsyncMock()

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_count", "selector": ".item",
                 "selector_type": "css", "expected": "abc"},
            ]},
        }))
    fake_assertion.to_have_count.assert_awaited_once_with(0, timeout=ANY)


@pytest.mark.asyncio
async def test_assert_url_uses_page(tool):
    """assert_url → expect(page).to_have_url(expected) — selector 불필요."""
    page = _mock_page()
    fake_assertion = MagicMock()
    fake_assertion.to_have_url = AsyncMock()

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion) as exp:
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_url",
                 "selector": None, "selector_type": None,
                 "expected": "/dashboard"},
            ]},
        }))
    exp.assert_called_once_with(page)
    fake_assertion.to_have_url.assert_awaited_once_with("/dashboard")


# ── 신규 page-level action ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_go_back_forward(tool):
    """reload / go_back / go_forward — selector null OK."""
    page = _mock_page()
    page.reload = AsyncMock()
    page.go_back = AsyncMock()
    page.go_forward = AsyncMock()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "reload"},
            {"step_no": 2, "action": "go_back"},
            {"step_no": 3, "action": "go_forward"},
        ]},
    }))
    page.reload.assert_awaited_once()
    page.go_back.assert_awaited_once()
    page.go_forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_url(tool):
    page = _mock_page()
    page.wait_for_url = AsyncMock()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "wait_for_url", "value": "**/dashboard"},
        ]},
    }))
    page.wait_for_url.assert_awaited_once_with("**/dashboard")


@pytest.mark.asyncio
async def test_wait_for_load_state_default_networkidle(tool):
    """value 미지정 / 잘못된 값 → fallback 'networkidle'."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "wait_for_load_state"},
            {"step_no": 2, "action": "wait_for_load_state", "value": "load"},
            {"step_no": 3, "action": "wait_for_load_state", "value": "bogus"},
        ]},
    }))
    calls = [c.args[0] for c in page.wait_for_load_state.await_args_list]
    assert calls == ["networkidle", "load", "networkidle"]


@pytest.mark.asyncio
async def test_wait_for_response_graceful_to_networkidle(tool):
    """이슈 #138: Playwright Page 에 wait_for_response 가 없으므로
    networkidle 대기로 graceful 변환. value (URL 패턴) 는 무시됨."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "wait_for_response", "value": "**/api/login"},
        ]},
    }))
    # value 무시하고 networkidle 호출. (_mock_page 의 default networkidle 호출 + step 1)
    calls = [c.args[0] for c in page.wait_for_load_state.await_args_list]
    assert "networkidle" in calls


# ── 신규 DOM action ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dom_extended_actions(tool):
    """clear/dblclick/hover/check/uncheck/press/upload — locator 메서드 호출 검증."""
    page = _mock_page()
    locator = page.locator.return_value
    locator.clear = AsyncMock()
    locator.dblclick = AsyncMock()
    locator.hover = AsyncMock()
    locator.check = AsyncMock()
    locator.uncheck = AsyncMock()
    locator.press = AsyncMock()
    locator.set_input_files = AsyncMock()

    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "clear", "selector": "#x", "selector_type": "css"},
            {"step_no": 2, "action": "dblclick", "selector": "#x", "selector_type": "css"},
            {"step_no": 3, "action": "hover", "selector": "#x", "selector_type": "css"},
            {"step_no": 4, "action": "check", "selector": "#x", "selector_type": "css"},
            {"step_no": 5, "action": "uncheck", "selector": "#x", "selector_type": "css"},
            {"step_no": 6, "action": "press", "selector": "#x",
             "selector_type": "css", "value": "Enter"},
            {"step_no": 7, "action": "upload", "selector": "#x",
             "selector_type": "css", "value": "/tmp/a.png"},
        ]},
    }))
    locator.clear.assert_awaited_once()
    locator.dblclick.assert_awaited_once()
    locator.hover.assert_awaited_once()
    locator.check.assert_awaited_once()
    locator.uncheck.assert_awaited_once()
    locator.press.assert_awaited_once_with("Enter", timeout=ANY)
    locator.set_input_files.assert_awaited_once_with("/tmp/a.png", timeout=ANY)


@pytest.mark.asyncio
async def test_press_fallback_to_enter(tool):
    """press value 누락 → 1-step fallback 'Enter'."""
    page = _mock_page()
    locator = page.locator.return_value
    locator.press = AsyncMock()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "press", "selector": "#x", "selector_type": "css"},
        ]},
    }))
    locator.press.assert_awaited_once_with("Enter", timeout=ANY)


# ── selector None fallback (DOM action) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_selector_none_dom_action_falls_back_to_text(tool):
    """selector None + DOM action → expected/value/action 으로 text fallback."""
    page = _mock_page()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "click",
             "selector": None, "selector_type": None,
             "expected": "가입 완료"},
        ]},
    }))
    # _build_locator 가 page.get_by_text("가입 완료") 호출했어야
    page.get_by_text.assert_called_once_with("가입 완료")


# ── 에러 카테고리 prefix ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_code_prefix_for_unsupported(tool):
    """unsupported action → error 가 TOOL_UI_UNSUPPORTED_ACTION prefix."""
    page = _mock_page()
    out = await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "teleport"},
        ]},
    }))
    err = out.result["ui_result"]["steps"][0]["error"]
    assert err is not None and err.startswith("TOOL_UI_UNSUPPORTED_ACTION")


@pytest.mark.asyncio
async def test_error_code_prefix_for_assertion_fail(tool):
    """AssertionError → error 가 TOOL_UI_ASSERTION_FAIL prefix."""
    page = _mock_page()
    fake_assertion = MagicMock()
    fake_assertion.to_be_visible = AsyncMock(side_effect=AssertionError("not visible"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        out = await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_visible",
                 "selector": "#x", "selector_type": "css"},
            ]},
        }))
    err = out.result["ui_result"]["steps"][0]["error"]
    assert err is not None and err.startswith("TOOL_UI_ASSERTION_FAIL")


# ── 이슈 #147: TOOL_UI_* 에러 코드 4종 신설 검증 ──────────────────────────


def test_tool_ui_new_error_codes_defined_in_enum():
    """이슈 #147: errors.py 에 4종 (TARGET_UNREACHABLE / INVALID_SELECTOR /
    DOM_SCAN_FALLBACK / AUTO_NAVIGATE) 정의 검증.

    PR #124 / #134 본문 약속 ↔ errors.py 갭 해소 + 본인 메모리
    `project_qapilot_ui_test_tool_design.md` 의 11종 표 정합.
    """
    from qapilot.shared.errors import ErrorCode
    assert ErrorCode.TOOL_UI_TARGET_UNREACHABLE == "TOOL_UI_TARGET_UNREACHABLE"
    assert ErrorCode.TOOL_UI_INVALID_SELECTOR == "TOOL_UI_INVALID_SELECTOR"
    assert ErrorCode.TOOL_UI_DOM_SCAN_FALLBACK == "TOOL_UI_DOM_SCAN_FALLBACK"
    assert ErrorCode.TOOL_UI_AUTO_NAVIGATE == "TOOL_UI_AUTO_NAVIGATE"


@pytest.mark.asyncio
async def test_role_name_selector_emits_invalid_selector_code(tool):
    """이슈 #147: `role:name` 형식 selector 처리 시 TOOL_UI_INVALID_SELECTOR
    메타 코드 발행 (PR #134 fail-safe 의 분류 신호)."""
    page = _mock_page()
    locator = page.get_by_role.return_value
    locator.click = AsyncMock()
    tool.logger = MagicMock()
    await tool.run(_input({
        "page": page, "tc_id": "TC-1",
        "action_mapping": {"steps": [
            {"step_no": 1, "action": "click", "selector": "button:로그인", "selector_type": "role"},
        ]},
    }))
    # role:name 진입 시 warning 로그 + code=TOOL_UI_INVALID_SELECTOR
    from qapilot.shared.errors import ErrorCode
    warning_calls = [c for c in tool.logger.warning.call_args_list
                     if c.args and c.args[0] == "ui_invalid_selector_fallback"]
    assert len(warning_calls) == 1
    assert warning_calls[0].kwargs.get("code") == ErrorCode.TOOL_UI_INVALID_SELECTOR


# ── 이슈 #141 D fail-safe: assert_text fuzzy match graceful ─────────────


@pytest.mark.asyncio
async def test_assert_text_fuzzy_match_above_threshold_recovers(tool):
    """이슈 #141 D fail-safe: assert_text strict 실패 시 fuzzy match (difflib ratio)
    임계값 0.6 이상이면 graceful pass. LLM 환각 expected ("로그인 완료") vs 실제
    DOM ("로그인이 완료되었습니다") 같은 의미 유사 케이스 회복.
    """
    page = _mock_page()
    locator = page.locator.return_value
    locator.inner_text = AsyncMock(return_value="로그인이 완료되었습니다.")
    tool.logger = MagicMock()

    fake_assertion = MagicMock()
    fake_assertion.to_have_text = AsyncMock(side_effect=AssertionError("text not matched"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_text", "selector": "#msg",
                 "selector_type": "css", "expected": "로그인 완료"},
            ]},
        }))

    # fuzzy match 적중 → ui_assert_text_fuzzy_match info 로그 발행 (FALLBACK_USED 코드)
    from qapilot.shared.errors import ErrorCode
    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_assert_text_fuzzy_match"]
    assert len(info_calls) == 1
    assert info_calls[0].kwargs.get("code") == ErrorCode.TOOL_UI_FALLBACK_USED


@pytest.mark.asyncio
async def test_assert_text_fuzzy_match_below_threshold_raises(tool):
    """이슈 #141 D fail-safe: fuzzy ratio 임계값 미달 → strict 에러 그대로 raise."""
    page = _mock_page()
    locator = page.locator.return_value
    locator.inner_text = AsyncMock(return_value="완전히 다른 텍스트")
    tool.logger = MagicMock()

    fake_assertion = MagicMock()
    fake_assertion.to_have_text = AsyncMock(side_effect=AssertionError("text not matched"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        # 임계값 미달 — fail 그대로 (tool 의 run 은 graceful 흡수 후 step status=fail)
        result = await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_text", "selector": "#msg",
                 "selector_type": "css", "expected": "환영합니다"},
            ]},
        }))
    # fuzzy match info 로그 발행 X
    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_assert_text_fuzzy_match"]
    assert len(info_calls) == 0
    # step 결과 fail
    ui_result = result.result["ui_result"]
    assert ui_result["status"] == "fail"


@pytest.mark.asyncio
async def test_assert_text_fuzzy_match_substring_bonus_breaks_threshold(tool):
    """이슈 #141 D fail-safe: expected 가 actual 의 substring 이면 0.2 가산점으로 임계값 통과."""
    page = _mock_page()
    locator = page.locator.return_value
    # expected="환영" / actual="환영합니다 김주환님 — 메인 페이지" — substring 매칭
    locator.inner_text = AsyncMock(return_value="환영합니다 김주환님 — 메인 페이지")
    tool.logger = MagicMock()

    fake_assertion = MagicMock()
    fake_assertion.to_have_text = AsyncMock(side_effect=AssertionError("strict mismatch"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_text", "selector": "#msg",
                 "selector_type": "css", "expected": "환영"},
            ]},
        }))

    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_assert_text_fuzzy_match"]
    assert len(info_calls) == 1


# ── 이슈 #141 D fail-safe (확장): assert/assert_visible fuzzy match ─────


@pytest.mark.asyncio
async def test_assert_visible_substring_match_in_page_text_recovers(tool):
    """이슈 #141 D fail-safe 확장: strict to_be_visible 실패 시 selector 텍스트가
    페이지 textContent 안에 substring 으로 존재하면 graceful pass.

    LLM 환각 selector="환영" 와 실제 페이지 textContent="환영합니다 김주환님" 매칭.
    """
    page = _mock_page()
    locator = page.locator.return_value
    locator.evaluate = AsyncMock(return_value="환영합니다 김주환님\n홈 페이지")
    tool.logger = MagicMock()

    fake_assertion = MagicMock()
    fake_assertion.to_be_visible = AsyncMock(side_effect=AssertionError("element(s) not found"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert", "selector": "환영",
                 "selector_type": "text"},
            ]},
        }))

    from qapilot.shared.errors import ErrorCode
    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_assert_visible_fuzzy_match"]
    assert len(info_calls) == 1
    assert info_calls[0].kwargs.get("match_type") == "substring"
    assert info_calls[0].kwargs.get("code") == ErrorCode.TOOL_UI_FALLBACK_USED


@pytest.mark.asyncio
async def test_assert_visible_fuzzy_match_above_threshold_recovers(tool):
    """이슈 #141 D fail-safe 확장: 페이지 줄 단위 fuzzy ratio 0.6+ 적중 시 pass.

    LLM 환각 selector="로그인이 완료되었습니다" vs 페이지 줄 "로그인 완료 — 환영합니다" — fuzzy 일치.
    """
    page = _mock_page()
    locator = page.locator.return_value
    locator.evaluate = AsyncMock(return_value="홈\n로그인 완료 — 환영합니다\n메뉴")
    tool.logger = MagicMock()

    fake_assertion = MagicMock()
    fake_assertion.to_be_visible = AsyncMock(side_effect=AssertionError("not found"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert_visible", "selector": "로그인 완료되었습니다",
                 "selector_type": "text"},
            ]},
        }))

    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_assert_visible_fuzzy_match"]
    assert len(info_calls) == 1
    assert info_calls[0].kwargs.get("match_type") == "fuzzy"


@pytest.mark.asyncio
async def test_assert_visible_no_match_raises(tool):
    """이슈 #141 D fail-safe 확장: 페이지 어디서도 매칭 안 되면 strict fail 그대로."""
    page = _mock_page()
    locator = page.locator.return_value
    locator.evaluate = AsyncMock(return_value="완전히 다른 내용\n다른 줄")
    tool.logger = MagicMock()

    fake_assertion = MagicMock()
    fake_assertion.to_be_visible = AsyncMock(side_effect=AssertionError("not found"))

    with patch("qapilot.tools.ui_test_tool.expect", return_value=fake_assertion):
        result = await tool.run(_input({
            "page": page, "tc_id": "TC-1",
            "action_mapping": {"steps": [
                {"step_no": 1, "action": "assert", "selector": "환영합니다",
                 "selector_type": "text"},
            ]},
        }))

    info_calls = [c for c in tool.logger.info.call_args_list
                  if c.args and c.args[0] == "ui_assert_visible_fuzzy_match"]
    assert len(info_calls) == 0
    ui_result = result.result["ui_result"]
    assert ui_result["status"] == "fail"
