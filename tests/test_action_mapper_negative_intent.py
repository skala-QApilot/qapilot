"""격차 1 — ActionMapper assert step 의 positive/negative outcome 분기 검증.

배경 (run `f142978d` + plan glistening-snuggling-treasure):
- `_resolve_mapping_with_frontend` 가 LLM step 의 selector 를 frontend 인덱스
  resolve 로 덮어쓰는데, `_intent_match_score` 에 outcome 방향성이 없어
  모든 TS-001 TC = `signup-success-toast`, 모든 TS-002 TC = `login-error` 로
  일괄 매핑 (positive/negative 무관) 됐다.

해결:
- `_infer_step_intent` assert branch 가 then/expected 에서 outcome 분류
- `_intent_match_score` 가 outcome 과 selector 방향 불일치 시 -2.0 감점
  (threshold 0.8 아래로) + 일치 시 가점
- assert 미해결 시 then 절을 expected 로 보존 (page-wide fuzzy fallback 용)
"""
from __future__ import annotations

from unittest.mock import MagicMock

from qapilot.agents.action_mapper_agent import ActionMapperAgent


def _make_agent() -> ActionMapperAgent:
    agent = ActionMapperAgent.__new__(ActionMapperAgent)
    agent.logger = MagicMock()
    return agent


_SIGNUP_SUCCESS = {
    "route": "/signup", "page": "Signup", "tag": "div",
    "control_type": "feedback_toast", "testid": "signup-success-toast",
    "text": "", "label": "", "placeholder": "", "actionable": False,
}
_SIGNUP_ERROR = {
    "route": "/signup", "page": "Signup", "tag": "div",
    "control_type": "feedback_message", "testid": "signup-error",
    "text": "", "label": "", "placeholder": "", "actionable": False,
}
_LOGIN_ERROR = {
    "route": "/login", "page": "Login", "tag": "div",
    "control_type": "feedback_message", "testid": "login-error",
    "text": "", "label": "", "placeholder": "", "actionable": False,
}


class TestInferOutcome:
    def test_negative_then_clause(self):
        agent = _make_agent()
        step = {"action": "assert", "selector": "", "expected": ""}
        tc = {"then": "이미 가입된 이메일이라는 오류 메시지가 표시된다 (409 Conflict)"}
        intent = agent._infer_step_intent(step, tc, "")
        assert intent["outcome"] == "negative"
        assert intent["target_kind"] == "assertion"

    def test_positive_then_clause(self):
        agent = _make_agent()
        step = {"action": "assert", "selector": "", "expected": ""}
        tc = {"then": "회원가입이 완료되고 성공 메시지가 노출된다"}
        intent = agent._infer_step_intent(step, tc, "")
        assert intent["outcome"] == "positive"

    def test_expected_text_also_classifies(self):
        agent = _make_agent()
        step = {"action": "assert_visible", "selector": "", "expected": "비밀번호가 유효하지 않습니다"}
        tc = {"then": ""}
        intent = agent._infer_step_intent(step, tc, "")
        assert intent["outcome"] == "negative"

    def test_explicit_target_kind_still_classifies_outcome(self):
        """LLM 이 target_kind 를 명시해 조기 return 을 타는 assert step 도
        outcome 분류 — trace 77bf4ec8 에서 이 경로가 outcome 을 건너뛰어
        TS-001 전 TC 가 signup-success-toast 로 재발했던 구멍의 회귀 테스트."""
        agent = _make_agent()
        step = {"action": "assert", "selector": "", "expected": "",
                "target_kind": "assertion"}
        tc = {"then": "이미 가입된 이메일이라는 오류 메시지가 표시된다"}
        intent = agent._infer_step_intent(step, tc, "")
        assert intent["target_kind"] == "assertion"
        assert intent["outcome"] == "negative"

    def test_explicit_kind_non_assert_has_no_outcome(self):
        agent = _make_agent()
        step = {"action": "click", "selector": "x", "target_kind": "submit"}
        tc = {"then": "오류가 표시된다"}
        intent = agent._infer_step_intent(step, tc, "")
        assert "outcome" not in intent


class TestResolveDirection:
    def test_negative_tc_resolves_error_selector(self):
        agent = _make_agent()
        intent = {
            "target_name": None, "target_kind": "assertion",
            "target_text": "중복 이메일 오류 메시지", "outcome": "negative",
        }
        resolved = agent._resolve_selector_from_intent(
            "assert", intent, [_SIGNUP_SUCCESS, _SIGNUP_ERROR], "/signup",
            "중복 이메일로 회원가입 시 오류",
        )
        assert resolved is not None
        assert resolved["selector"] == "signup-error"

    def test_positive_tc_resolves_success_selector(self):
        agent = _make_agent()
        intent = {
            "target_name": None, "target_kind": "assertion",
            "target_text": "회원가입 성공 메시지", "outcome": "positive",
        }
        resolved = agent._resolve_selector_from_intent(
            "assert", intent, [_SIGNUP_SUCCESS, _SIGNUP_ERROR], "/signup",
            "정상 회원가입 성공",
        )
        assert resolved is not None
        assert resolved["selector"] == "signup-success-toast"

    def test_positive_tc_does_not_fall_back_to_error_selector(self):
        """positive TC 인데 페이지에 error feedback 밖에 없으면 — error 로
        매핑하느니 미해결 (None) 이 정답 (then 절 page-wide fuzzy 로 검증)."""
        agent = _make_agent()
        intent = {
            "target_name": None, "target_kind": "assertion",
            "target_text": "로그인 성공", "outcome": "positive",
        }
        resolved = agent._resolve_selector_from_intent(
            "assert", intent, [_LOGIN_ERROR], "/login", "정상 로그인 성공",
        )
        assert resolved is None


class TestUnresolvedAssertKeepsThenText:
    def test_unresolved_assert_preserves_then_as_expected(self):
        agent = _make_agent()
        tc = {
            "tc_id": "TS-002-TC-01",
            "name": "정상 로그인",
            "given": "가입된 사용자",
            "when": "올바른 자격으로 로그인",
            "then": "로그인 성공 후 대시보드로 이동한다",
        }
        mapping = {
            "tc_id": "TS-002-TC-01",
            "steps": [
                {"step_no": 1, "action": "assert", "selector": None,
                 "selector_type": None, "value": None, "expected": None,
                 "api_endpoint": None},
            ],
        }
        result = agent._resolve_mapping_with_frontend(mapping, tc, [_LOGIN_ERROR])
        assert_step = [s for s in result["steps"] if s["action"] == "assert"][0]
        # positive outcome 인데 error selector 뿐 — 미해결 + then 절 보존
        assert assert_step["selector"] is None
        assert assert_step["expected"] == "로그인 성공 후 대시보드로 이동한다"
