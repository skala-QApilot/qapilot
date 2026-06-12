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



def _make_tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool


# ── _build_locator_chain — selector_type 별 chain 정의 정확성 ────────────────


def test_chain_text_type_has_4_entries(mock_page_with_distinct_locators):
    """text 타입: get_by_text → get_by_label → get_by_placeholder → get_by_test_id."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "이메일", "selector_type": "text", "action": "click",
    })
    labels = [c[0] for c in chain]
    assert labels == ["get_by_text", "get_by_label", "get_by_placeholder", "get_by_test_id"]
    page.get_by_text.assert_called_once_with("이메일")
    page.get_by_label.assert_called_once_with("이메일")
    page.get_by_placeholder.assert_called_once_with("이메일")
    page.get_by_test_id.assert_called_once_with("이메일")


def test_chain_label_type_has_3_entries(mock_page_with_distinct_locators):
    """label 타입: label → text → placeholder."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "이메일", "selector_type": "label", "action": "fill",
    })
    assert [c[0] for c in chain] == ["get_by_label", "get_by_text", "get_by_placeholder"]


def test_chain_placeholder_type_has_3_entries(mock_page_with_distinct_locators):
    """placeholder 타입: placeholder → label → text."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "이메일", "selector_type": "placeholder", "action": "fill",
    })
    assert [c[0] for c in chain] == ["get_by_placeholder", "get_by_label", "get_by_text"]


def test_chain_testid_type_has_3_entries(mock_page_with_distinct_locators):
    """testid 타입: get_by_test_id + data-testid/data-test-id css 직접 시도."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "email", "selector_type": "testid", "action": "fill",
    })
    labels = [c[0] for c in chain]
    assert labels == ["get_by_test_id", "locator[data-testid]", "locator[data-test-id]"]
    # css fallback 호출 인자 검증
    page.locator.assert_any_call('[data-testid="email"]')
    page.locator.assert_any_call('[data-test-id="email"]')


def test_chain_role_type_falls_back_to_text(mock_page_with_distinct_locators):
    """role 타입: role → text."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "button", "selector_type": "role", "action": "click",
    })
    assert [c[0] for c in chain] == ["get_by_role", "get_by_text"]


def test_chain_css_single_attempt(mock_page_with_distinct_locators):
    """css 는 정확한 selector 가정 — 단일 시도, chain 적용 안 함."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "#login-btn", "selector_type": "css", "action": "click",
    })
    assert len(chain) == 1
    assert chain[0][0] == "locator"
    page.locator.assert_called_once_with("#login-btn")


def test_chain_xpath_single_attempt_with_prefix(mock_page_with_distinct_locators):
    """xpath 는 단일 시도. prefix 자동 부착."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    chain = tool._build_locator_chain(page, {
        "selector": "//button[@id='ok']", "selector_type": "xpath", "action": "click",
    })
    assert len(chain) == 1
    page.locator.assert_called_once_with("xpath=//button[@id='ok']")


def test_chain_selector_none_falls_back_with_3_entries(mock_page_with_distinct_locators):
    """selector=None: expected/value/action 으로 text→placeholder→label fallback 3개."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
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
async def test_first_attempt_success_no_retry_no_warning(mock_page_with_distinct_locators):
    """1차 시도 성공 → 2~N차 미발화, retry warning 없음."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
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
async def test_first_fails_second_succeeds_emits_success_log(mock_page_with_distinct_locators):
    """1차 PWTimeoutError → 2차 적중 → success log 1건."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators

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
    success_calls = [c for c in tool.logger.info.call_args_list if c.args and c.args[0] == "ui_fallback_chain_success"]
    assert len(success_calls) == 1
    assert success_calls[0].kwargs["matched_at"] == "get_by_label"
    assert success_calls[0].kwargs["attempt"] == 2


@pytest.mark.asyncio
async def test_all_chain_steps_fail_raises_last_error(mock_page_with_distinct_locators):
    """모든 chain step 실패 → 마지막 timeout 에러 그대로 raise."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
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
async def test_chain_short_timeout_for_fallback_steps(mock_page_with_distinct_locators):
    """2차+ 시도는 _CHAIN_FALLBACK_TIMEOUT_MS, 1차는 _CHAIN_PRIMARY_TIMEOUT_MS."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
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
async def test_chain_assertion_error_also_retries(mock_page_with_distinct_locators):
    """expect().to_have_text() 같은 assertion fail (AssertionError) 도 chain 진행."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators

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
async def test_chain_does_not_apply_to_css_single_step(mock_page_with_distinct_locators):
    """css selector 는 chain 적용 안 함 — fail 시 그대로 raise (chain 의미 없음)."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.locator.return_value.click = AsyncMock(side_effect=PWTimeoutError("css fail"))

    step = {"selector": "#login-btn", "selector_type": "css", "action": "click"}
    with pytest.raises(PWTimeoutError):
        await tool._run_dom_action(page, "click", step)

    # locator 1회만 호출
    page.locator.assert_called_once_with("#login-btn")
    page.locator.return_value.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_execution_error_does_not_retry(mock_page_with_distinct_locators):
    """ToolExecutionError (예: upload value 누락) 는 chain 의미 없음 — 즉시 raise."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    # _apply_action 에서 upload value 누락 시 raise

    step = {"selector": "input[type=file]", "selector_type": "css", "action": "upload"}
    # value 없음 → ToolExecutionError
    from qapilot.shared.errors import ToolExecutionError
    with pytest.raises(ToolExecutionError):
        await tool._run_dom_action(page, "upload", step)


# ── Option B: 런타임 DOM Scan + Fuzzy Match ─────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_dom_scan_success(mock_page_with_distinct_locators):
    """모든 chain 실패 시 DOM scan 수행 후 fuzzy match로 성공."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    
    # 1차 chain(get_by_text 등)은 모두 실패
    for loc_mock in [page.get_by_text, page.get_by_label, page.get_by_placeholder, page.get_by_test_id]:
        loc_mock.return_value.fill = AsyncMock(side_effect=PWTimeoutError("chain fail"))
    
    # page.evaluate 가 DOM 스캔 결과를 반환
    page.evaluate = AsyncMock(return_value=[
        {"tag": "input", "text": "", "placeholder": "example@email.com", "label": "이메일", "testid": "email", "id": "", "name": ""}
    ])
    
    # fuzzy match 후 get_by_test_id 가 호출됨
    # 첫 번째 호출(1차 체인)은 실패하고, 두 번째 호출(DOM 스캔 후)은 성공하도록 side_effect 설정
    page.get_by_test_id.return_value.fill.side_effect = [PWTimeoutError("chain fail"), None]
    
    from qapilot.shared.errors import ErrorCode
    step = {"selector": "이메일", "selector_type": "text", "action": "fill"}
    await tool._run_dom_action(page, "fill", step)
    
    # evaluate 호출 확인
    page.evaluate.assert_awaited_once()
    
    # fallback_locator 의 fill 호출 확인 (두 번 호출됨)
    assert page.get_by_test_id.return_value.fill.call_count == 2
    tool.logger.info.assert_called_with(
        "ui_fallback_dom_scan_success",
        action="fill",
        selector_type="text",
        selector="이메일",
        code=ErrorCode.TOOL_UI_DOM_SCAN_FALLBACK,
    )


@pytest.mark.asyncio
async def test_fallback_dom_scan_failed(mock_page_with_distinct_locators):
    """DOM scan 결과에서도 match를 찾지 못하면 마지막 에러 발생."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    
    for loc_mock in [page.get_by_text, page.get_by_label, page.get_by_placeholder, page.get_by_test_id]:
        loc_mock.return_value.click = AsyncMock(side_effect=PWTimeoutError("chain fail"))
    
    # DOM 스캔 결과 무관한 요소만 있음
    page.evaluate = AsyncMock(return_value=[
        {"tag": "button", "text": "취소", "placeholder": "", "label": "", "testid": "", "id": "", "name": ""}
    ])
    
    step = {"selector": "저장하기", "selector_type": "text", "action": "click"}
    with pytest.raises(PWTimeoutError):
        await tool._run_dom_action(page, "click", step)

    page.evaluate.assert_awaited_once()


# ── 이슈 #119: 옵션 B edge case + 우선순위 + 일관성 검증 ────────────────────


@pytest.mark.asyncio
async def test_fallback_dom_scan_returns_none_for_empty_selector(mock_page_with_distinct_locators):
    """selector / expected / value / action 모두 비어있으면 DOM scan 자체 안 함 → None."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock()

    step = {"selector": "", "selector_type": "text", "action": ""}
    result = await tool._fallback_dom_scan(page, step)

    assert result is None
    page.evaluate.assert_not_called()  # 스캔 자체 발화 안 함


@pytest.mark.asyncio
async def test_fallback_dom_scan_returns_none_when_evaluate_raises(mock_page_with_distinct_locators):
    """page.evaluate 가 raise → debug log + None 반환 (호출자에 전파 X)."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock(side_effect=RuntimeError("page closed"))

    step = {"selector": "이메일", "selector_type": "text", "action": "fill"}
    result = await tool._fallback_dom_scan(page, step)

    assert result is None
    tool.logger.debug.assert_called_once()
    assert tool.logger.debug.call_args.args[0] == "dom_scan_evaluate_failed"


@pytest.mark.asyncio
async def test_fallback_dom_scan_returns_none_below_threshold(mock_page_with_distinct_locators):
    """모든 후보의 score 가 임계값 0.6 미만 → None."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    # target='이메일' vs '주문하기' — SequenceMatcher ratio ~0 + 포함관계 없음
    page.evaluate = AsyncMock(return_value=[
        {"tag": "button", "text": "주문하기", "placeholder": "", "label": "",
         "testid": "", "id": "", "name": ""}
    ])

    step = {"selector": "이메일", "selector_type": "text", "action": "fill"}
    result = await tool._fallback_dom_scan(page, step)
    assert result is None


@pytest.mark.asyncio
async def test_fallback_dom_scan_returns_none_when_dom_empty(mock_page_with_distinct_locators):
    """DOM 스캔 결과 빈 list → None."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock(return_value=[])

    step = {"selector": "이메일", "selector_type": "text", "action": "fill"}
    result = await tool._fallback_dom_scan(page, step)
    assert result is None


@pytest.mark.asyncio
async def test_fallback_dom_scan_returns_testid_first(mock_page_with_distinct_locators):
    """반환 우선순위 — 매치 element 에 testid + placeholder + text 모두 있어도 testid 우선."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock(return_value=[
        {"tag": "input",
         "text": "이메일 텍스트",
         "placeholder": "이메일 입력",
         "label": "이메일 라벨",
         "testid": "email-input",
         "id": "email",
         "name": "email"}
    ])

    step = {"selector": "이메일", "selector_type": "text", "action": "fill"}
    result = await tool._fallback_dom_scan(page, step)
    assert result is not None
    page.get_by_test_id.assert_called_once_with("email-input")
    page.get_by_placeholder.assert_not_called()
    page.get_by_text.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_dom_scan_falls_back_to_name_when_other_attrs_empty(mock_page_with_distinct_locators):
    """testid/placeholder/text/label/id 모두 빈 문자열 → name 최후 fallback."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock(return_value=[
        {"tag": "input",
         "text": "", "placeholder": "", "label": "", "testid": "", "id": "",
         "name": "username"}
    ])

    step = {"selector": "username", "selector_type": "text", "action": "fill"}
    result = await tool._fallback_dom_scan(page, step)
    assert result is not None
    page.locator.assert_called_once_with('[name="username"]')


@pytest.mark.asyncio
async def test_fallback_dom_scan_substring_match_bonus_breaks_threshold(mock_page_with_distinct_locators):
    """포함관계 가산점 (+0.2) 로 임계값 통과 — target='이메일' vs cand='이메일을 입력하세요'."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock(return_value=[
        {"tag": "input",
         "text": "", "placeholder": "이메일을 입력하세요",
         "label": "", "testid": "", "id": "", "name": ""}
    ])

    step = {"selector": "이메일", "selector_type": "text", "action": "fill"}
    result = await tool._fallback_dom_scan(page, step)
    assert result is not None
    page.get_by_placeholder.assert_called_once_with("이메일을 입력하세요")


@pytest.mark.asyncio
async def test_fallback_dom_scan_uses_expected_when_selector_none(mock_page_with_distinct_locators):
    """selector=None 일 때 expected 를 target 으로 fuzzy match."""
    tool = _make_tool()
    page = mock_page_with_distinct_locators
    page.evaluate = AsyncMock(return_value=[
        {"tag": "div", "text": "가입 완료", "placeholder": "",
         "label": "", "testid": "", "id": "", "name": ""}
    ])

    step = {"selector": None, "selector_type": None, "action": "assert_visible",
            "expected": "가입 완료"}
    result = await tool._fallback_dom_scan(page, step)
    assert result is not None
    page.get_by_text.assert_called_once_with("가입 완료")


@pytest.mark.asyncio
async def test_option_b_does_not_swallow_tool_execution_error(mock_page_with_distinct_locators):
    """이슈 #119 P2 — 옵션 B 분기에서 ToolExecutionError 는 즉시 raise (chain 의미 없음, 옵션 A 와 일관성)."""
    from qapilot.shared.errors import ErrorCode
    from qapilot.shared.errors import ToolExecutionError as TEE

    tool = _make_tool()
    page = mock_page_with_distinct_locators
    # 옵션 A chain 모두 timeout — fallback 진입
    for loc_mock in [page.get_by_text, page.get_by_label, page.get_by_placeholder, page.get_by_test_id]:
        loc_mock.return_value.click = AsyncMock(side_effect=PWTimeoutError("chain fail"))

    page.evaluate = AsyncMock(return_value=[
        {"tag": "button", "text": "이메일", "placeholder": "",
         "label": "", "testid": "", "id": "", "name": ""}
    ])
    # 옵션 B 가 반환한 Locator 의 click 시도가 ToolExecutionError
    # (1차 chain 호출도 동일 mock 이라 first call 은 PWTimeoutError, 두번째 = TEE)
    page.get_by_text.return_value.click = AsyncMock(
        side_effect=[PWTimeoutError("chain fail"), TEE(ErrorCode.TOOL_UI_UNKNOWN, "kaboom")]
    )

    step = {"selector": "이메일", "selector_type": "text", "action": "click"}
    with pytest.raises(TEE):
        await tool._run_dom_action(page, "click", step)
