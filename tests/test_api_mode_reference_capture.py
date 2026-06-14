"""api-mode 스텝별 참고 화면 (방안 A) — ui payload steps[] + 결과 라우트 헬퍼.

run 004be10c 해부: api-mode 의 참고 화면이 navigate-only 라 step_1~6 이 전부
동일 이미지(빈 폼)였고, ui payload.steps=[] 라 프론트가 스텝 스트립을 못 그렸다.
방안 A = 부작용 없는 범위(navigate+fill)로 실제 구동해 스텝별로 다른 화면 +
ui payload.steps[] 를 매핑 스텝으로 채움.
"""
from __future__ import annotations

import inspect

from qapilot.orchestrator.pipeline import (
    _api_mode_ref_steps,
    _make_live_shot,
    _ref_step_plan,
    _result_route_from,
)


class TestMakeLiveShot:
    """라이브 프리뷰 — 스텝마다 증분 업로드 콜백 (실행 중 스텝별 전진)."""

    def test_none_when_no_ui_result_id(self):
        # DB 비활성(id 없음) → None → 캡처는 디스크에만, mirror 경로로.
        assert _make_live_shot(
            trace_id="t", ts_id="ts", tc_id="tc", ui_result_id=None) is None

    def test_returns_async_callback_when_id_present(self):
        cb = _make_live_shot(
            trace_id="t", ts_id="ts", tc_id="tc", ui_result_id="uid")
        assert callable(cb)
        assert inspect.iscoroutinefunction(cb)


class TestRefStepPlanSafety:
    """방안 A 안전 불변식 — 부작용 중복 차단의 결정적 증명."""

    def test_terminal_mutating_submit_is_not_performed(self):
        # api_endpoint 를 가진 click = 터미널 mutating submit → 미수행(noop).
        # API 가 이미 동일 부작용을 수행했으므로 브라우저 클릭은 중복.
        step = {"action": "click", "selector": "signup-submit",
                "api_endpoint": "POST /api/auth/signup"}
        assert _ref_step_plan(step, None)["op"] == "noop"

    def test_non_mutating_click_is_performed(self):
        # api_endpoint 없는 click (탭 전환·내비) 은 안전 → 수행.
        step = {"action": "click", "selector": "tab-2"}
        assert _ref_step_plan(step, None)["op"] == "click"

    def test_fill_prefers_actual_request_body_value(self):
        # 매핑 value 가 아니라 실제 제출값(request_body) 으로 채운다.
        step = {"action": "fill", "selector": "email", "target_name": "email",
                "value": "newuser_2026@test.com"}
        rb = {"email": "newuser_2026+abc123@test.com"}
        plan = _ref_step_plan(step, rb)
        assert plan == {"op": "fill", "value": "newuser_2026+abc123@test.com"}

    def test_fill_masks_process_env_placeholder(self):
        # process.env.* 마스킹은 빈 문자열로 (literal 'process.env.X' 타이핑 방지).
        step = {"action": "fill", "selector": "password", "target_name": "password",
                "value": "process.env.TEST_PASSWORD"}
        assert _ref_step_plan(step, None) == {"op": "fill", "value": ""}

    def test_fill_falls_back_to_mapping_value(self):
        step = {"action": "fill", "selector": "name", "target_name": "name",
                "value": "테스트유저"}
        assert _ref_step_plan(step, {})["op"] == "fill"
        assert _ref_step_plan(step, {})["value"] == "테스트유저"

    def test_navigate_only_for_path_routes(self):
        assert _ref_step_plan(
            {"action": "navigate", "value": "/signup"}, None) == {
            "op": "navigate", "route": "/signup"}
        # path 가 아닌 navigate (절대 URL 등) → noop
        assert _ref_step_plan(
            {"action": "navigate", "value": "https://x"}, None)["op"] == "noop"

    def test_assert_positive_maps_to_result_route(self):
        # positive(성공 기대) → 결과 앱 화면(/plans 류)
        assert _ref_step_plan({"action": "assert"}, None)["op"] == "result"
        assert _ref_step_plan({"action": "assert_visible"}, None)["op"] == "result"

    def test_assert_negative_stays_on_form(self):
        # ⚠️ negative(거부 기대) 는 /plans(성공 앱) 로 가면 '가입 성공처럼' 보임 →
        # noop 으로 폼(거부 맥락: 보호자 동의 필드/잘못된 입력) 에 머문다.
        assert _ref_step_plan(
            {"action": "assert"}, None, intent_negative=True)["op"] == "noop"
        assert _ref_step_plan(
            {"action": "assert_visible"}, None, intent_negative=True)["op"] == "noop"

    def test_negative_does_not_change_navigate_or_fill(self):
        # intent 는 결과 화면에만 영향 — navigate/fill 안전 동작은 동일.
        assert _ref_step_plan(
            {"action": "navigate", "value": "/signup"}, None,
            intent_negative=True)["op"] == "navigate"
        assert _ref_step_plan(
            {"action": "fill", "selector": "email", "value": "x"}, None,
            intent_negative=True)["op"] == "fill"
        # mutating submit 은 intent 무관 항상 noop (중복 제출 차단)
        assert _ref_step_plan(
            {"action": "click", "api_endpoint": "POST /x"}, None,
            intent_negative=True)["op"] == "noop"

    def test_wait_and_unknown_are_noop(self):
        assert _ref_step_plan({"action": "wait", "value": "1000"}, None)["op"] == "noop"
        assert _ref_step_plan({"action": "screenshot"}, None)["op"] == "noop"


class TestApiModeRefSteps:
    def test_maps_steps_with_screenshot_path(self):
        steps = [
            {"step_no": 1, "action": "navigate", "value": "/signup"},
            {"step_no": 2, "action": "fill", "selector": "email",
             "target_name": "email", "value": "x@y.com"},
            {"step_no": 7, "action": "assert", "selector": "signup-success-toast"},
        ]
        out = _api_mode_ref_steps(steps)
        assert [s["step_no"] for s in out] == [1, 2, 7]
        # screenshot_path 가 step_no 와 정합 — 프론트 라이트박스가 step_{n}.png 요청
        assert out[2]["screenshot_path"] == "step_7.png"
        # action 보존 (스텝 스트립 라벨)
        assert out[0]["action"] == "navigate"
        # target_name 폴백: selector 사용
        assert out[2]["target_name"] == "signup-success-toast"

    def test_status_is_none_not_per_step_verified(self):
        # verdict 의 단일 진실은 api/cross_check kind — 참고 화면 스텝은 검증 아님
        out = _api_mode_ref_steps([{"step_no": 1, "action": "navigate"}])
        assert out[0]["status"] is None

    def test_step_no_autofill_when_missing(self):
        out = _api_mode_ref_steps([{"action": "navigate"}, {"action": "fill"}])
        assert [s["step_no"] for s in out] == [1, 2]
        assert out[1]["screenshot_path"] == "step_2.png"

    def test_empty_and_none_safe(self):
        assert _api_mode_ref_steps(None) == []
        assert _api_mode_ref_steps([]) == []
        # 비-dict 항목 무시
        assert _api_mode_ref_steps(["bad", {"action": "navigate"}]) == [
            {"step_no": 1, "action": "navigate", "status": None,
             "screenshot_path": "step_1.png", "target_name": None, "value": None}
        ]


class TestResultRouteFrom:
    """프론트 라우트는 응답 redirect path 만 — API 세그먼트(/auth) 는 절대 안 됨."""

    def test_api_segment_is_never_a_frontend_route(self):
        # ⚠️ 회귀 가드: POST /api/auth/signup 에서 /auth 를 뽑으면 SPA 흰 화면
        # (run 92269223 step_7). response redirect 없으면 None → 네비 생략.
        assert _result_route_from(None) is None
        assert _result_route_from({}) is None
        # signup 응답(redirect 없음) → None (직전 채워진 폼 유지)
        assert _result_route_from(
            {"id": 135, "email": "x@y.com", "created_at": "2026"}) is None

    def test_response_redirect_is_used(self):
        assert _result_route_from({"redirect": "/dashboard"}) == "/dashboard"
        assert _result_route_from({"location": "/welcome"}) == "/welcome"
        assert _result_route_from({"next": "/plans"}) == "/plans"

    def test_non_path_redirect_ignored(self):
        # 절대 URL 등 '/' 로 시작 안 하는 값은 무시 → None
        assert _result_route_from({"redirect": "https://x"}) is None
        assert _result_route_from({"url": "javascript:void"}) is None
