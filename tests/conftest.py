"""pytest 공통 fixture — strict spec mock fixture 통합 (이슈 #143).

본 모듈은 본인 영역 (D = UITestTool/CodeGenerator + A = pipeline) 단위 테스트가
공유할 strict spec mock fixture 를 정의한다.

설계 원칙 (spec §단위 테스트 정책, v1.16):
- 모든 mock 은 spec=RealClass 강제. 임의 attribute 주입 금지.
- 실 API 에 없는 attribute 를 mock 에 주입하려 하면 AttributeError 로 즉시 차단
  (예: `page.wait_for_response = AsyncMock()` 같은 함정 패턴 — 사례 #1 PR #138)
- pytest 표준 fixture 주입 패턴 사용 (helper 함수 호출 X)

본 fixture 사용 시 단위 테스트가 통합 단계에서 폭발하는 패턴 (사례 #1) 의 진원 차단.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Locator, Page

from qapilot.shared.llm_client import LLMClient
from qapilot.tools.base_tool import BaseTool


def _build_mock_locator() -> MagicMock:
    """단일 Locator mock — Playwright Locator 의 모든 async 메서드 AsyncMock 주입."""
    loc = MagicMock(spec=Locator)
    loc.fill = AsyncMock()
    loc.click = AsyncMock()
    loc.press = AsyncMock()
    loc.clear = AsyncMock()
    loc.select_option = AsyncMock()
    loc.check = AsyncMock()
    loc.uncheck = AsyncMock()
    loc.dblclick = AsyncMock()
    loc.hover = AsyncMock()
    loc.set_input_files = AsyncMock()
    loc.evaluate = AsyncMock()
    return loc


@pytest.fixture
def mock_page() -> MagicMock:
    """공유 locator 패턴 — page.locator / page.get_by_* 가 모두 같은 mock locator 반환.

    범용 케이스 (`test_ui_test_tool.py` 다수 + `test_ui_test_tool_auto_navigate_121.py`)
    에 사용. 옵션 A chain 의 entry 별 distinct 검증이 필요한 케이스는
    `mock_page_with_distinct_locators` 사용.

    spec=Page 강제로 실 Playwright API 에 없는 attribute 주입 차단.
    """
    page = MagicMock(spec=Page)
    page.on = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.screenshot = AsyncMock()
    # evaluate 의 default return_value 는 빈 문자열 — UITestTool 옵션 C fail-safe
    # 의 page-wide fuzzy match 가 잘못 발동하지 않도록. fuzzy 검증이 필요한 테스트는
    # `mock_page.evaluate = AsyncMock(return_value="...")` 로 재정의.
    page.evaluate = AsyncMock(return_value="")
    page.go_back = AsyncMock()
    page.go_forward = AsyncMock()
    page.reload = AsyncMock()

    loc = _build_mock_locator()
    page.locator = MagicMock(return_value=loc)
    for name in (
        "get_by_role", "get_by_label", "get_by_placeholder", "get_by_text",
        "get_by_test_id", "get_by_alt_text", "get_by_title",
    ):
        setattr(page, name, MagicMock(return_value=loc))

    return page


@pytest.fixture
def mock_page_with_distinct_locators() -> MagicMock:
    """get_by_* 별로 distinct Locator mock 반환 — chain entry 검증용.

    `test_ui_test_tool_fallback_chain_111.py` 의 옵션 A chain 정의 정확성 검증 (각
    entry 가 distinct locator) 에 사용.
    """
    page = MagicMock(spec=Page)
    page.on = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.screenshot = AsyncMock()
    page.evaluate = AsyncMock(return_value="")
    for name in (
        "get_by_text", "get_by_label", "get_by_placeholder",
        "get_by_test_id", "get_by_role", "get_by_alt_text", "get_by_title", "locator",
    ):
        setattr(page, name, MagicMock(return_value=_build_mock_locator()))
    return page


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """LLMClient strict spec — 새 메서드 추가 시 자동 잠복 차단.

    `chat` 은 async 메서드 — spec=LLMClient 만으로는 자동 AsyncMock 화 안 되므로
    명시 주입. 누적 카운터 attribute (`total_input_tokens` 등) 는 0 초기값.

    각 테스트는 `llm.chat.return_value = LLMResponse(...)` 같이 시나리오별 응답 주입.
    """
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock()
    llm.total_input_tokens = 0
    llm.total_output_tokens = 0
    llm.total_cost_usd = 0.0
    llm.cache_hit = False
    return llm


@pytest.fixture
def mock_base_tool() -> MagicMock:
    """BaseTool strict spec — pipeline 노드의 tool.run 호출 mock 용.

    `test_pipeline_codebase_scan_fallback_156.py` / `test_pipeline_fixes_100.py` 의
    `patch(...CodebaseScannerTool, return_value=mock_base_tool)` 패턴에 사용.
    """
    tool = MagicMock(spec=BaseTool)
    tool.run = AsyncMock()
    return tool
