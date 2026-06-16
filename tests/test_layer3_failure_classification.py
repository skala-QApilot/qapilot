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

    # api-mode: per-step status 없음 → 무조건 PRODUCT_DEFECT_CANDIDATE (검출 신호 보존).
    # '추정 오라클 vs 진짜 결함'은 신호만으론 구분 불가 → 측정의 clean-union delta 로 분리.
    def _api(self, summary):
        return _classify_failure(
            {"has_mismatch": True, "summary": summary},
            {"tc_id": "x", "status": "fail", "steps": [], "error": ""})[0]

    def test_api_mode_fail_is_product_candidate(self):
        # 결함이 잘못 성공시킨 신호(기대 4xx 실제 201)도 PRODUCT 로 보존(검출 가능)
        assert self._api("기대 status 400~499 — 실제 201") == "PRODUCT_DEFECT_CANDIDATE"
        assert self._api("observe: status 500 (기대 [401])") == "PRODUCT_DEFECT_CANDIDATE"


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


class TestDefectTypeInference:
    """①장애유형 — ②결정분류(category) 와 독립적으로 산출되어 둘 다 측정에 쓰인다."""

    def _defect_type(self, category, error_code):
        # insert_defects 의 ①장애유형 결정부 재현 (pool 없이).
        from qapilot.db import defect_writer as dw
        return (
            dw._infer_defect_type(error_code)
            if category in dw._PRODUCT_DECISIONS else None
        )

    def test_product_candidate_keeps_defect_type_from_error_code(self):
        # ②=PRODUCT_DEFECT_CANDIDATE 라도 ①장애유형은 error_code 로 보존된다.
        assert self._defect_type("PRODUCT_DEFECT_CANDIDATE", "RULE_MINOR") == "DOMAIN_RULE"
        assert self._defect_type("PRODUCT_DEFECT_CANDIDATE", "API_500") == "API_ERROR"
        assert self._defect_type("PRODUCT_DEFECT_CANDIDATE", "DATA_MISMATCH_X") == "DATA_MISMATCH"

    def test_test_and_env_decisions_have_no_defect_type(self):
        # 테스트/환경 분류는 제품 장애가 아니므로 ①장애유형 없음(None).
        assert self._defect_type("TEST_DEFECT_MAPPING", "UI_X") is None
        assert self._defect_type("ENV_TIMEOUT", "INFRA_X") is None

    def test_missing_error_code_yields_none_not_default(self):
        # 단서 없으면 임의 기본값(UI_ERROR) 으로 ① 채우지 않는다 — 측정 정확도 보호.
        assert self._defect_type("PRODUCT_DEFECT_CANDIDATE", None) is None


class TestDefectWriterAssigneeFromBlame:
    """defects.assignee — git_diff.blame 기반 담당자 추천 (pool 없이 순수 로직만 검증)."""

    def test_exact_file_match_uses_author_email(self):
        from qapilot.db import defect_writer as dw
        blame = [{"file": "app/services/payment_service.py", "author": "Alice",
                   "author_email": "alice@example.com"}]
        blame_map = dw._build_blame_map(blame)
        assignee = dw._resolve_assignee(blame_map, "app/services/payment_service.py", None)
        assert assignee == "alice@example.com"

    def test_falls_back_to_author_name_when_email_empty(self):
        from qapilot.db import defect_writer as dw
        blame = [{"file": "app/services/payment_service.py", "author": "Alice", "author_email": ""}]
        blame_map = dw._build_blame_map(blame)
        assignee = dw._resolve_assignee(blame_map, "app/services/payment_service.py", None)
        assert assignee == "Alice"

    def test_basename_match_when_path_prefix_differs(self):
        from qapilot.db import defect_writer as dw
        blame = [{"file": "src/app/services/payment_service.py", "author": "Alice",
                   "author_email": "alice@example.com"}]
        blame_map = dw._build_blame_map(blame)
        assignee = dw._resolve_assignee(blame_map, "app/services/payment_service.py", None)
        assert assignee == "alice@example.com"

    def test_no_match_returns_none(self):
        from qapilot.db import defect_writer as dw
        blame = [{"file": "app/services/payment_service.py", "author": "Alice",
                   "author_email": "alice@example.com"}]
        blame_map = dw._build_blame_map(blame)
        assert dw._resolve_assignee(blame_map, "app/other/unrelated.py", None) is None

    def test_blame_author_takes_precedence_over_blame_map(self):
        from qapilot.db import defect_writer as dw
        blame = [{"file": "app/services/payment_service.py", "author": "Alice",
                   "author_email": "alice@example.com"}]
        blame_map = dw._build_blame_map(blame)
        assignee = dw._resolve_assignee(blame_map, "app/services/payment_service.py", "bob@example.com")
        assert assignee == "bob@example.com"
