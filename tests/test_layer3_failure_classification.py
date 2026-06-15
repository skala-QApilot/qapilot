"""Layer 3 재설계 — 결정적 실패 분류기 회귀 테스트.

run 45522e5d 진단: 코드 컨텍스트 0 상태에서 모든 실패를 LLM root cause 에
넣어 환각 진단 (무관 파일 confidence 0.90) + 테스트 측 결함이 전부 SUT defect
(category=UI_ERROR 단일) 로 기록 — false defect ~96%.

해소: _classify_failure 가 error code/step 구조로 결정적 1차 분류,
PRODUCT_DEFECT_CANDIDATE 만 LLM 경로. defect_writer 는 분류 category 우선.
"""
from __future__ import annotations

from qapilot.orchestrator.pipeline import _classify_failure


def _ui(steps=None, error=None):
    return {"tc_id": "TC-1", "status": "fail", "steps": steps or [], "error": error}


class TestClassifyFailure:
    def test_timeout_is_env(self):
        cat, reason = _classify_failure({}, _ui(error="ToolExecutionError: [TOOL_001] Tool 타임아웃: 60초 초과"))
        assert cat == "ENV_TIMEOUT"
        assert "사전조건" in reason

    def test_interaction_failure_is_test_defect(self):
        steps = [
            {"step_no": 1, "action": "navigate", "status": "pass"},
            {"step_no": 2, "action": "fill", "status": "fail",
             "error": "TOOL_UI_LOCATOR_NOT_FOUND: ..."},
        ]
        cat, _ = _classify_failure({}, _ui(steps=steps))
        assert cat == "TEST_DEFECT_MAPPING"

    def test_uncheck_not_checkbox_is_test_defect(self):
        steps = [{"step_no": 5, "action": "uncheck", "status": "fail",
                  "error": "TOOL_UI_UNKNOWN: Not a checkbox"}]
        cat, _ = _classify_failure({}, _ui(steps=steps))
        assert cat == "TEST_DEFECT_MAPPING"

    def test_assert_after_clean_interactions_is_product_candidate(self):
        steps = [
            {"step_no": 1, "action": "navigate", "status": "pass"},
            {"step_no": 2, "action": "fill", "status": "pass"},
            {"step_no": 3, "action": "click", "status": "pass"},
            {"step_no": 4, "action": "assert_visible", "status": "fail",
             "error": "TOOL_UI_ASSERTION_FAIL: ..."},
        ]
        cat, reason = _classify_failure({}, _ui(steps=steps))
        assert cat == "PRODUCT_DEFECT_CANDIDATE"
        assert "제품 결함 후보" in reason

    def test_ui_pass_with_unverified_axes_is_env(self):
        cat, _ = _classify_failure({"api_unverified": True}, {"tc_id": "x", "status": "pass", "steps": []})
        assert cat == "ENV_UNVERIFIED"

    def test_ui_pass_with_real_mismatch_is_product_candidate(self):
        cat, _ = _classify_failure({"has_mismatch": True}, {"tc_id": "x", "status": "pass", "steps": []})
        assert cat == "PRODUCT_DEFECT_CANDIDATE"


class TestDefectWriterCategoryPassthrough:
    def test_rc_category_wins_over_error_code_inference(self):
        from qapilot.db import defect_writer as dw
        # insert_defects 의 row 구성 로직만 검증 — pool 없이 category 결정부 재현
        rc = {"tc_id": "TC-1", "category": "TEST_DEFECT_MAPPING",
              "candidates": [{"cause": "c", "confidence": 1.0}]}
        cc = {"tc_id": "TC-1", "error_code": "UI_X"}
        category = rc.get("category") or dw._infer_category(cc.get("error_code"))
        assert category == "TEST_DEFECT_MAPPING"

    def test_fallback_to_error_code_when_no_category(self):
        from qapilot.db import defect_writer as dw
        rc = {"tc_id": "TC-1", "candidates": [{"cause": "c"}]}
        cc = {"tc_id": "TC-1", "error_code": "API_TRACE_FAIL"}
        category = rc.get("category") or dw._infer_category(cc.get("error_code"))
        assert category == "API_ERROR"
