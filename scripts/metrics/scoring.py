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
    endpoint: str | None = None      # 'POST /api/orders' (관측 호출에서 유도)
    req_id: str | None = None
    fix_files: list[str] = field(default_factory=list)  # 원인 위치(fix_recommender rank순)
    cause_texts: list[str] = field(default_factory=list)  # 원인 의미(LLM-judge용)
    observed_status: int | None = None   # 관측 HTTP status (golden 오라클 대조)
    oracle_estimated: bool = False       # then 기대코드 미명시 → LLM 추정 여부

    @property
    def is_product_fail(self) -> bool:
        return self.verdict == "fail" and self.has_mismatch and self.decision == _PRODUCT


def _endpoint_from_calls(calls: list[dict]) -> tuple[str | None, int | None]:
    """관측 호출들에서 대표 endpoint('METHOD /path') + 마지막 status 유도."""
    import re as _re
    last_status = None
    ep = None
    for c in calls or []:
        sc = c.get("status_code")
        if sc is not None:
            last_status = sc
        url = c.get("url") or ""
        m = _re.search(r"https?://[^/]+(/[^?#]*)", url)
        path = m.group(1) if m else url
        meth = (c.get("method") or "").upper()
        # mutating(4xx/5xx 또는 비-GET) 호출을 대표로 선호
        if ep is None or (meth and meth != "GET"):
            ep = f"{meth} {path}".strip()
    return ep, last_status


def normalize_run(
    cross_check_results: list[dict],
    root_cause_results: list[dict],
    endpoint_by_tc: dict[str, str] | None = None,
    req_by_tc: dict[str, str] | None = None,
    fix_results: list[dict] | None = None,
    api_results: list[dict] | None = None,
) -> list[TCOutcome]:
    """pipeline 산출 → TCOutcome 리스트."""
    endpoint_by_tc = endpoint_by_tc or {}
    req_by_tc = req_by_tc or {}
    rc_by_tc = {rc.get("tc_id"): rc for rc in root_cause_results if rc.get("tc_id")}
    fix_by_tc = {fr.get("tc_id"): fr for fr in (fix_results or []) if fr.get("tc_id")}
    api_by_tc = {ar.get("tc_id"): ar for ar in (api_results or []) if ar.get("tc_id")}

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
        # ①장애유형 — 파이프라인이 root_cause 에 채운 defect_type(상태코드+도메인규칙) 우선,
        # 없으면(구버전 아티팩트) error_code prefix 로 폴백.
        if decision == _PRODUCT:
            defect_type = rc.get("defect_type") or _infer_defect_type(error_code)
        else:
            defect_type = None
        # 원인 위치: fix_recommender suggestions 의 file_path (rank 순)
        fr = fix_by_tc.get(tc_id) or {}
        fix_files = [s.get("file_path") for s in (fr.get("suggestions") or [])
                     if s.get("file_path")]
        # 원인 의미: root_cause 후보 cause 텍스트
        cause_texts = [c.get("cause") for c in (rc.get("candidates") or [])
                       if c.get("cause")]
        # 관측 endpoint/status
        ar = api_by_tc.get(tc_id) or {}
        ep_obs, obs_status = _endpoint_from_calls(ar.get("calls") or [])
        # 오라클 추정 여부: summary 에 'LLM 추정'/'미명시' 표지
        summary = str(cc.get("summary") or "")
        oracle_estimated = ("LLM 추정" in summary) or ("미명시" in summary)
        out.append(TCOutcome(
            tc_id=str(tc_id),
            verdict=verdict,
            has_mismatch=bool(cc.get("has_mismatch")),
            error_code=error_code,
            decision=decision,
            defect_type=defect_type,
            candidates=list(rc.get("candidates") or []),
            endpoint=endpoint_by_tc.get(str(tc_id)) or ep_obs,
            req_id=req_by_tc.get(str(tc_id)),
            fix_files=fix_files,
            cause_texts=cause_texts,
            observed_status=obs_status,
            oracle_estimated=oracle_estimated,
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
    """clean SUT → 전 케이스 PASS 기대. false-fail = 테스트 코드 부정확.

    두 수치 보고:
    - accuracy (전체): pass / total — QApilot 자체 오라클 기준(추정 포함).
    - accuracy_explicit: 오라클이 명시(then status)인 TC 한정 pass율 — 추정 노이즈 제거한
      '신뢰 가능 오라클' 기준의 정확도. (estimated 오라클 fail 은 오라클 미완성 이슈로 분리)
    """
    total = len(outcomes)
    if total == 0:
        return {"accuracy": 0.0, "n_total": 0, "n_pass": 0,
                "accuracy_explicit": None, "n_estimated_fail": 0, "false_fails": []}
    passes = [o for o in outcomes if o.verdict == "pass"]
    explicit = [o for o in outcomes if not o.oracle_estimated]
    explicit_pass = [o for o in explicit if o.verdict == "pass"]
    est_fail = [o for o in outcomes if o.verdict != "pass" and o.oracle_estimated]
    false_fails = [
        {"tc_id": o.tc_id, "decision": o.decision, "error_code": o.error_code,
         "oracle_estimated": o.oracle_estimated, "observed_status": o.observed_status}
        for o in outcomes if o.verdict != "pass"
    ]
    return {
        "accuracy": round(len(passes) / total, 4),
        "accuracy_explicit": round(len(explicit_pass) / len(explicit), 4) if explicit else None,
        "n_total": total, "n_pass": len(passes),
        "n_explicit": len(explicit), "n_estimated_fail": len(est_fail),
        "false_fails": false_fails,
    }


def golden_oracle_accuracy(outcomes: list[TCOutcome], golden) -> dict:
    """관측 status 가 golden 명시 status 와 일치하는 TC 비율 (정답 오라클 기준).

    golden 은 endpoint 가 없으므로 req_id→endpoint 매핑이 어렵다. 여기서는
    **관측된 (status) 가 golden 의 어떤 케이스 expected_status 집합에 속하는지**를
    엔드포인트 기반으로 join 하지 않고, 보조 신호로만 본다 — 정밀 join 은 후속.
    현재는 '관측 status 분포 vs golden expected_status 분포' 일치도를 리포트.
    """
    from collections import Counter
    obs = Counter(o.observed_status for o in outcomes if o.observed_status)
    gold = Counter(g.expected_status for g in golden.values() if g.expected_status)
    return {"observed_status_dist": dict(obs), "golden_status_dist": dict(gold),
            "note": "endpoint 기반 per-TC join 은 후속(현재는 분포 대조)"}


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


# ── 검출된 결함 = 측정 단위 ─────────────────────────────────────────
def detected_hits(fault_outcomes: list[TCOutcome], fault: FaultGT,
                  clean_outcomes: list[TCOutcome] | None = None) -> list[TCOutcome]:
    """결함 ON 에서 '진짜 검출된' product-fail = clean 에 없던 + (있으면)해당 endpoint.

    분류·원인 정확도는 이 '검출된 결함'에만 적용한다(clean 오탐 제외). 정답지(faults)와
    대조 가능한 단위가 곧 검출된 결함이기 때문.
    """
    clean_pf = {o.tc_id for o in (clean_outcomes or []) if o.is_product_fail}
    have_ep = any(o.endpoint for o in fault_outcomes)
    out = []
    for o in fault_outcomes:
        if not o.is_product_fail or o.tc_id in clean_pf:
            continue
        if have_ep and o.endpoint and not _exercises_fault(o, fault):
            continue
        out.append(o)
    return out


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
    # clean 에서 product-fail 인 TC = 오탐. 검출 신호에서 제외(delta).
    clean_pf = {o.tc_id for o in (clean_outcomes or []) if o.is_product_fail}
    have_ep = any(o.endpoint for o in fault_outcomes)
    hits = []
    for o in fault_outcomes:
        if not o.is_product_fail:
            continue
        if o.tc_id in clean_pf:          # clean 에서도 fail → 결함 신호 아님(오탐)
            continue
        if have_ep and o.endpoint and not _exercises_fault(o, fault):
            continue                     # endpoint 정보 있으면 해당 엔드포인트만
        hits.append(o)
    return {
        "detected": len(hits) > 0,
        "basis": "endpoint+delta" if have_ep else "delta_vs_clean",
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
def classification(fault_outcomes: list[TCOutcome], fault: FaultGT,
                   clean_outcomes: list[TCOutcome] | None = None) -> dict:
    """**검출된 결함**에 대해 ①장애유형·②결정분류 정확도 (clean 오탐 제외)."""
    hits = detected_hits(fault_outcomes, fault, clean_outcomes)
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
                    ns: tuple[int, ...] = (1, 3, 5),
                    clean_outcomes: list[TCOutcome] | None = None) -> dict:
    """**검출된 결함**의 원인 후보 중 정답 fix_file 이 Top-N 안에 드나 + MRR (clean 오탐 제외).

    여러 검출 TC 가 있으면 각 TC 별 최선 rank 중 가장 좋은 것을 결함 단위 대표로.
    """
    hits = detected_hits(fault_outcomes, fault, clean_outcomes)
    best_rank: int | None = None
    per_tc = []
    for o in hits:
        rank = None
        # 위치는 fix_recommender 의 file_path(rank 순). root_cause 후보엔 file_path 없음.
        for i, fp in enumerate(o.fix_files, start=1):
            if _file_matches(fp, fault.fix_file):
                rank = i
                break
        per_tc.append({"tc_id": o.tc_id, "rank": rank,
                       "n_fix_files": len(o.fix_files)})
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


# ── §3.5(b) 원인 의미 일치 — LLM-judge ──────────────────────────────
def root_cause_semantic(fault_outcomes: list[TCOutcome], fault,
                        llm_judge, clean_outcomes: list[TCOutcome] | None = None) -> dict:
    """**검출된 결함**의 root_cause cause 텍스트가 정답 root_cause 와 의미 일치하나.

    llm_judge(candidate_cause: str, gt_root_cause: str) -> bool 를 주입. None 이면 미실행.
    검출된 TC 중 후보 하나라도 의미 일치하면 그 TC 는 hit.
    """
    hits = detected_hits(fault_outcomes, fault, clean_outcomes)
    if not hits or llm_judge is None:
        return {"n": len(hits), "match_rate": None, "per_tc": []}
    per_tc, matched = [], 0
    for o in hits:
        ok = any(llm_judge(c, fault.root_cause) for c in o.cause_texts[:3])
        matched += 1 if ok else 0
        per_tc.append({"tc_id": o.tc_id, "match": ok,
                       "top_cause": (o.cause_texts or [None])[0]})
    return {"n": len(hits), "match_rate": round(matched / len(hits), 4),
            "per_tc": per_tc}


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
