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

import difflib
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

# spec §4.5.3 selector_type 9종 ↔ Playwright Page API 매핑표 (참조용).
# 이슈 #111 (의미적 chain 도입) 후 chain 메서드가 selector_type 별 직접 호출로 dispatch
# 하므로 이 dict 자체는 코드에서 미사용 (dead code). 단, spec §4.5.3 의 표와 1:1 매칭되어
# 새 selector_type 추가 시 일관성 확인용 + 디버깅 참조용으로 보존.
_GET_BY_METHODS = {  # noqa: F841 — spec §4.5.3 매핑표 참조용 (의미 보존)
    "role": "get_by_role",
    "label": "get_by_label",
    "placeholder": "get_by_placeholder",
    "text": "get_by_text",
    "testid": "get_by_test_id",
    "alttext": "get_by_alt_text",
    "title": "get_by_title",
}

# Fallback chain timeout 분배 (이슈 #111). 1차는 ActionMapper 의 결정에 가까운 timeout 유지,
# 2차 이상은 short timeout 으로 총 시간 폭증 방지. 1 × 10s + N × 5s ≈ 단일 30s default 와 비슷.
_CHAIN_PRIMARY_TIMEOUT_MS = 10_000
_CHAIN_FALLBACK_TIMEOUT_MS = 5_000


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
        """DOM action (selector 필요). spec §4.5.4 — selector_type 별 의미적 chain.

        1차 시도는 ActionMapper 가 결정한 selector_type 그대로 (timeout 10s).
        실패 시 2~N차 fallback chain (timeout 5s) 으로 적중 시도. 모두 실패 시 마지막
        에러 raise. 이슈 #111 — `qapilot test` e2e 의 UI 100% fail 원인 (LLM 추론
        selector ↔ SUT DOM mismatch) 보완. 자세한 배경: docs/frontend-dom-scan-gap.md
        """
        chain = self._build_locator_chain(page, step)
        last_error: BaseException | None = None

        for attempt, (label, locator) in enumerate(chain):
            # 1차 = long (10s), 2차+ = short (5s). 단일 30s default 와 비슷한 총 시간.
            timeout_ms = _CHAIN_PRIMARY_TIMEOUT_MS if attempt == 0 else _CHAIN_FALLBACK_TIMEOUT_MS
            try:
                await self._apply_action(locator, action, step, timeout_ms=timeout_ms)
                if attempt > 0:
                    self.logger.info(
                        "ui_fallback_chain_success",
                        action=action,
                        selector_type=step.get("selector_type"),
                        selector=step.get("selector"),
                        matched_at=label,
                        attempt=attempt + 1,
                        code=ErrorCode.TOOL_UI_FALLBACK_USED,
                    )
                return
            except (PWTimeoutError, AssertionError) as e:
                last_error = e
                next_label = chain[attempt + 1][0] if attempt + 1 < len(chain) else None
                self.logger.warning(
                    "ui_fallback_chain_retry",
                    action=action,
                    selector_type=step.get("selector_type"),
                    selector=step.get("selector"),
                    tried=label,
                    attempt=attempt + 1,
                    next=next_label,
                    error=str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
                continue
            except ToolExecutionError:
                # 명시적 ToolExecutionError (예: upload value 누락) 는 chain 의미 없음 — 즉시 raise
                raise

        # 1차 chain 모두 실패 — 옵션 B (런타임 DOM scan + fuzzy match) 진행
        fallback_locator = await self._fallback_dom_scan(page, step)
        if fallback_locator is not None:
            try:
                await self._apply_action(fallback_locator, action, step, timeout_ms=_CHAIN_FALLBACK_TIMEOUT_MS)
                self.logger.info(
                    "ui_fallback_dom_scan_success",
                    action=action,
                    selector_type=step.get("selector_type"),
                    selector=step.get("selector"),
                    code=ErrorCode.TOOL_UI_FALLBACK_USED,
                )
                return
            except Exception as e:
                last_error = e
                self.logger.warning(
                    "ui_fallback_dom_scan_failed",
                    action=action,
                    error=str(e).splitlines()[0] if str(e) else type(e).__name__,
                )

        # 모든 chain step 실패 — 마지막 에러 raise. caller (_run_step) 가 TOOL_UI_LOCATOR_NOT_FOUND 분류.
        if last_error is not None:
            raise last_error
        raise ToolExecutionError(
            ErrorCode.TOOL_UI_LOCATOR_NOT_FOUND,
            "locator chain 0건 — selector 결정 불가",
        )

    async def _fallback_dom_scan(self, page: Page, step: ActionStep) -> Locator | None:
        """옵션 B: 런타임 DOM을 스캔하고 fuzzy match로 가장 유사한 요소를 찾는다."""
        selector = str(step.get("selector") or step.get("expected") or step.get("value") or step.get("action") or "").strip()
        if not selector:
            return None

        # DOM 스캔 JS 스크립트 실행 (보이는 요소만 추출)
        js_code = """
        () => {
            const elements = document.querySelectorAll('input, textarea, select, button, a, label, [role="button"]');
            const data = [];
            for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                
                let text = el.innerText || el.textContent || '';
                if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) {
                    text = el.value || text;
                }
                
                data.push({
                    tag: el.tagName.toLowerCase(),
                    text: text.trim().substring(0, 100),
                    placeholder: el.placeholder || '',
                    label: el.getAttribute('aria-label') || '',
                    testid: el.getAttribute('data-testid') || el.getAttribute('data-test-id') || '',
                    id: el.id || '',
                    name: el.name || ''
                });
            }
            return data;
        }
        """
        try:
            dom_data = await page.evaluate(js_code)
        except Exception as e:
            self.logger.debug("dom_scan_evaluate_failed", error=str(e))
            return None

        best_match = None
        best_score = 0.0
        target = selector.lower()

        # Fuzzy match 로직
        for el in dom_data:
            candidates = [
                el.get("text", ""), el.get("placeholder", ""), el.get("label", ""),
                el.get("testid", ""), el.get("name", ""), el.get("id", "")
            ]
            
            for candidate in candidates:
                cand = candidate.strip().lower()
                if not cand:
                    continue
                    
                score = difflib.SequenceMatcher(None, target, cand).ratio()
                
                # 완전 포함 관계면 가산점 부여
                if target in cand or cand in target:
                    score += 0.2
                    
                if score > best_score:
                    best_score = score
                    best_match = el

        # 임계값(0.6) 이상인 경우만 매치 성공으로 간주
        if best_match and best_score >= 0.6:
            if best_match.get("testid"):
                return page.get_by_test_id(best_match["testid"])
            elif best_match.get("placeholder"):
                return page.get_by_placeholder(best_match["placeholder"])
            elif best_match.get("text"):
                return page.get_by_text(best_match["text"])
            elif best_match.get("label"):
                return page.get_by_label(best_match["label"])
            elif best_match.get("id"):
                return page.locator(f"#{best_match['id']}")
            elif best_match.get("name"):
                return page.locator(f"[name=\"{best_match['name']}\"]")
                
        return None

    async def _apply_action(
        self,
        locator: Locator,
        action: str,
        step: ActionStep,
        *,
        timeout_ms: int | None = None,
    ) -> None:
        """단일 Locator 에 action 27종 중 DOM action 18종 적용. timeout_ms 가 주어지면 그 timeout 사용.

        chain 의 각 step 에서 호출. _run_dom_action 의 fallback 순회 헬퍼.
        """
        value = step.get("value")
        expected = step.get("expected")
        kw: dict[str, Any] = {"timeout": timeout_ms} if timeout_ms is not None else {}

        if action == "fill":
            await locator.fill(value or "", **kw)
            return
        if action == "clear":
            await locator.clear(**kw)
            return
        if action == "click":
            await locator.click(**kw)
            return
        if action == "dblclick":
            await locator.dblclick(**kw)
            return
        if action == "hover":
            await locator.hover(**kw)
            return
        if action == "select":
            await locator.select_option(value or "", **kw)
            return
        if action == "check":
            await locator.check(**kw)
            return
        if action == "uncheck":
            await locator.uncheck(**kw)
            return
        if action == "press":
            await locator.press(value or "Enter", **kw)  # 1-step fallback: Enter
            return
        if action == "upload":
            if not value:
                raise ToolExecutionError(
                    ErrorCode.TOOL_UI_UNKNOWN, "upload 에는 value (파일 경로) 필요"
                )
            await locator.set_input_files(value, **kw)
            return

        # assert 계열 8종 — expect(locator) 는 timeout 인자 따로 받음
        if action == "assert" or action == "assert_visible":
            await expect(locator).to_be_visible(**kw)
            return
        if action == "assert_hidden":
            await expect(locator).to_be_hidden(**kw)
            return
        if action == "assert_text":
            await expect(locator).to_have_text(expected or "", **kw)
            return
        if action == "assert_value":
            await expect(locator).to_have_value(expected or "", **kw)
            return
        if action == "assert_enabled":
            await expect(locator).to_be_enabled(**kw)
            return
        if action == "assert_disabled":
            await expect(locator).to_be_disabled(**kw)
            return
        if action == "assert_count":
            try:
                count = int(str(expected).strip()) if expected is not None else 0
            except (ValueError, TypeError):
                count = 0
            await expect(locator).to_have_count(count, **kw)
            return

    def _build_locator_chain(self, page: Page, step: ActionStep) -> list[tuple[str, Locator]]:
        """selector_type 별 의미적 chain (spec §4.5.4 확장).

        1차는 ActionMapper 결정 존중, 2차 이상은 자연어 그룹 (text/label/placeholder) 사이의
        다른 entry point 시도 + testid 안전망. selector=None 케이스 (ActionMapper 의 fallback
        미작동) 도 chain 으로 확장.

        chain 의 의미:
        - text/label/placeholder 는 자연어 selector — 셋 다 시도해도 무해 (timeout 만 비용)
        - testid 는 식별자 — get_by_test_id + data-testid/data-test-id css 직접 시도
        - role/alttext/title 은 보조 → 실패 시 text fallback
        - css/xpath 는 정확한 selector 가정 — chain 적용 안 함

        Returns:
            [(label, Locator)] 순서대로 1차→N차. 각 entry 는 log 표기용 label + locator.
        """
        selector = step.get("selector")
        selector_type = step.get("selector_type") or "css"

        # selector 자체 부재 — ActionMapper 의 fallback 미작동 케이스
        if not selector:
            fallback = step.get("expected") or step.get("value") or step.get("action") or ""
            text = str(fallback)
            self.logger.warning(
                "ui_fallback_locator",
                action=step.get("action"),
                used=text,
                code=ErrorCode.TOOL_UI_FALLBACK_USED,
            )
            return [
                ("get_by_text", page.get_by_text(text)),
                ("get_by_placeholder", page.get_by_placeholder(text)),
                ("get_by_label", page.get_by_label(text)),
            ]

        if selector_type == "text":
            return [
                ("get_by_text", page.get_by_text(selector)),
                ("get_by_label", page.get_by_label(selector)),
                ("get_by_placeholder", page.get_by_placeholder(selector)),
                ("get_by_test_id", page.get_by_test_id(selector)),
            ]
        if selector_type == "label":
            return [
                ("get_by_label", page.get_by_label(selector)),
                ("get_by_text", page.get_by_text(selector)),
                ("get_by_placeholder", page.get_by_placeholder(selector)),
            ]
        if selector_type == "placeholder":
            return [
                ("get_by_placeholder", page.get_by_placeholder(selector)),
                ("get_by_label", page.get_by_label(selector)),
                ("get_by_text", page.get_by_text(selector)),
            ]
        if selector_type == "testid":
            return [
                ("get_by_test_id", page.get_by_test_id(selector)),
                ("locator[data-testid]", page.locator(f'[data-testid="{selector}"]')),
                ("locator[data-test-id]", page.locator(f'[data-test-id="{selector}"]')),
            ]
        if selector_type == "role":
            return [
                ("get_by_role", page.get_by_role(selector)),  # type: ignore[arg-type]
                ("get_by_text", page.get_by_text(selector)),
            ]
        if selector_type == "alttext":
            return [
                ("get_by_alt_text", page.get_by_alt_text(selector)),
                ("get_by_text", page.get_by_text(selector)),
            ]
        if selector_type == "title":
            return [
                ("get_by_title", page.get_by_title(selector)),
                ("get_by_text", page.get_by_text(selector)),
            ]
        if selector_type == "xpath":
            xpath = selector if selector.startswith("xpath=") else f"xpath={selector}"
            return [("xpath", page.locator(xpath))]
        # css 또는 알 수 없는 타입 — 단일 시도
        return [("locator", page.locator(selector))]

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
