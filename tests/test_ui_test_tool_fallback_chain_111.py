"""이슈 #111 — UITestTool selector_type 별 의미적 retry chain 검증.

배경: e2e 첫 완주 (trace `721a4e4f`) 의 UI 100% fail 원인 = ActionMapper LLM 의
selector 추론과 SUT 실제 DOM 의 mismatch. spec §4.5.4 의 `1-step fallback` 을
`selector_type 별 의미적 chain` 으로 확장해 적중률 회복.

상세 배경: docs/frontend-dom-scan-gap.md
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PWTimeoutError

from qapilot.tools.ui_test_tool import (
    UITestTool,
    _CHAIN_FALLBACK_TIMEOUT_MS,
    _CHAIN_PRIMARY_TIMEOUT_MS,
)


def _mock_page_with_locators() -> MagicMock:
    """page.get_by_*, page.locator 각각 별도 MagicMock 반환. 각 호출이 distinct mock 받아 chain 의 각 step 검증 가능."""
    page = MagicMock()
    # 각 get_by_* 가 호출될 때마다 새 mock 반환 (chain 의 각 entry 가 distinct locator 인지 보장)
    for name in ("get_by_text", "get_by_label", "get_by_placeholder",
                 "get_by_test_id", "get_by_role", "get_by_alt_text", "get_by_title", "locator"):
        loc = MagicMock()
        loc.fill = AsyncMock()
        loc.click = AsyncMock()
        loc.press = AsyncMock()
        loc.clear = AsyncMock()
        loc.select_option = AsyncMock()
        loc.check = AsyncMock()
        setattr(page, name, MagicMock(return_value=loc))
    return page


def _make_tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool


# ── _build_locator_chain — selector_type 별 chain 정의 정확성 ────────────────


def test_chain_text_type_has_4_entries():
    """text 타입: get_by_text → get_by_label → get_by_placeholder → get_by_test_id."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "이메일", "selector_type": "text", "action": "click",
    })
    labels = [c[0] for c in chain]
    assert labels == ["get_by_text", "get_by_label", "get_by_placeholder", "get_by_test_id"]
    page.get_by_text.assert_called_once_with("이메일")
    page.get_by_label.assert_called_once_with("이메일")
    page.get_by_placeholder.assert_called_once_with("이메일")
    page.get_by_test_id.assert_called_once_with("이메일")


def test_chain_label_type_has_3_entries():
    """label 타입: label → text → placeholder."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "이메일", "selector_type": "label", "action": "fill",
    })
    assert [c[0] for c in chain] == ["get_by_label", "get_by_text", "get_by_placeholder"]


def test_chain_placeholder_type_has_3_entries():
    """placeholder 타입: placeholder → label → text."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "이메일", "selector_type": "placeholder", "action": "fill",
    })
    assert [c[0] for c in chain] == ["get_by_placeholder", "get_by_label", "get_by_text"]


def test_chain_testid_type_has_3_entries():
    """testid 타입: get_by_test_id + data-testid/data-test-id css 직접 시도."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "email", "selector_type": "testid", "action": "fill",
    })
    labels = [c[0] for c in chain]
    assert labels == ["get_by_test_id", "locator[data-testid]", "locator[data-test-id]"]
    # css fallback 호출 인자 검증
    page.locator.assert_any_call('[data-testid="email"]')
    page.locator.assert_any_call('[data-test-id="email"]')


def test_chain_role_type_falls_back_to_text():
    """role 타입: role → text."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "button", "selector_type": "role", "action": "click",
    })
    assert [c[0] for c in chain] == ["get_by_role", "get_by_text"]


def test_chain_css_single_attempt():
    """css 는 정확한 selector 가정 — 단일 시도, chain 적용 안 함."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "#login-btn", "selector_type": "css", "action": "click",
    })
    assert len(chain) == 1
    assert chain[0][0] == "locator"
    page.locator.assert_called_once_with("#login-btn")


def test_chain_xpath_single_attempt_with_prefix():
    """xpath 는 단일 시도. prefix 자동 부착."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": "//button[@id='ok']", "selector_type": "xpath", "action": "click",
    })
    assert len(chain) == 1
    page.locator.assert_called_once_with("xpath=//button[@id='ok']")


def test_chain_selector_none_falls_back_with_3_entries():
    """selector=None: expected/value/action 으로 text→placeholder→label fallback 3개."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    chain = tool._build_locator_chain(page, {
        "selector": None, "selector_type": None, "action": "click",
        "expected": "가입 완료",
    })
    labels = [c[0] for c in chain]
    assert labels == ["get_by_text", "get_by_placeholder", "get_by_label"]
    page.get_by_text.assert_called_once_with("가입 완료")
    page.get_by_placeholder.assert_called_once_with("가입 완료")
    page.get_by_label.assert_called_once_with("가입 완료")


# ── _run_dom_action — chain 순회 + 부분 graceful ─────────────────────────────


@pytest.mark.asyncio
async def test_first_attempt_success_no_retry_no_warning():
    """1차 시도 성공 → 2~N차 미발화, retry log 없음."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    step = {"selector": "이메일", "selector_type": "text", "action": "click"}

    await tool._run_dom_action(page, "click", step)

    # chain 의 4개 locator 모두 빌드되었지만, 1차 click 만 await
    first_locator = page.get_by_text.return_value
    first_locator.click.assert_awaited_once_with(timeout=_CHAIN_PRIMARY_TIMEOUT_MS)

    others = [
        page.get_by_label.return_value.click,
        page.get_by_placeholder.return_value.click,
        page.get_by_test_id.return_value.click,
    ]
    for other in others:
        other.assert_not_called()

    # retry 로그 미발화
    retry_calls = [c for c in tool.logger.warning.call_args_list if c.args and c.args[0] == "ui_fallback_chain_retry"]
    assert retry_calls == []


@pytest.mark.asyncio
async def test_first_fails_second_succeeds_emits_success_log():
    """1차 PWTimeoutError → 2차 적중 → success log + retry warning 1건."""
    tool = _make_tool()
    page = _mock_page_with_locators()

    # 1차 (get_by_text) click 은 timeout, 2차 (get_by_label) click 은 성공
    page.get_by_text.return_value.click = AsyncMock(side_effect=PWTimeoutError("Locator timeout"))
    page.get_by_label.return_value.click = AsyncMock()

    step = {"selector": "이메일", "selector_type": "text", "action": "click"}
    await tool._run_dom_action(page, "click", step)

    # 1차 timeout, 2차 success
    page.get_by_text.return_value.click.assert_awaited_once_with(timeout=_CHAIN_PRIMARY_TIMEOUT_MS)
    page.get_by_label.return_value.click.assert_awaited_once_with(timeout=_CHAIN_FALLBACK_TIMEOUT_MS)

    # 3차/4차 미발화
    page.get_by_placeholder.return_value.click.assert_not_called()
    page.get_by_test_id.return_value.click.assert_not_called()

    # 로깅 검증
    retry_calls = [c for c in tool.logger.warning.call_args_list if c.args and c.args[0] == "ui_fallback_chain_retry"]
    success_calls = [c for c in tool.logger.info.call_args_list if c.args and c.args[0] == "ui_fallback_chain_success"]
    assert len(retry_calls) == 1
    assert retry_calls[0].kwargs["tried"] == "get_by_text"
    assert retry_calls[0].kwargs["next"] == "get_by_label"
    assert len(success_calls) == 1
    assert success_calls[0].kwargs["matched_at"] == "get_by_label"
    assert success_calls[0].kwargs["attempt"] == 2


@pytest.mark.asyncio
async def test_all_chain_steps_fail_raises_last_error():
    """모든 chain step 실패 → 마지막 timeout 에러 그대로 raise."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    last_err = PWTimeoutError("마지막 timeout")
    page.get_by_text.return_value.click = AsyncMock(side_effect=PWTimeoutError("1"))
    page.get_by_label.return_value.click = AsyncMock(side_effect=PWTimeoutError("2"))
    page.get_by_placeholder.return_value.click = AsyncMock(side_effect=PWTimeoutError("3"))
    page.get_by_test_id.return_value.click = AsyncMock(side_effect=last_err)

    step = {"selector": "이메일", "selector_type": "text", "action": "click"}
    with pytest.raises(PWTimeoutError) as exc:
        await tool._run_dom_action(page, "click", step)
    assert "마지막 timeout" in str(exc.value)


@pytest.mark.asyncio
async def test_chain_short_timeout_for_fallback_steps():
    """2차+ 시도는 _CHAIN_FALLBACK_TIMEOUT_MS, 1차는 _CHAIN_PRIMARY_TIMEOUT_MS."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    page.get_by_label.return_value.click = AsyncMock(side_effect=PWTimeoutError("1차 fail"))
    page.get_by_text.return_value.click = AsyncMock(side_effect=PWTimeoutError("2차 fail"))
    page.get_by_placeholder.return_value.click = AsyncMock()  # 3차 success

    step = {"selector": "이메일", "selector_type": "label", "action": "click"}
    await tool._run_dom_action(page, "click", step)

    # 1차 = primary timeout
    page.get_by_label.return_value.click.assert_awaited_once_with(timeout=_CHAIN_PRIMARY_TIMEOUT_MS)
    # 2차/3차 = fallback timeout (각각 호출 1회)
    page.get_by_text.return_value.click.assert_awaited_once_with(timeout=_CHAIN_FALLBACK_TIMEOUT_MS)
    page.get_by_placeholder.return_value.click.assert_awaited_once_with(timeout=_CHAIN_FALLBACK_TIMEOUT_MS)


@pytest.mark.asyncio
async def test_chain_assertion_error_also_retries():
    """expect().to_have_text() 같은 assertion fail (AssertionError) 도 chain 진행."""
    tool = _make_tool()
    page = _mock_page_with_locators()

    # mock expect — to_be_visible 가 AssertionError 시뮬레이션
    with patch("qapilot.tools.ui_test_tool.expect") as mexpect:
        first_exp = MagicMock()
        first_exp.to_be_visible = AsyncMock(side_effect=AssertionError("element not visible"))
        second_exp = MagicMock()
        second_exp.to_be_visible = AsyncMock()  # 2차 success
        third_exp = MagicMock()
        third_exp.to_be_visible = AsyncMock()
        fourth_exp = MagicMock()
        fourth_exp.to_be_visible = AsyncMock()
        mexpect.side_effect = [first_exp, second_exp, third_exp, fourth_exp]

        step = {"selector": "버튼", "selector_type": "text", "action": "assert_visible"}
        await tool._run_dom_action(page, "assert_visible", step)

    first_exp.to_be_visible.assert_awaited_once()
    second_exp.to_be_visible.assert_awaited_once()


@pytest.mark.asyncio
async def test_chain_does_not_apply_to_css_single_step():
    """css selector 는 chain 적용 안 함 — fail 시 그대로 raise (chain 의미 없음)."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    page.locator.return_value.click = AsyncMock(side_effect=PWTimeoutError("css fail"))

    step = {"selector": "#login-btn", "selector_type": "css", "action": "click"}
    with pytest.raises(PWTimeoutError):
        await tool._run_dom_action(page, "click", step)

    # locator 1회만 호출
    page.locator.assert_called_once_with("#login-btn")
    page.locator.return_value.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_execution_error_does_not_retry():
    """ToolExecutionError (예: upload value 누락) 는 chain 의미 없음 — 즉시 raise."""
    tool = _make_tool()
    page = _mock_page_with_locators()
    # _apply_action 에서 upload value 누락 시 raise

    step = {"selector": "input[type=file]", "selector_type": "css", "action": "upload"}
    # value 없음 → ToolExecutionError
    from qapilot.shared.errors import ToolExecutionError
    with pytest.raises(ToolExecutionError):
        await tool._run_dom_action(page, "upload", step)
