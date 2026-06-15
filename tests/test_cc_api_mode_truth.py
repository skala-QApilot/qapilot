"""run 04d5f79e 2차 감사 후속 — api-mode verdict 단일 진실 + 폴백 회귀.

발견 ①: cc 의 의도 재추론이 부재-긍정 then ("위약금이 부과되지 않는다",
"로그에 기록되지 않는다") 을 negative 로 오분류 → 404/409 를 '기대된 거부'
로 구제 — family 라우터 미배선 (진짜 제품 결함) 검출 5건이 pass 로 둔갑.
→ api-mode TC 의 cc status 는 exec 판정 (api_exec_verdict) 이 최종.

발견 ②: UI 가 검증을 못 끝낸 TC (skip 강등/steps=0) 가 U/F 로 사장 —
tc.api 보유 시 api-mode 폴백으로 측정 가능화.
"""
from __future__ import annotations

from qapilot.orchestrator.pipeline import _derive_cc_status


class TestCcApiModeTruth:
    def test_exec_fail_not_rescued_by_cc_pass_shape(self):
        # cc 가 구제 모양 (has_mismatch=False) 이어도 exec fail 이 최종
        cc = {"api_exec_verdict": "fail", "has_mismatch": False,
              "intent_satisfied": True, "match_score": 1.0}
        assert _derive_cc_status(cc) == "fail"

    def test_exec_pass_not_downgraded_by_gates(self):
        # UI 전제 게이트 (inputs/skip/unverified) 는 api-mode 에 부적용
        cc = {"api_exec_verdict": "pass", "inputs_incomplete": True,
              "ui_skipped": True, "db_unverified": True}
        assert _derive_cc_status(cc) == "pass"

    def test_non_api_mode_unchanged(self):
        assert _derive_cc_status({"has_mismatch": True}) == "fail"
        assert _derive_cc_status({"inputs_incomplete": True}) == "unverified"
        assert _derive_cc_status({"ui_skipped": True}) == "unverified"
        assert _derive_cc_status({"error_code": "CC_PARSE_FAIL"}) == "unverified"
        assert _derive_cc_status({}) == "pass"

    def test_absence_positive_then_regression(self):
        # 회귀 고정: TS-019-TC-03 시나리오 — exec fail (404) 인데 cc 가
        # "부과되지 않는다" 를 negative 오분류해 pass 구제하던 형태
        cc_rescued_shape = {
            "api_exec_verdict": "fail",  # 기대 2xx — 실제 404
            "has_mismatch": False,        # cc 오구제 결과
            "match_score": 1.0,
        }
        assert _derive_cc_status(cc_rescued_shape) == "fail"
