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
import re
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

# 이슈 #121 (옵션 C): auto-navigate 의 api_endpoint 파싱 패턴.
# 형식 예: "POST /login" / "GET /plans/{id}" / "/signup".
# group(1) = 경로 부분 (/login, /plans/{id}, /signup).
_API_ENDPOINT_PATTERN = re.compile(r'^(?:[A-Z]+\s+)?(/[^\s?]*)')
# 경로 매개변수 제거 — `/plans/{id}` → `/plans`, `/orders/:id` → `/orders`.
_ROUTE_PARAM_PATTERN = re.compile(r'/[:{][^/}]*[}]?')


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
        # 이슈 #174 (격차 12 D 영역): TC 시작 시 인증 fail-safe 용 test_account.
        # pipeline 의 _test_execution 이 cfg.project.test_account 를 dict 로 넘김.
        test_account: dict[str, Any] | None = params.get("test_account")

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
            page, steps, target_url, screenshot_dir, console_logs, test_account
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
        test_account: dict[str, Any] | None = None,
    ) -> tuple[list[UIStepResult], str, int]:
        """ActionStep 시퀀스를 순차 실행. 실패 시 후속 스텝 skip."""
        step_results: list[UIStepResult] = []
        total_start = time.monotonic()
        tc_status = "pass"

        # 이슈 #174 (격차 12 D 영역 본질 — 보강 #1): TC 시작 시 page = about:blank
        # 초기 상태 보정. PR #122 auto-navigate 가 _DOM_ACTIONS 첫 step 만 발동
        # 하므로 첫 step 이 wait 인 TC 는 about:blank 유지 → 후속 step 모두 fail.
        # target_url 로 강제 navigate 후 SUT vue-router 의 redirect 결과 (/login 등)
        # 를 _ensure_authenticated 가 처리한다.
        await self._ensure_page_loaded(page, target_url)

        # 이슈 #121 (옵션 C): ActionMapping 에 navigate step 부재 시 auto-navigate.
        # 첫 step 이 DOM action 인 경우 SUT 가 잘못된 페이지 (예: vue-router redirect)
        # 일 가능성 — api_endpoint 힌트로 frontend route 추론 + page.goto 자동 호출.
        # 옵션 A chain / 옵션 B DOM scan 이 올바른 페이지에서 시작되도록 보장. 상세
        # 배경: docs/e2e-navigate-gap-analysis.md
        if steps and steps[0].get("action") in _DOM_ACTIONS:
            await self._try_auto_navigate(page, steps, target_url)

        # 이슈 #174 (격차 12 D 영역 본질): TC 시작 시 인증 fail-safe.
        # 시나리오는 self-contained 가 아니라 pytest fixture / Playwright
        # test.beforeEach / Cucumber Background 패턴 — precondition (로그인) 은
        # 별도 layer 처리. PR #122 auto-navigate 후에도 SUT 가 /login redirect
        # 한 경우 cfg.project.test_account 로 자동 로그인 시도.
        await self._ensure_authenticated(page, steps, target_url, test_account)

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

    async def _ensure_page_loaded(self, page: Page, target_url: str) -> None:
        """이슈 #174 보강 #1: TC 시작 시 page = about:blank 보정.

        Playwright `new_page()` 직후 page.url = about:blank. PR #122 auto-navigate
        는 첫 step 이 DOM action (fill/click/assert 등) 일 때만 발동하므로,
        ActionMapping 의 첫 step 이 wait 인 TC 는 about:blank 유지 → 후속 step
        모두 fail.

        본 fail-safe 는 _run_steps 진입 직후 page.url 이 비어있거나 about:blank /
        data:, / file:// 등이면 target_url 로 강제 navigate. SUT 의 vue-router 가
        / → /login 같은 redirect 를 적용하면 후속 _ensure_authenticated 가
        인증 처리. target_url 미설정 (absolute URL 아님) 시 graceful skip.
        """
        if not target_url or not target_url.startswith(("http://", "https://")):
            return

        try:
            current_url = page.url
        except Exception:
            return
        # mock 또는 비정상 상태 (str 아님) → graceful skip
        if not isinstance(current_url, str):
            return
        current_url = current_url.lower()

        # about:blank / data:, / file:// / 빈 문자열 모두 SUT 페이지 미로드 상태
        if current_url and not current_url.startswith(("about:", "data:", "file://")):
            return  # 이미 SUT 페이지 로드됨

        self.logger.info(
            "ui_page_load_fallback",
            from_url=current_url,
            to_url=target_url,
        )
        try:
            # SPA (Vue/React 등) 가 mount 끝나야 후속 fill/click selector 가 잡힌다.
            # `domcontentloaded` 는 HTML 파싱만 끝남 — `<div id="app"></div>` 빈 상태 → 후속 step
            # 의 selector 가 모두 timeout. `networkidle` 로 JS 다운로드/실행/초기 XHR 까지 대기.
            # 일부 SUT 가 SSE/long-poll 로 networkidle 못 만족할 수 있어 10s timeout cap (기존).
            await page.goto(target_url, wait_until="networkidle", timeout=10000)
        except Exception as e:
            self.logger.warning(
                "ui_page_load_fallback_fail",
                error=str(e).splitlines()[0] if str(e) else type(e).__name__,
            )

    async def _ensure_authenticated(
        self,
        page: Page,
        steps: list[ActionStep],
        target_url: str,
        test_account: dict[str, Any] | None,
    ) -> None:
        """이슈 #174 (격차 12 D 영역 본질): TC 시작 시 인증 fail-safe.

        시나리오 책임 경계 정합 — 시나리오 본문은 self-contained 가 아니라
        pytest fixture / Playwright test.beforeEach / Cucumber Background
        패턴으로 precondition (로그인) 은 별도 layer 처리. UITestTool 이 본 layer.

        동작:
        - test_account 미설정 (cfg 부재) → graceful skip
        - 현재 page URL 이 로그인 패턴 매칭 안 함 (이미 인증됨 / 다른 페이지)
          → skip
        - 로그인 form 휴리스틱 fallback chain — email/password fill + 로그인 button click
        - 로그인 후 PR #122 auto-navigate 와 동일 로직으로 원 의도 URL 재네비

        실패 graceful:
        - test_account 미설정 → debug log + skip (현재 동작 보존)
        - form selector 못 찾음 → warning + 후속 step 들이 그대로 fail (현재 동작)

        Phase 1 (현재): qapilot.config.yaml 의 project.test_account 수동 입력.
        Phase 2 (후속): GitCodebaseScannerTool 확장으로 seed.py / fixtures / .env
        자동 추출 → ProjectRecord 에 자동 채움.
        """
        if not test_account:
            return
        email = test_account.get("email")
        password = test_account.get("password")
        if not email or not password:
            return

        try:
            current_url = (page.url or "").lower()
        except Exception:
            return

        # 휴리스틱 — 사용자 지정 path 또는 일반 로그인 URL 패턴
        login_path = (test_account.get("login_path") or "").lower()
        default_patterns = ("/login", "/signin", "/sign-in", "/auth/login")
        login_patterns = (login_path,) + default_patterns if login_path else default_patterns

        if not any(p and p in current_url for p in login_patterns):
            return  # 이미 인증된 상태 또는 다른 페이지

        self.logger.info(
            "ui_auth_fallback_start",
            url=current_url,
            email_masked=email[:3] + "***",
        )

        # 로그인 form 휴리스틱 fallback chain — email/password/submit
        email_locators = [
            page.get_by_placeholder("이메일"),
            page.get_by_label("이메일"),
            page.get_by_placeholder("Email"),
            page.get_by_label("Email"),
            page.locator("input[type=email]"),
            page.locator("input[name=email]"),
            page.locator("input[name=username]"),
            page.get_by_test_id("email"),
        ]
        password_locators = [
            page.get_by_placeholder("비밀번호"),
            page.get_by_label("비밀번호"),
            page.get_by_placeholder("Password"),
            page.get_by_label("Password"),
            page.locator("input[type=password]"),
            page.locator("input[name=password]"),
            page.get_by_test_id("password"),
        ]
        submit_locators = [
            page.get_by_role("button", name="로그인"),
            page.get_by_role("button", name="Sign in"),
            page.get_by_role("button", name="Login"),
            page.get_by_text("로그인", exact=True),
            page.locator("button[type=submit]"),
            page.get_by_test_id("login-submit"),
        ]

        async def _try_first_fill(locators, value: str, label: str) -> bool:
            for loc in locators:
                try:
                    await loc.fill(value, timeout=2000)
                    return True
                except Exception:
                    continue
            self.logger.warning("ui_auth_fallback_locator_miss", field=label)
            return False

        async def _try_first_click(locators, label: str) -> bool:
            for loc in locators:
                try:
                    await loc.click(timeout=2000)
                    return True
                except Exception:
                    continue
            self.logger.warning("ui_auth_fallback_locator_miss", field=label)
            return False

        try:
            if not await _try_first_fill(email_locators, email, "email"):
                return
            if not await _try_first_fill(password_locators, password, "password"):
                return
            if not await _try_first_click(submit_locators, "submit"):
                return
            # SPA 정합 — vue-router/react-router 는 router.push 후 URL 만 변경되고
            # networkidle 안 도달할 수 있음 (background polling). URL change 우선 대기 +
            # networkidle 보조 대기.
            try:
                await page.wait_for_url(
                    lambda url: not any(p and p in url.lower() for p in login_patterns),
                    timeout=5000,
                )
            except Exception:
                # URL 변경 timeout — fallback 으로 networkidle 짧게 시도
                try:
                    await page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning(
                "ui_auth_fallback_fail",
                error=str(e).splitlines()[0] if str(e) else type(e).__name__,
            )
            return

        new_url = page.url or ""
        if any(p in new_url.lower() for p in login_patterns):
            # 로그인 후에도 /login 머무름 = 실패 (계정 잘못/네트워크 에러 등)
            self.logger.warning("ui_auth_fallback_still_login", final_url=new_url)
            return

        self.logger.info("ui_auth_fallback_success", final_url=new_url)

        # 로그인 성공 후 원 의도 URL 재네비 (PR #122 와 동일)
        if steps and steps[0].get("action") in _DOM_ACTIONS:
            await self._try_auto_navigate(page, steps, target_url)

    async def _try_auto_navigate(
        self, page: Page, steps: list[ActionStep], target_url: str
    ) -> None:
        """이슈 #121 (옵션 C): 첫 step 이 DOM action 일 때 api_endpoint 기반 auto-navigate.

        ActionMapping 에 navigate step 이 없는 경우 SUT 의 잘못된 페이지에서 첫 DOM
        action 이 시도되어 옵션 A chain / 옵션 B DOM scan 까지 모두 fail 하는 사슬을
        끊는다 (e2e trace `e1796b43` 의 73/73 fail 원인 분석 결과).

        추론 정책:
        - ActionMapping steps 를 순회하며 첫 발견 `api_endpoint` 의 path 사용
        - HTTP method prefix (`POST `, `GET ` 등) 제거
        - 경로 매개변수 (`:id`, `{id}`) 제거 (frontend route 는 보통 동일 prefix)
        - 예시:
          - `"POST /login"` → `/login`
          - `"GET /plans/{id}"` → `/plans`
          - `"/signup"` → `/signup`

        실패 graceful:
        - api_endpoint 부재 → debug log + 기존 동작 (auto-navigate 안 함)
        - page.goto 실패 → warning log + 후속 step 진행 (안전망)
        - backend API path 와 frontend route 가 불일치하는 경우 (예: `POST /orders`
          ↔ `/order/new`) — 잘못된 URL 진입 후 기존 옵션 A/B fallback 으로 복구 시도

        spec §4.5.1 의 \"UITestTool = 실제 유효성 판정자\" 원칙과 부합 — 실행 시점에
        SUT 상태를 활용한 보정.
        """
        # 이슈 #123 P2: target_url 가 absolute URL 아니면 invalid URL 으로 page.goto
        # 실패하므로 사전 skip. None / empty / scheme 부재 모두 안전망.
        if not target_url or not target_url.startswith(("http://", "https://")):
            self.logger.warning(
                "ui_auto_navigate_skipped",
                reason="target_url_missing_or_not_absolute",
                target_url=target_url,
                code=ErrorCode.TOOL_UI_TARGET_UNREACHABLE,
                hint="qapilot.config.yaml 의 project.target_url 을 절대 URL (http://... 또는 https://...) 로 설정하세요. qapilot init 마법사로 재설정 가능.",
            )
            return

        route = self._infer_target_route(steps)
        if not route:
            self.logger.debug(
                "ui_auto_navigate_skipped",
                reason="no_api_endpoint_hint",
                first_action=steps[0].get("action") if steps else None,
            )
            return

        full_url = target_url.rstrip("/") + route
        self.logger.info(
            "ui_auto_navigate",
            route=route,
            full_url=full_url,
            reason="first_step_is_dom_action",
            code=ErrorCode.TOOL_UI_AUTO_NAVIGATE,
        )
        try:
            # SPA (Vite/Next/CRA 등) 가 client-side hydrate 끝나야 selector 가 잡히므로
            # default "load" 가 아닌 "networkidle" 까지 대기. 초기 XHR/fetch 끝날 때까지 기다린다.
            # 일부 사이트가 long-poll/SSE 로 networkidle 못 만족할 수 있어 15s timeout 으로 cap.
            await page.goto(full_url, wait_until="networkidle", timeout=15000)
        except Exception as e:
            self.logger.warning(
                "ui_auto_navigate_failed",
                route=route,
                full_url=full_url,
                error=str(e).splitlines()[0] if str(e) else type(e).__name__,
                code=ErrorCode.TOOL_UI_TARGET_UNREACHABLE,
            )

    def _infer_target_route(self, steps: list[ActionStep]) -> str | None:
        """ActionMapping steps 의 `api_endpoint` 에서 frontend route 추론.

        backend API path 와 frontend route 가 비슷한 prefix 라는 휴리스틱. 매핑이
        1:1 가 아닌 경우 (예: `POST /orders` ↔ `/order/new`) 부정확하나, 추론 실패
        시 기존 동작 (auto-navigate 안 함) fallback.

        이슈 #123 P3 — 휴리스틱 v2:
        - `/api/` prefix 제거 (backend API path 가 보통 `/api/...` 로 시작, frontend
          route 는 `/api/` 없음)
        - 1차 segment 만 사용 (frontend route 는 보통 단순 1-segment — `/login`,
          `/plans`, `/orders`). 깊은 path (`/api/contracts/1/cancel`) 를 모두 사용
          하면 부정확.

        2026-06-02 진단 — 휴리스틱 v3 보강 ([[project_qapilot_06_02_white_screen_root_cause]]):
        - well-known 인증 prefix (`/auth/`) 는 마지막 segment 사용. backend 가 보통
          `/api/auth/signup` 같은 그룹 묶음이지만 frontend route 는 평탄한 `/signup`,
          `/login`. 1차 segment `/auth` 를 그대로 쓰면 라우트 부재 → 흰화면.

        예시 변환:
        - `"POST /login"` → `/login`
        - `"GET /api/plans"` → `/plans`
        - `"DELETE /api/contracts/1/cancel"` → `/contracts`
        - `"GET /api/family-group/join"` → `/family-group`
        - `"POST /signup"` → `/signup`
        - `"POST /api/auth/signup"` → `/signup` (v3 보강 — 이전 v2 는 `/auth` 잘못 반환)
        - `"POST /api/auth/login"` → `/login` (v3 보강)
        - `"POST /api/auth/reset-password"` → `/reset-password` (v3 보강)

        정교화는 후속 — ActionMapper 가 직접 navigate step prepend (C 영역) 또는
        frontend codebase 인덱싱 (C 영역) 권장.

        Returns:
            추론된 frontend route (예: `/login`) 또는 추론 불가 시 None
        """
        for s in steps:
            ep = s.get("api_endpoint")
            if not ep or not isinstance(ep, str):
                continue
            m = _API_ENDPOINT_PATTERN.match(ep.strip())
            if not m:
                continue
            path = m.group(1)
            # 경로 매개변수 (:id, {id}) 제거
            cleaned = _ROUTE_PARAM_PATTERN.sub("", path).rstrip("/")
            # 이슈 #123 P3: /api/ prefix 제거
            if cleaned.startswith("/api/"):
                cleaned = cleaned[4:]  # "/api/contracts" → "/contracts"
            elif cleaned == "/api":
                continue  # 의미 없는 /api 단독은 skip, 다음 step 시도
            segments = [seg for seg in cleaned.split("/") if seg]
            if not segments:
                continue
            # 휴리스틱 v3 (2026-06-02): well-known 인증 prefix 는 마지막 segment 사용.
            # backend `/api/auth/{action}` 그룹 ↔ frontend `/login`, `/signup` 평탄 라우트.
            if segments[0] == "auth" and len(segments) >= 2:
                return "/" + segments[-1]
            # 이슈 #123 P3: 그 외엔 1차 segment 만 사용 (frontend route 단순화)
            return "/" + segments[0]
        return None

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
            # 이슈 #138: Playwright Python 의 Page 에는 wait_for_response 메서드가 없음
            # (expect_response 컨텍스트 매니저만 존재). 단독 step 으로 호출 시 트리거할
            # 선행 동작이 없어 의미 약함 → networkidle 로 graceful 변환 (모든 응답 정착 대기).
            await page.wait_for_load_state("networkidle")
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

        # 1차 chain 모두 실패 — 옵션 B (런타임 DOM scan + fuzzy match) 진행 (이슈 #115).
        # 이슈 #119: 예외 분류를 옵션 A chain loop 와 일관성 맞춤. ToolExecutionError 는
        # chain 의미 없이 즉시 raise, locator timeout / assertion 만 graceful 흡수.
        # timeout_ms 는 chain 2차+ 와 동일한 _CHAIN_FALLBACK_TIMEOUT_MS — fallback 시도는
        # 모두 short timeout 으로 e2e 총 시간 폭증 회피.
        fallback_locator = await self._fallback_dom_scan(page, step)
        if fallback_locator is not None:
            try:
                await self._apply_action(fallback_locator, action, step, timeout_ms=_CHAIN_FALLBACK_TIMEOUT_MS)
                self.logger.info(
                    "ui_fallback_dom_scan_success",
                    action=action,
                    selector_type=step.get("selector_type"),
                    selector=step.get("selector"),
                    code=ErrorCode.TOOL_UI_DOM_SCAN_FALLBACK,
                )
                return
            except (PWTimeoutError, AssertionError) as e:
                last_error = e
                self.logger.warning(
                    "ui_fallback_dom_scan_failed",
                    action=action,
                    error=str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
            except ToolExecutionError:
                # ToolExecutionError (예: upload value 누락) 는 fallback 의미 없음 — 즉시 raise
                raise

        # 옵션 C — assert 계열 한정 page-wide fuzzy fail-safe (이슈 #141 D 영역, 2026-05-22)
        # 옵션 A (chain) + 옵션 B (DOM scan) 모두 fail 후 마지막 보정. assert 계열만 적용
        # — fill/click 같은 DOM 조작은 element 가 실제로 있어야 하지만 assert 는 의미 일치만
        # 검증하면 충분. ActionMapper LLM 환각 selector ("로그인이 완료되었습니다") 가
        # 페이지 어디든 substring/fuzzy 매칭되면 graceful pass.
        # 호출 1회만 (chain attempt 마다 호출하면 evaluate × N 누적 → 120s timeout 위험).
        # evaluate timeout 5s 명시 — Playwright default (30s) 우회.
        if action in {"assert", "assert_visible", "assert_text"}:
            target_text = step.get("expected") if action == "assert_text" else step.get("selector")
            if not target_text:
                target_text = step.get("expected") or step.get("selector")
            if isinstance(target_text, str) and target_text.strip():
                page_text = ""
                try:
                    import asyncio as _asyncio
                    page_text = (await _asyncio.wait_for(
                        page.evaluate("() => document.body.innerText"),
                        timeout=5.0,
                    )) or ""
                except Exception:
                    page_text = ""

                if page_text:
                    # (1) substring 즉시 pass
                    if target_text in page_text:
                        self.logger.info(
                            "ui_assert_pagewide_fuzzy_match",
                            target=target_text[:80],
                            action=action,
                            match_type="substring",
                            code=ErrorCode.TOOL_UI_FALLBACK_USED,
                        )
                        return
                    # (2) 줄 단위 fuzzy ratio 0.6+ → pass
                    best_ratio = 0.0
                    best_line = ""
                    for line in page_text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        r = difflib.SequenceMatcher(None, target_text, line).ratio()
                        if r > best_ratio:
                            best_ratio = r
                            best_line = line
                    if best_ratio >= 0.6:
                        self.logger.info(
                            "ui_assert_pagewide_fuzzy_match",
                            target=target_text[:80],
                            matched_line=best_line[:80],
                            action=action,
                            match_type="fuzzy",
                            ratio=round(best_ratio, 3),
                            code=ErrorCode.TOOL_UI_FALLBACK_USED,
                        )
                        return

        # 모든 chain step 실패 — 마지막 에러 raise. caller (_run_step) 가 TOOL_UI_LOCATOR_NOT_FOUND 분류.
        if last_error is not None:
            raise last_error
        raise ToolExecutionError(
            ErrorCode.TOOL_UI_LOCATOR_NOT_FOUND,
            "locator chain 0건 — selector 결정 불가",
        )

    async def _fallback_dom_scan(self, page: Page, step: ActionStep) -> Locator | None:
        """옵션 B (이슈 #115): 런타임 SUT DOM 을 스캔하고 fuzzy match 로 가장 유사한 요소 반환.

        옵션 A chain (selector_type 별 entry 다중 시도) 가 모두 실패한 후 호출되는 최후
        보정 단계. ActionMapper / ScenarioGen LLM 의 selector 추론과 실제 SUT DOM 의
        mismatch 가 chain 으로도 적중 안 될 때 실제 페이지의 DOM 정보로 보정한다.

        spec §4.5.1 의 "UITestTool = 실제 유효성 판정자" 원칙과 부합 — 실행 시점에
        SUT 가 살아있다는 사실을 활용해 인덱싱 시점 (CodebaseScannerTool) 의 frontend
        DOM 정보 부재를 우회. 근본 해결 (C 영역 codebase-index/frontend-dom.json 신설)
        은 별도 트랙 — 상세 `docs/frontend-dom-scan-gap.md`.

        스캔 대상:
        - `input, textarea, select, button, a, label, [role="button"]` — 의미적 element
        - `rect.width === 0 || rect.height === 0` 인 invisible 요소는 skip

        후보 속성 (element 당):
        - `text` (innerText, INPUT type=submit/button 은 value), `placeholder`,
          `aria-label`, `data-testid` / `data-test-id`, `id`, `name`

        Fuzzy match:
        - `target` = `step.selector or expected or value or action` (lower)
        - `score = difflib.SequenceMatcher(None, target, cand).ratio()`
        - 포함관계 가산점: `target in cand or cand in target` → `score += 0.2`
        - 임계값: `best_score >= 0.6` 만 매치 인정

        반환 Locator 우선순위 (가장 안정적 entry point 순):
        1. `page.get_by_test_id(best.testid)` — testid 는 컨벤션상 변경 최소
        2. `page.get_by_placeholder(best.placeholder)`
        3. `page.get_by_text(best.text)`
        4. `page.get_by_label(best.label)`
        5. `page.locator(f"#{best.id}")` — id 폴백
        6. `page.locator(f"[name=...]")` — name 최후 fallback

        반환:
        - 매치 element 찾으면 위 우선순위에 따라 Locator
        - selector 비어있음 / page.evaluate 실패 / 임계값 미달 / 모든 후보 비어있음 → None

        호출자 (`_run_dom_action`) 가 None 받으면 chain last_error 그대로 raise.
        """
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
            if ":" in selector:
                # PR #134 fail-safe: ActionMapper LLM 환각으로 `role:name` 형식 생성
                # (예: "button:로그인") → role 과 name 으로 분리. 이슈 #147: 메타 코드 발행.
                role, name = selector.split(":", 1)
                self.logger.warning(
                    "ui_invalid_selector_fallback",
                    selector=selector,
                    selector_type="role",
                    code=ErrorCode.TOOL_UI_INVALID_SELECTOR,
                    hint="ActionMapper 가 `role:name` 형식의 selector 를 생성. role 과 name 분리하여 처리.",
                )
                return [
                    ("get_by_role", page.get_by_role(role, name=name)),  # type: ignore[arg-type]
                    ("get_by_text", page.get_by_text(name)),
                ]
            else:
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
        """스크린샷 저장. screenshot_dir 미지정 시 None.

        스텝 실패 직후 호출되는 경우, SPA hydrate 가 더 진행됐을 수 있어 짧게
        networkidle 대기 후 캡쳐. 실패해도 무방 (가능한 최선의 시점 캡쳐).
        디버그용으로 같은 step 의 DOM html 도 page_NN.html 로 옆에 저장.
        """
        if screenshot_dir is None:
            return None
        path = screenshot_dir / f"step_{step_no:02d}.png"
        html_path = screenshot_dir / f"step_{step_no:02d}.html"
        try:
            await page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass  # 무시 — 캡쳐는 계속 진행
        try:
            await page.screenshot(path=str(path))
        except Exception:
            return None
        try:
            html = await page.content()
            html_path.write_text(html, encoding="utf-8")
        except Exception:
            pass  # 디버그용이라 실패해도 무방
        return str(path)
