"""UI 테스트 Tool (FR-006).

ActionMapperAgent (FR-004) 가 출력한 ActionMapping 의 ActionStep 시퀀스를
Python Playwright 로 실행하고, 스텝별 스크린샷·console 로그·실행 시간을
UITestResult 로 반환한다.

호출자(Layer 2 노드 _test_execution) 가 Playwright Page 인스턴스를 주입한다.
APITraceTool 과 동일 page 인스턴스를 공유하므로 cross-check 시 trace_id 매칭이
자연스럽다 (page.context 의 extra_http_headers 에 X-Trace-Id 사전 주입 가정).

설계 결정: memory/project_qapilot_ui_test_tool_design.md (옵션 D — ActionMapping 직접 실행)

C/D 정책 (PR #73 ActionMapper 정규화와 정합):
- ActionMapper 는 가능한 한 실패하지 않고 표준화한다.
- CodeGenerator 는 가능한 한 코드 생성을 멈추지 않는다.
- 실제 유효성은 UITestTool 실행 단계에서 판단한다.
- 모호 케이스는 1-step fallback 적용 + error 필드에 명시.

담당: D
Created: 2026-05-15
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import expect

from qapilot.shared.errors import ErrorCode, ToolExecutionError
from qapilot.shared.schemas import ActionStep, UIStepResult, UITestResult
from qapilot.tools.base_tool import BaseTool

# action 27종 — PR #73 ActionMapper 정규화 vocabulary 와 정합.
# selector 가 필요한 DOM action (18종)
_DOM_ACTIONS = {
    "fill", "clear", "click", "dblclick", "hover", "select",
    "check", "uncheck", "press", "upload",
    "assert", "assert_visible", "assert_hidden", "assert_text",
    "assert_value", "assert_enabled", "assert_disabled", "assert_count",
}
# selector 가 필요 없는 page-level action (9종)
_PAGE_ACTIONS = {
    "navigate", "reload", "go_back", "go_forward",
    "wait", "wait_for_url", "wait_for_load_state", "wait_for_response",
    "assert_url",
}
_SUPPORTED_ACTIONS = _DOM_ACTIONS | _PAGE_ACTIONS

# selector_type 7종 → Page.get_by_* 매핑. css/xpath 는 page.locator() 사용.
_GET_BY_METHODS = {
    "role": "get_by_role",
    "label": "get_by_label",
    "placeholder": "get_by_placeholder",
    "text": "get_by_text",
    "testid": "get_by_test_id",
    "alttext": "get_by_alt_text",
    "title": "get_by_title",
}


class UITestTool(BaseTool):
    """UI 테스트 Tool (FR-006).

    역할: ActionStep 시퀀스를 Playwright 실행. 스텝별 스크린샷·console 로그·에러 캡처.
    설정: 페이지 로드 30초, 스텝 30초 타임아웃 (page 인스턴스 측에서 설정 가정).
    스크린샷: PNG, screenshot_dir 미지정 시 안 찍음.
    """

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """ActionMapping 의 steps 를 순차 실행하고 UITestResult 를 반환한다.

        Args:
            params: 실행 파라미터.
                page (Page): 외부 주입 Playwright 페이지 (필수).
                action_mapping (ActionMapping): tc_id + steps[ActionStep] (필수).
                tc_id (str, optional): action_mapping.tc_id 없을 때 사용.
                target_url (str, optional): navigate 의 relative path 에 prefix.
                screenshot_dir (str | Path, optional): 스텝별 스크린샷 저장 경로.

        Returns:
            {"ui_result": UITestResult}.

        Raises:
            ToolExecutionError: page 누락, 또는 steps 가 비어있을 때.
        """
        page = params.get("page")
        if page is None:
            raise ToolExecutionError(
                ErrorCode.TOOL_001,
                "params['page'] (Playwright Page 인스턴스) 필요",
            )

        action_mapping = params.get("action_mapping") or {}
        tc_id: str = params.get("tc_id") or action_mapping.get("tc_id") or "unknown"
        target_url: str = params.get("target_url", "")

        screenshot_dir: Path | None = None
        if params.get("screenshot_dir") is not None:
            screenshot_dir = Path(params["screenshot_dir"])
            screenshot_dir.mkdir(parents=True, exist_ok=True)

        steps: list[ActionStep] = list(action_mapping.get("steps") or [])
        if not steps:
            raise ToolExecutionError(
                ErrorCode.TOOL_001,
                f"action_mapping['steps'] 가 비어있습니다. (tc_id={tc_id})",
            )

        console_logs: list[str] = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))

        step_results, tc_status, total_duration_ms = await self._run_steps(
            page, steps, target_url, screenshot_dir, console_logs
        )

        ui_result: UITestResult = {
            "tc_id": tc_id,
            "status": tc_status,
            "steps": step_results,
            "total_duration_ms": total_duration_ms,
        }
        self.logger.info(
            "ui_test_complete",
            tc_id=tc_id,
            status=tc_status,
            steps=len(step_results),
            duration_ms=total_duration_ms,
        )
        return {"ui_result": ui_result}

    async def _run_steps(
        self,
        page: Page,
        steps: list[ActionStep],
        target_url: str,
        screenshot_dir: Path | None,
        console_logs: list[str],
    ) -> tuple[list[UIStepResult], str, int]:
        """ActionStep 시퀀스를 순차 실행. 실패 시 후속 스텝 skip."""
        step_results: list[UIStepResult] = []
        total_start = time.monotonic()
        tc_status = "pass"

        for idx, step in enumerate(steps):
            step_no = int(step.get("step_no") or idx + 1)
            action = step.get("action", "")

            step_start = time.monotonic()
            status: str = "pass"
            error_msg: str | None = None

            try:
                await self._run_step(page, step, target_url)
            except PWTimeoutError as e:
                status = "fail"
                tc_status = "fail"
                code = ErrorCode.TOOL_UI_LOCATOR_NOT_FOUND if action in _DOM_ACTIONS else ErrorCode.TOOL_UI_TIMEOUT
                error_msg = f"{code}: {e}"
            except AssertionError as e:
                status, tc_status = "fail", "fail"
                error_msg = f"{ErrorCode.TOOL_UI_ASSERTION_FAIL}: {e}"
            except ToolExecutionError as e:
                # _run_step 가 미지원 action 또는 fallback 실패 시 raise
                status, tc_status = "fail", "fail"
                error_msg = f"{e.code}: {e.message}"
            except Exception as e:
                status, tc_status = "fail", "fail"
                code = ErrorCode.TOOL_UI_NAVIGATION_FAIL if action == "navigate" else ErrorCode.TOOL_UI_UNKNOWN
                error_msg = f"{code}: {type(e).__name__}: {e}"

            screenshot_path = await self._capture_screenshot(page, screenshot_dir, step_no)
            duration_ms = int((time.monotonic() - step_start) * 1000)

            step_results.append({
                "step_no": step_no,
                "action": action,
                "status": status,  # type: ignore[typeddict-item]
                "screenshot_path": screenshot_path,
                "console_logs": list(console_logs),
                "error": error_msg,
                "duration_ms": duration_ms,
            })

            if status == "fail":
                for remaining in steps[idx + 1:]:
                    step_results.append({
                        "step_no": int(remaining.get("step_no") or 0),
                        "action": remaining.get("action", ""),
                        "status": "skip",
                        "screenshot_path": None,
                        "console_logs": [],
                        "error": "이전 스텝 실패로 건너뜀",
                        "duration_ms": 0,
                    })
                break

        total_duration_ms = int((time.monotonic() - total_start) * 1000)
        return step_results, tc_status, total_duration_ms

    async def _run_step(self, page: Page, step: ActionStep, target_url: str) -> None:
        """단일 ActionStep 을 실행한다. 27종 action 분기 + 1-step fallback."""
        action = step.get("action", "")

        if action not in _SUPPORTED_ACTIONS:
            raise ToolExecutionError(
                ErrorCode.TOOL_UI_UNSUPPORTED_ACTION,
                f"지원하지 않는 action: {action!r}",
            )

        if action in _PAGE_ACTIONS:
            await self._run_page_action(page, action, step, target_url)
        else:
            await self._run_dom_action(page, action, step)

    async def _run_page_action(
        self, page: Page, action: str, step: ActionStep, target_url: str
    ) -> None:
        """page-level action (selector 없음)."""
        value = step.get("value")

        if action == "navigate":
            url = (value or "").strip()
            if not url:
                raise ToolExecutionError(
                    ErrorCode.TOOL_UI_NAVIGATION_FAIL, "navigate 에는 value 필요"
                )
            full = (
                url if url.startswith(("http://", "https://"))
                else f"{target_url.rstrip('/')}{url}"
            )
            await page.goto(full)
            return

        if action == "reload":
            await page.reload()
            return
        if action == "go_back":
            await page.go_back()
            return
        if action == "go_forward":
            await page.go_forward()
            return

        if action == "wait":
            # value 가 숫자 문자열 → timeout, 아니면 networkidle (1-step fallback)
            if value is not None and str(value).strip().isdigit():
                await page.wait_for_timeout(int(value))
            else:
                await page.wait_for_load_state("networkidle")
            return
        if action == "wait_for_url":
            await page.wait_for_url(value or "**/*")
            return
        if action == "wait_for_load_state":
            # value 가 "load" | "domcontentloaded" | "networkidle" — 미지정 시 networkidle
            state = (value or "networkidle").strip()
            if state not in {"load", "domcontentloaded", "networkidle"}:
                state = "networkidle"
            await page.wait_for_load_state(state)  # type: ignore[arg-type]
            return
        if action == "wait_for_response":
            await page.wait_for_response(value or "**/*")
            return

        if action == "assert_url":
            expected = step.get("expected") or value
            if expected is None:
                raise ToolExecutionError(
                    ErrorCode.TOOL_UI_ASSERTION_FAIL, "assert_url 에는 expected 필요"
                )
            await expect(page).to_have_url(expected)
            return

    async def _run_dom_action(
        self, page: Page, action: str, step: ActionStep
    ) -> None:
        """DOM action (selector 필요). selector None → fallback locator."""
        value = step.get("value")
        expected = step.get("expected")
        locator = self._build_locator(page, step)

        if action == "fill":
            await locator.fill(value or "")
            return
        if action == "clear":
            await locator.clear()
            return
        if action == "click":
            await locator.click()
            return
        if action == "dblclick":
            await locator.dblclick()
            return
        if action == "hover":
            await locator.hover()
            return
        if action == "select":
            await locator.select_option(value or "")
            return
        if action == "check":
            await locator.check()
            return
        if action == "uncheck":
            await locator.uncheck()
            return
        if action == "press":
            await locator.press(value or "Enter")  # 1-step fallback: Enter
            return
        if action == "upload":
            if not value:
                raise ToolExecutionError(
                    ErrorCode.TOOL_UI_UNKNOWN, "upload 에는 value (파일 경로) 필요"
                )
            await locator.set_input_files(value)
            return

        # assert 계열 8종
        if action == "assert" or action == "assert_visible":
            await expect(locator).to_be_visible()
            return
        if action == "assert_hidden":
            await expect(locator).to_be_hidden()
            return
        if action == "assert_text":
            await expect(locator).to_have_text(expected or "")
            return
        if action == "assert_value":
            await expect(locator).to_have_value(expected or "")
            return
        if action == "assert_enabled":
            await expect(locator).to_be_enabled()
            return
        if action == "assert_disabled":
            await expect(locator).to_be_disabled()
            return
        if action == "assert_count":
            # expected 가 숫자 문자열이어야 함. 변환 실패 시 fallback=0
            try:
                count = int(str(expected).strip()) if expected is not None else 0
            except (ValueError, TypeError):
                count = 0
            await expect(locator).to_have_count(count)
            return

    def _build_locator(self, page: Page, step: ActionStep) -> Locator:
        """selector + selector_type → Locator.

        selector None + DOM action (ActionMapper 의 fallback 통과 후 도착) →
        expected/value 로 text fallback. 9종 type 모두 지원, 미지원은 css fallback.
        """
        selector = step.get("selector")
        selector_type = step.get("selector_type") or "css"

        # selector None 도착 시 1-step fallback (ActionMapper 가 fallback 못 한 경우)
        if not selector:
            fallback = step.get("expected") or step.get("value") or step.get("action") or ""
            self.logger.warning(
                "ui_fallback_locator",
                action=step.get("action"),
                used=fallback,
                code=ErrorCode.TOOL_UI_FALLBACK_USED,
            )
            return page.get_by_text(str(fallback))

        if selector_type in _GET_BY_METHODS:
            method = getattr(page, _GET_BY_METHODS[selector_type])
            return method(selector)
        if selector_type == "xpath" and not selector.startswith("xpath="):
            return page.locator(f"xpath={selector}")
        return page.locator(selector)

    @staticmethod
    async def _capture_screenshot(
        page: Page, screenshot_dir: Path | None, step_no: int
    ) -> str | None:
        """스크린샷 저장. screenshot_dir 미지정 시 None."""
        if screenshot_dir is None:
            return None
        path = screenshot_dir / f"step_{step_no:02d}.png"
        try:
            await page.screenshot(path=str(path))
        except Exception:
            return None
        return str(path)
