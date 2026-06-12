"""cross_check 완벽 점검 후속 — 구제(intent rescue)의 실증 게이트 회귀 테스트.

run d20fc18f 증거 기반 발견 3건:
- D1: positive 구제의 POST 2xx 가 endpoint 무관 — 자동 로그인 POST 200 이
      '가입 완료' 증거로 둔갑 (TS-006-TC-05)
- D2: validation 구제가 입력 완전성을 안 봄 — fill 1/4 매핑 결손으로
      email-required 에 막힌 것을 '비밀번호 규칙 검증 통과' 로 구제 (TS-001-TC-03)
- D3: LLM 응답 파싱 실패가 match_score 1.0 의 조용한 pass 로 둔갑
"""
from __future__ import annotations

from unittest.mock import MagicMock

from qapilot.agents.cross_check_agent import CrossCheckAgent


def _agent() -> CrossCheckAgent:
    agent = CrossCheckAgent.__new__(CrossCheckAgent)
    agent.logger = MagicMock()
    return agent


_POSITIVE_INTENT = {
    "name": "정상적인 신규 가입 시도", "tags": ["normal"],
    "then": "가입이 완료된다.", "api": "POST /api/orders",
}


class TestEndpointMatchedEvidence:
    def test_unrelated_post_200_not_rescue_evidence(self):
        # 자동 로그인 POST /api/auth/login 200 — 대상 endpoint (orders) 아님
        api = {"calls": [{"method": "POST", "status_code": 200,
                          "url": "http://sut/api/auth/login"}]}
        ui = {"steps": [{"action": "fill", "status": "fail"}]}
        assert _agent()._evaluate_scenario_intent(_POSITIVE_INTENT, ui, api) is False

    def test_matching_post_201_is_rescue_evidence(self):
        api = {"calls": [{"method": "POST", "status_code": 201,
                          "url": "http://sut/api/orders"}]}
        ui = {"steps": [{"action": "assert", "status": "fail"}]}
        assert _agent()._evaluate_scenario_intent(_POSITIVE_INTENT, ui, api) is True

    def test_no_api_field_blocks_api_evidence(self):
        # run 04d5f79e 2차 감사: api=None TC 가 무관 POST 200 (자동 로그인 등)
        # 으로 구제되던 구멍 — TS-028 'FCP 1.5초' 성능 TC 3건 false-pass 실증.
        # api=None 이면 API 증거 사용 불가, UI 전 step pass 만이 구제 근거.
        intent = dict(_POSITIVE_INTENT, api=None)
        api = {"calls": [{"method": "POST", "status_code": 200, "url": "http://sut/x"}]}
        ui = {"steps": [{"action": "assert", "status": "fail"}]}
        assert _agent()._evaluate_scenario_intent(intent, ui, api) is False

    def test_no_api_field_ui_all_pass_still_rescues(self):
        intent = dict(_POSITIVE_INTENT, api=None)
        api = {"calls": []}
        ui = {"steps": [{"action": "assert", "status": "pass"}]}
        assert _agent()._evaluate_scenario_intent(intent, ui, api) is True

    def test_negative_requires_endpoint_matched_4xx(self):
        # 무관 4xx (에셋 404) 가 negative 구제 증거로 둔갑 금지
        intent = {"name": "오류", "tags": ["edge_case"],
                  "then": "오류가 반환된다.", "api": "POST /api/family"}
        unrelated = {"calls": [{"method": "GET", "status_code": 404,
                                "url": "http://sut/assets/x.png"}]}
        matched = {"calls": [{"method": "POST", "status_code": 409,
                              "url": "http://sut/api/family"}]}
        ui = {"steps": []}
        assert _agent()._evaluate_scenario_intent(intent, ui, unrelated) is False
        assert _agent()._evaluate_scenario_intent(intent, ui, matched) is True


class TestInputsCompletenessGate:
    VALIDATION_INTENT = {
        "name": "비밀번호가 7자인 경우", "tags": ["boundary", "edge_case"],
        "then": "입력 오류가 표시된다.", "api": "POST /api/auth/signup",
    }

    def _form_prevent_ctx(self):
        ui = {"steps": [
            {"action": "fill", "status": "pass"},
            {"action": "click", "status": "pass"},
            {"action": "assert", "status": "fail"},
        ]}
        api = {"calls": []}  # POST 0 — form prevent
        return ui, api

    def test_incomplete_inputs_denies_rescue(self):
        ui, api = self._form_prevent_ctx()
        intent = dict(self.VALIDATION_INTENT, inputs_complete=False)
        assert _agent()._evaluate_scenario_intent(intent, ui, api) is False

    def test_complete_inputs_allows_form_prevent_rescue(self):
        ui, api = self._form_prevent_ctx()
        intent = dict(self.VALIDATION_INTENT, inputs_complete=True)
        assert _agent()._evaluate_scenario_intent(intent, ui, api) is True


class TestExpectedApiPath:
    def test_param_path_prefix(self):
        assert CrossCheckAgent._expected_api_path(
            "PATCH /api/orders/{order_id}/cancel") == "/api/orders/"

    def test_plain_path(self):
        assert CrossCheckAgent._expected_api_path("POST /api/auth/signup") == "/api/auth/signup"

    def test_none_empty(self):
        assert CrossCheckAgent._expected_api_path(None) == ""


import pytest


@pytest.mark.asyncio
async def test_llm_parse_failure_is_not_silent_pass():
    agent = _agent()
    agent.prompts = MagicMock()
    agent.prompts.system = MagicMock(return_value="S")
    agent.prompts.render = MagicMock(return_value="U")
    agent.with_correction_hint = MagicMock(side_effect=lambda p, e: p)
    resp = MagicMock(); resp.content = "이건 JSON 이 아님"
    chat = MagicMock()
    async def fake_chat(*a, **k): return resp
    chat.side_effect = fake_chat
    agent.llm = MagicMock(); agent.llm.chat = fake_chat

    mismatches, score, _, error_code, summary = await agent._analyze_with_llm({}, {}, {}, None)
    assert error_code == "CC_PARSE_FAIL"
    assert score == 0.0
    assert "unverified" in summary


class TestApiKindIntentAwareLabeling:
    """api kind 의 의도 인지 라벨링 — 의도된 4xx 를 fail 로 오라벨하던 격차."""

    def test_negative_intent_4xx_is_pass(self):
        from qapilot.orchestrator.pipeline import _derive_api_status
        payload = {"error_calls": 1, "calls": [
            {"method": "POST", "status_code": 409, "url": "http://sut/api/auth/signup"},
        ]}
        assert _derive_api_status(payload, intent_negative=True) == "pass"

    def test_negative_intent_5xx_still_fail(self):
        from qapilot.orchestrator.pipeline import _derive_api_status
        payload = {"error_calls": 1, "calls": [
            {"method": "POST", "status_code": 500, "url": "http://sut/api/orders"},
        ]}
        assert _derive_api_status(payload, intent_negative=True) == "fail"

    def test_positive_intent_4xx_is_fail(self):
        from qapilot.orchestrator.pipeline import _derive_api_status
        payload = {"error_calls": 1, "calls": [
            {"method": "POST", "status_code": 409, "url": "http://sut/api/auth/signup"},
        ]}
        assert _derive_api_status(payload, intent_negative=False) == "fail"

    def test_no_errors_pass_regardless(self):
        from qapilot.orchestrator.pipeline import _derive_api_status
        assert _derive_api_status({"error_calls": 0, "calls": []}, intent_negative=True) == "pass"
