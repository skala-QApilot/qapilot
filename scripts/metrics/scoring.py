"""지표 산출 — 순수 함수. QApilot 산출물 ↔ ground truth 대조.

측정 설계서 §3 산식 구현:
- §3.1 유효 판정율            valid_verdict_rate
- §3.2 테스트 코드 정확도      test_code_accuracy_clean
- §3.3 결함 검출(Recall/Prec)  detection
- §3.4 장애 분류 ①∧②          classification
- §3.5 원인 추론 Top-N/MRR     root_cause_topn
- §4   비결정성 pass^k 등       consistency

QApilot run 한 건 입력 = (cross_check_results, root_cause_results). 둘 다 pipeline 이
뱉는 list[dict] (pipeline.py). normalize_run 으로 TCOutcome 리스트로 변환 후 채점.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field

from ground_truth import FaultGT, GoldenCase, fault_path_regex

# defect_writer._CATEGORY_PREFIX 와 동일 규약(①장애유형 산출). 동기 유지.
_CATEGORY_PREFIX = {
    "UI": "UI_ERROR", "API": "API_ERROR", "DATA": "DATA_MISMATCH",
    "DB": "DATA_MISMATCH", "INFRA": "INFRA", "DOMAIN": "DOMAIN_RULE", "RULE": "DOMAIN_RULE",
}
_PRODUCT = "PRODUCT_DEFECT_CANDIDATE"


def _infer_defect_type(error_code: str | None) -> str | None:
    """①장애유형 — error_code prefix. 단서 없으면 None (설계서 §3.4)."""
    if not error_code:
        return None
    return _CATEGORY_PREFIX.get(error_code.upper().split("_")[0])


@dataclass
class TCOutcome:
    """QApilot 가 한 TC 에 대해 낸 결과 (정규화)."""
    tc_id: str
    verdict: str                     # pass | fail | skip | unverified
    has_mismatch: bool
    error_code: str = ""
    decision: str | None = None      # ②결정분류 (root_cause category)
    defect_type: str | None = None   # ①장애유형 (error_code 유래)
    candidates: list[dict] = field(default_factory=list)
    endpoint: str | None = None      # 'POST /api/orders' 등 (있으면 결함 매핑 정밀)
    req_id: str | None = None

    @property
    def is_product_fail(self) -> bool:
        return self.verdict == "fail" and self.has_mismatch and self.decision == _PRODUCT


def normalize_run(
    cross_check_results: list[dict],
    root_cause_results: list[dict],
    endpoint_by_tc: dict[str, str] | None = None,
    req_by_tc: dict[str, str] | None = None,
) -> list[TCOutcome]:
    """pipeline 산출 → TCOutcome 리스트."""
    endpoint_by_tc = endpoint_by_tc or {}
    req_by_tc = req_by_tc or {}
    rc_by_tc = {rc.get("tc_id"): rc for rc in root_cause_results if rc.get("tc_id")}

    out: list[TCOutcome] = []
    for cc in cross_check_results:
        tc_id = cc.get("tc_id")
        if not tc_id:
            continue
        verdict = (cc.get("api_exec_verdict") or cc.get("verdict") or "").lower()
        if not verdict:
            verdict = "fail" if cc.get("has_mismatch") else "pass"
        error_code = cc.get("error_code") or ""
        rc = rc_by_tc.get(tc_id) or {}
        decision = rc.get("category")
        defect_type = (
            _infer_defect_type(error_code) if decision == _PRODUCT else None
        )
        out.append(TCOutcome(
            tc_id=str(tc_id),
            verdict=verdict,
            has_mismatch=bool(cc.get("has_mismatch")),
            error_code=error_code,
            decision=decision,
            defect_type=defect_type,
            candidates=list(rc.get("candidates") or []),
            endpoint=endpoint_by_tc.get(str(tc_id)),
            req_id=req_by_tc.get(str(tc_id)),
        ))
    return out


# ── §3.1 유효 판정율 ────────────────────────────────────────────────
def valid_verdict_rate(outcomes: list[TCOutcome]) -> dict:
    """(PASS + 결함검출 FAIL) / 전체. S(skip)/U(unverified)/테스트·환경 FAIL 제외."""
    total = len(outcomes)
    if total == 0:
        return {"rate": 0.0, "n_total": 0, "n_pass": 0, "n_product_fail": 0}
    n_pass = sum(1 for o in outcomes if o.verdict == "pass")
    n_pf = sum(1 for o in outcomes if o.is_product_fail)
    return {
        "rate": round((n_pass + n_pf) / total, 4),
        "n_total": total, "n_pass": n_pass, "n_product_fail": n_pf,
    }


# ── §3.2 테스트 코드 정확도 (clean SUT) ─────────────────────────────
def test_code_accuracy_clean(outcomes: list[TCOutcome]) -> dict:
    """clean SUT → 전 케이스 PASS 기대. false-fail = 테스트 코드 부정확."""
    total = len(outcomes)
    if total == 0:
        return {"accuracy": 0.0, "n_total": 0, "n_pass": 0, "false_fails": []}
    passes = [o for o in outcomes if o.verdict == "pass"]
    false_fails = [
        {"tc_id": o.tc_id, "verdict": o.verdict, "decision": o.decision,
         "error_code": o.error_code}
        for o in outcomes if o.verdict != "pass"
    ]
    return {
        "accuracy": round(len(passes) / total, 4),
        "n_total": total, "n_pass": len(passes), "false_fails": false_fails,
    }


# ── 결함 ↔ TC 매핑 ──────────────────────────────────────────────────
def _exercises_fault(o: TCOutcome, fault: FaultGT) -> bool:
    """TC 가 결함 endpoint 를 건드렸나. endpoint 정보 없으면 None→호출자가 fallback."""
    if not o.endpoint:
        return False
    parts = o.endpoint.strip().split(None, 1)
    method, path = (parts[0], parts[1]) if len(parts) == 2 else ("", o.endpoint)
    if fault.method and method and method.upper() != fault.method.upper():
        return False
    return bool(fault_path_regex(fault).search(path))


# ── §3.3 결함 검출 ──────────────────────────────────────────────────
def detection(
    fault_outcomes: list[TCOutcome],
    fault: FaultGT,
    clean_outcomes: list[TCOutcome] | None = None,
) -> dict:
    """결함 ON 에서 affected endpoint TC 가 PRODUCT_DEFECT 로 FAIL 했나.

    endpoint 정보가 있으면 endpoint 매칭, 없으면 clean 대비 신규 product-fail(delta)
    을 검출 신호로 사용(fallback). detected = bool, 근거 tc 리스트.
    """
    have_ep = any(o.endpoint for o in fault_outcomes)
    if have_ep:
        hits = [o for o in fault_outcomes if o.is_product_fail and _exercises_fault(o, fault)]
        basis = "endpoint"
    else:
        clean_pf = {o.tc_id for o in (clean_outcomes or []) if o.is_product_fail}
        hits = [o for o in fault_outcomes if o.is_product_fail and o.tc_id not in clean_pf]
        basis = "delta_vs_clean"
    return {
        "detected": len(hits) > 0,
        "basis": basis,
        "hit_tc_ids": [o.tc_id for o in hits],
        "n_hits": len(hits),
    }


def precision_from_clean(clean_outcomes: list[TCOutcome]) -> dict:
    """clean SUT 의 product-fail = false positive. Precision 보정용."""
    total_pf = sum(1 for o in clean_outcomes if o.is_product_fail)
    return {
        "false_positives": total_pf,
        "fp_tc_ids": [o.tc_id for o in clean_outcomes if o.is_product_fail],
    }


# ── §3.4 장애 분류 ①∧② ─────────────────────────────────────────────
def classification(fault_outcomes: list[TCOutcome], fault: FaultGT) -> dict:
    """검출된 product-fail 에 대해 ①장애유형·②결정분류 정확도."""
    hits = [o for o in fault_outcomes
            if o.verdict == "fail" and o.has_mismatch and (
                not o.endpoint or _exercises_fault(o, fault))]
    if not hits:
        return {"n": 0, "decision_acc": None, "type_acc": None, "both_acc": None, "detail": []}
    detail, d_ok, t_ok, b_ok = [], 0, 0, 0
    for o in hits:
        dec_ok = o.decision == _PRODUCT                      # ②
        typ_ok = (o.defect_type == fault.defect_type)        # ①
        d_ok += dec_ok; t_ok += typ_ok; b_ok += (dec_ok and typ_ok)
        detail.append({"tc_id": o.tc_id, "decision": o.decision, "decision_ok": dec_ok,
                       "defect_type": o.defect_type, "expected_type": fault.defect_type,
                       "type_ok": typ_ok})
    n = len(hits)
    return {
        "n": n,
        "decision_acc": round(d_ok / n, 4),
        "type_acc": round(t_ok / n, 4),
        "both_acc": round(b_ok / n, 4),
        "detail": detail,
    }


# ── §3.5 원인 추론 Top-N / MRR ──────────────────────────────────────
def _file_matches(candidate_path: str | None, fix_file: str) -> bool:
    """후보 file_path 가 정답 fix_file 과 일치(경로 표기 차이 허용 — suffix/basename)."""
    if not candidate_path or not fix_file:
        return False
    cp = candidate_path.replace("\\", "/").lower()
    ff = fix_file.replace("\\", "/").lower()
    return cp.endswith(ff) or ff.endswith(cp) or os.path.basename(cp) == os.path.basename(ff)


def root_cause_topn(fault_outcomes: list[TCOutcome], fault: FaultGT,
                    ns: tuple[int, ...] = (1, 3, 5)) -> dict:
    """검출 product-fail 후보 중 정답 fix_file 이 Top-N 안에 드나 + MRR.

    여러 검출 TC 가 있으면 각 TC 별 최선 rank 중 가장 좋은 것을 결함 단위 대표로.
    """
    hits = [o for o in fault_outcomes
            if o.is_product_fail and (not o.endpoint or _exercises_fault(o, fault))]
    best_rank: int | None = None
    per_tc = []
    for o in hits:
        rank = None
        for i, cand in enumerate(o.candidates, start=1):
            if _file_matches(cand.get("file_path"), fault.fix_file):
                rank = i
                break
        per_tc.append({"tc_id": o.tc_id, "rank": rank,
                       "n_candidates": len(o.candidates)})
        if rank is not None and (best_rank is None or rank < best_rank):
            best_rank = rank
    out = {f"top{n}": (best_rank is not None and best_rank <= n) for n in ns}
    out.update({
        "best_rank": best_rank,
        "mrr": round(1.0 / best_rank, 4) if best_rank else 0.0,
        "n_hits": len(hits),
        "per_tc": per_tc,
    })
    return out


# ── §4 비결정성 — N회 일관성 ────────────────────────────────────────
def consistency(bools: list[bool]) -> dict:
    """N회 불리언 결과 → pass^k(전회 성공) / pass@k(1회+ 성공) / 비율."""
    n = len(bools)
    if n == 0:
        return {"n": 0, "pass_pow_k": False, "pass_at_k": False, "rate": 0.0}
    s = sum(1 for b in bools if b)
    return {
        "n": n,
        "pass_pow_k": s == n,        # 항상 성공 (QA 도구 신뢰성)
        "pass_at_k": s >= 1,         # 한 번이라도 성공
        "rate": round(s / n, 4),
    }


def mean_std(values: list[float]) -> dict:
    if not values:
        return {"mean": 0.0, "std": 0.0, "n": 0}
    return {
        "mean": round(statistics.fmean(values), 4),
        "std": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
        "n": len(values),
    }
