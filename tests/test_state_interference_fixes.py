"""run feb0dc5e 전수 점검 후속 — 상태 간섭 3축 회귀 테스트.

축 ①: auth-negative TC ("비인증 → 차단 검증") 에 인증 fail-safe 가 발동해
       검증 전제 (비인증) 를 파괴 — test_account 미전달로 차단.
축 ②: 부재 기대 ("포함되지 않는다") assert 가 존재-검증으로 강제되고,
       empty-state 문구 ("...없습니다") 와 fuzzy 오결합 (스크린샷 실증:
       요금제 카탈로그 정상 표시 중 대시보드 empty-state 를 찾음).
축 ③: 서술형 then 텍스트 assert 실패가 PRODUCT 후보로 오염.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from qapilot.agents.action_mapper_agent import ActionMapperAgent
from qapilot.agents.code_generator_agent import CodeGeneratorAgent
from qapilot.orchestrator.pipeline import _classify_failure, _is_auth_negative_tc


class TestAuthNegativeDetection:
    def test_auth_tag_with_block_then(self):
        assert _is_auth_negative_tc("인증 오류가 발생한다.", ["auth"]) is True
        assert _is_auth_negative_tc("로그인이 요구된다.", ["auth"]) is True
        assert _is_auth_negative_tc("인증이 필요하다는 메시지가 표시된다.", ["auth"]) is True

    def test_jwt_negative_without_auth_tag(self):
        assert _is_auth_negative_tc("인증 실패 처리되고 자동 로그아웃된다.", ["edge_case"]) is True

    def test_auth_positive_keeps_recovery(self):
        # auth 태그라도 positive (인증된 조회) 는 복구 유지
        assert _is_auth_negative_tc("사용자의 이메일, 이름이 표시된다.", ["normal", "auth"]) is False

    def test_non_auth_negative_keeps_recovery(self):
        assert _is_auth_negative_tc("입력 오류가 표시된다.", ["edge_case"]) is False


class TestAbsenceIntentUnresolvable:
    def _agent(self):
        agent = ActionMapperAgent.__new__(ActionMapperAgent)
        agent.logger = MagicMock()
        return agent

    EMPTY_STATE = {"route": "/dashboard", "tag": "div", "control_type": "text",
                   "testid": "", "text": "현재 이용 중인 요금제가 없습니다.",
                   "label": "", "placeholder": "", "actionable": False}

    def test_absence_then_resolves_to_none(self):
        agent = self._agent()
        intent = agent._infer_step_intent(
            {"action": "assert", "selector": "", "expected": ""},
            {"then": "비활성 요금제는 목록에 포함되지 않는다."}, "",
        )
        assert intent.get("expects_absence")
        resolved = agent._resolve_selector_from_intent(
            "assert", intent, [self.EMPTY_STATE], "/plans", "",
        )
        assert resolved is None

    def test_empty_state_text_disqualified_for_presence_intent(self):
        agent = self._agent()
        intent = {"target_kind": "assertion", "target_text": "요금제 목록이 표시된다",
                  "outcome": "positive"}
        score = agent._intent_match_score("assert", intent, self.EMPTY_STATE, "", "/dashboard")
        assert score == 0.0

    def test_empty_state_allowed_when_intent_expects_emptiness(self):
        agent = self._agent()
        intent = {"target_kind": "assertion", "target_text": "이용 중인 요금제가 없다는 안내",
                  "outcome": "positive"}
        score = agent._intent_match_score("assert", intent, self.EMPTY_STATE, "", "/dashboard")
        assert score > 0.0


class TestAbsenceThenNotDowngradedToGetByText:
    def test_absence_then_stays_manual_review(self):
        agent = CodeGeneratorAgent.__new__(CodeGeneratorAgent)
        agent.logger = MagicMock()
        line = agent._render_step({
            "action": "assert", "selector": None, "selector_type": None,
            "expected": "비활성 요금제는 목록에 포함되지 않는다.", "value": None,
        })
        text = line if isinstance(line, str) else "\n".join(line)
        assert "getByText" not in text
        assert "QAPILOT_MANUAL_REVIEW" in text


class TestSentenceAssertClassification:
    def test_sentence_text_assert_fail_is_test_defect(self):
        ui = {"tc_id": "x", "status": "fail", "steps": [
            {"step_no": 1, "action": "navigate", "status": "pass"},
            {"step_no": 2, "action": "assert", "status": "fail",
             "selector": "인증 실패 처리되고 자동 로그아웃된다.",
             "error": "TOOL_UI_ASSERTION_FAIL: ..."},
        ]}
        cat, reason = _classify_failure({}, ui)
        assert cat == "TEST_DEFECT_UNVERIFIABLE"
        assert "표현력 한계" in reason

    def test_short_testid_assert_fail_still_product_candidate(self):
        ui = {"tc_id": "x", "status": "fail", "steps": [
            {"step_no": 1, "action": "click", "status": "pass"},
            {"step_no": 2, "action": "assert", "status": "fail",
             "selector": "signup-error",
             "error": "TOOL_UI_ASSERTION_FAIL: ..."},
        ]}
        cat, _ = _classify_failure({}, ui)
        assert cat == "PRODUCT_DEFECT_CANDIDATE"
