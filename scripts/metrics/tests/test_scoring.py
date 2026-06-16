"""scoring 핵심 산식 단위 검증 (설계서 §3.1~3.5, §4).

합성 데이터로 각 지표가 의도대로 계산되는지 고정. SUT/QApilot 불필요.
실행: cd scripts/metrics && python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scoring  # noqa: E402
from ground_truth import FaultGT  # noqa: E402


def tc(tc_id, verdict="pass", has_mismatch=False, error_code="", decision=None,
       candidates=None, endpoint=None, fix_files=None):
    return scoring.TCOutcome(
        tc_id=tc_id, verdict=verdict, has_mismatch=has_mismatch, error_code=error_code,
        decision=decision,
        defect_type=(scoring._infer_defect_type(error_code) if decision == scoring._PRODUCT else None),
        candidates=candidates or [], endpoint=endpoint, fix_files=fix_files or [])


RULE004 = FaultGT(
    id="BUG-RULE-004", category="RULE", defect_type="DOMAIN_RULE", severity="HIGH",
    endpoint="POST /api/orders", method="POST", path="/api/orders",
    fix_file="backend/app/routers/orders.py", fix_function="_validate_minor_plan_eligibility",
    root_cause="성인 청소년요금제 제한 우회", violated_requirement="약관 v3 제13조의2 4항")


# ── §3.1 유효 판정율 ────────────────────────────────────────────────
def test_valid_verdict_rate_excludes_skip_and_test_defect():
    outs = [
        tc("a", "pass"),
        tc("b", "fail", has_mismatch=True, error_code="RULE_X", decision=scoring._PRODUCT),  # 결함검출
        tc("c", "skip"),                                                                     # 제외
        tc("d", "fail", has_mismatch=True, decision="TEST_DEFECT_MAPPING"),                  # 제외(테스트결함)
    ]
    r = scoring.valid_verdict_rate(outs)
    assert r["n_pass"] == 1 and r["n_product_fail"] == 1
    assert r["rate"] == round(2 / 4, 4)


# ── §3.2 테스트 코드 정확도 (clean) ─────────────────────────────────
def test_test_code_accuracy_clean_counts_false_fails():
    outs = [tc("a", "pass"), tc("b", "pass"),
            tc("c", "fail", has_mismatch=True, decision="TEST_DEFECT_MAPPING")]
    r = scoring.test_code_accuracy_clean(outs)
    assert r["accuracy"] == round(2 / 3, 4)
    assert [f["tc_id"] for f in r["false_fails"]] == ["c"]


# ── §3.3 결함 검출 ──────────────────────────────────────────────────
def test_detection_by_endpoint():
    fault_run = [
        tc("o1", "fail", has_mismatch=True, error_code="RULE_MINOR",
           decision=scoring._PRODUCT, endpoint="POST /api/orders"),
        tc("o2", "pass", endpoint="GET /api/plans"),
    ]
    r = scoring.detection(fault_run, RULE004)
    assert r["detected"] is True and r["basis"] == "endpoint+delta" and r["hit_tc_ids"] == ["o1"]


def test_detection_fallback_delta_vs_clean():
    # endpoint 정보 없음 → clean 대비 신규 product-fail 로 검출
    clean = [tc("x", "pass")]
    fault_run = [tc("x", "fail", has_mismatch=True, error_code="RULE_X", decision=scoring._PRODUCT)]
    r = scoring.detection(fault_run, RULE004, clean_outcomes=clean)
    assert r["detected"] is True and r["basis"] == "delta_vs_clean"


def test_no_detection_when_only_test_defect():
    fault_run = [tc("o1", "fail", has_mismatch=True, decision="TEST_DEFECT_MAPPING",
                    endpoint="POST /api/orders")]
    assert scoring.detection(fault_run, RULE004)["detected"] is False


# ── §3.4 장애 분류 ①∧② ─────────────────────────────────────────────
def test_classification_both_correct():
    run = [tc("o1", "fail", has_mismatch=True, error_code="RULE_MINOR",
              decision=scoring._PRODUCT, endpoint="POST /api/orders")]
    r = scoring.classification(run, RULE004)
    assert r["type_acc"] == 1.0 and r["decision_acc"] == 1.0 and r["both_acc"] == 1.0


def test_classification_wrong_type():
    # decision 은 PRODUCT 맞지만 error_code prefix 가 API → defect_type 불일치
    run = [tc("o1", "fail", has_mismatch=True, error_code="API_500",
              decision=scoring._PRODUCT, endpoint="POST /api/orders")]
    r = scoring.classification(run, RULE004)
    assert r["decision_acc"] == 1.0 and r["type_acc"] == 0.0 and r["both_acc"] == 0.0


# ── §3.5 원인 추론 Top-N / MRR ──────────────────────────────────────
def test_root_cause_topn_and_mrr():
    # 위치는 fix_recommender 의 file_path(rank 순)로 채점
    fix = ["backend/app/routers/auth.py",            # 오답 rank1
           "backend/app/routers/orders.py"]          # 정답 rank2
    run = [tc("o1", "fail", has_mismatch=True, error_code="RULE_X",
              decision=scoring._PRODUCT, fix_files=fix, endpoint="POST /api/orders")]
    r = scoring.root_cause_topn(run, RULE004)
    assert r["best_rank"] == 2
    assert r["top1"] is False and r["top3"] is True and r["top5"] is True
    assert r["mrr"] == 0.5


def test_root_cause_basename_match():
    run = [tc("o1", "fail", has_mismatch=True, error_code="RULE_X",
              decision=scoring._PRODUCT, fix_files=["/abs/whatever/orders.py"],
              endpoint="POST /api/orders")]
    assert scoring.root_cause_topn(run, RULE004)["top1"] is True


# ── §4 비결정성 ─────────────────────────────────────────────────────
def test_consistency_pass_pow_k():
    assert scoring.consistency([True, True, True]) == {
        "n": 3, "pass_pow_k": True, "pass_at_k": True, "rate": 1.0}
    r = scoring.consistency([True, False, True])
    assert r["pass_pow_k"] is False and r["pass_at_k"] is True and r["rate"] == round(2/3, 4)


def test_infer_defect_type_none_without_clue():
    assert scoring._infer_defect_type("") is None
    assert scoring._infer_defect_type("RULE_MINOR") == "DOMAIN_RULE"
    assert scoring._infer_defect_type("API_500") == "API_ERROR"
