"""run 3b50a65b 검증 — "가입" 키워드 오염 (TS-006) 회귀 테스트.

TS-006 "신규 가입 테스트" (요금제 가입, POST /api/orders) 가 회원가입 페이지
(/signup + signup-submit/-error) 로 매핑된 원인 3중 오염:
1. _SEMANTIC_ALIASES["가입"] → signup
2. _frontend_candidate_score: "가입" → signup 페이지 +2.0
3. _route_hint_from_tc: "가입" → "/signup"

수정: 회원가입 명시 시에만 signup 신호. TC.api segment ↔ DOM route 일치를
keyword 보다 우선 (도메인 무관 신호).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from qapilot.agents.action_mapper_agent import ActionMapperAgent, _SEMANTIC_ALIASES


def _agent(dom_index=None) -> ActionMapperAgent:
    agent = ActionMapperAgent.__new__(ActionMapperAgent)
    agent.logger = MagicMock()
    agent._frontend_dom_index = dom_index or []
    return agent


_PLAN_TC = {
    "tc_id": "TS-006-TC-05",
    "name": "정상적인 신규 가입 시도",
    "given": "로그인된 사용자가 요금제 목록을 본다",
    "when": "신규 가입을 신청한다",
    "then": "가입이 완료된다",
    "api": "POST /api/orders",
}
_SIGNUP_TC = {
    "tc_id": "TS-001-TC-01",
    "name": "유효한 이메일 형식으로 회원가입",
    "given": "사용자가 유효한 이메일을 입력한다",
    "when": "회원가입을 시도한다",
    "then": "회원가입이 성공한다",
    "api": "POST /api/auth/signup",
}


class TestSemanticAliases:
    def test_bare_join_alias_removed(self):
        assert "가입" not in _SEMANTIC_ALIASES
        assert "회원가입" in _SEMANTIC_ALIASES


class TestRouteHint:
    DOM = [
        {"route": "/signup", "testid": "signup-submit"},
        {"route": "/plans", "testid": "plan-apply"},
        {"route": "/orders", "testid": "order-list"},
    ]

    def test_plan_join_tc_not_signup(self):
        hint = _agent(self.DOM)._route_hint_from_tc(_PLAN_TC)
        assert hint != "/signup"
        assert hint == "/orders"  # api segment 일치 우선

    def test_signup_tc_still_signup(self):
        # api 1차 (auth → route 없음) → keyword 2차 (회원가입 명시)
        dom = [{"route": "/signup", "testid": "signup-submit"}]
        assert _agent(dom)._route_hint_from_tc(_SIGNUP_TC) == "/signup"

    def test_no_api_no_keyword_returns_none(self):
        tc = {"name": "가족 그룹 가입 테스트", "given": "", "when": "가입 신청", "then": "가입 완료", "api": None}
        dom = [{"route": "/signup"}]
        assert _agent(dom)._route_hint_from_tc(tc) is None


class TestCandidateScore:
    SIGNUP_EL = {"file": "src/pages/Signup.vue", "page": "signup", "route": "/signup",
                 "control_type": "submit", "text": "가입하기", "testid": "signup-submit"}
    PLAN_EL = {"file": "src/pages/Plans.vue", "page": "plans", "route": "/plans",
               "control_type": "submit", "text": "요금제 신청하기", "testid": "plan-apply"}

    def test_plan_join_scenario_prefers_plan_elements(self):
        agent = _agent()
        agent._current_tc_api = "POST /api/plans"
        text = "정상적인 신규 가입 시도 요금제 목록에서 신규 가입을 신청한다"
        s_signup = agent._frontend_candidate_score(text, self.SIGNUP_EL)
        s_plan = agent._frontend_candidate_score(text, self.PLAN_EL)
        assert s_plan > s_signup

    def test_signup_scenario_still_prefers_signup(self):
        agent = _agent()
        agent._current_tc_api = "POST /api/auth/signup"
        text = "유효한 이메일 형식으로 회원가입 회원가입을 시도한다"
        s_signup = agent._frontend_candidate_score(text, self.SIGNUP_EL)
        s_plan = agent._frontend_candidate_score(text, self.PLAN_EL)
        assert s_signup > s_plan
