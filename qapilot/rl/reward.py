"""보상 계산 (§4) — 순수 함수. DB/네트워크 의존 없음.

완료된 런의 관측(`RunOutcome`)으로부터 시나리오·영역 단위 보상을 계산한다.
보상 해킹 방지(§4.3): 확정 *제품결함* 에만 큰 보상, 보류/테스트오탐은 패널티,
중복·비용은 패널티.
"""

from __future__ import annotations

from collections import defaultdict

from qapilot.rl.schemas import (
    AreaStat,
    RewardConfig,
    RunOutcome,
    ScenarioReward,
)


def is_product_defect(category: str, cfg: RewardConfig) -> bool:
    """카테고리가 '제품결함'인가 (테스트오탐/환경결함 제외)."""
    cat = (category or "").upper()
    if cfg.product_defect_categories:
        return cat in {c.upper() for c in cfg.product_defect_categories}
    if cat in {c.upper() for c in cfg.test_defect_categories}:
        return False
    if cat in {c.upper() for c in cfg.env_categories}:
        return False
    return bool(cat)


def _modality_coverage(run: RunOutcome, ts_id: str) -> float:
    """해당 시나리오의 모달리티 커버리지(0~1). ui/api/db/cross_check 4축 중 몇 개를 덮었나."""
    modalities = {"ui", "api", "db", "cross_check"}
    seen = {t.kind for t in run.tcs if t.ts_id == ts_id and t.kind in modalities}
    return len(seen) / len(modalities) if modalities else 0.0


def compute_scenario_rewards(
    run: RunOutcome,
    cfg: RewardConfig | None = None,
    *,
    novelty_by_ts: dict[str, float] | None = None,
    cost_tokens_by_ts: dict[str, int] | None = None,
    hitl_approved_ts: set[str] | None = None,
    redundancy_by_ts: dict[str, float] | None = None,
) -> dict[str, ScenarioReward]:
    """시나리오(ts_id) 단위 보상을 계산한다.

    선택 신호(novelty / cost / hitl / redundancy)는 없으면 0 으로 둔다 — P1 은
    DB 에 이미 있는 결함·판정·커버리지 신호만으로도 동작한다.
    """
    cfg = cfg or RewardConfig()
    novelty_by_ts = novelty_by_ts or {}
    cost_tokens_by_ts = cost_tokens_by_ts or {}
    hitl_approved_ts = hitl_approved_ts or set()
    redundancy_by_ts = redundancy_by_ts or {}

    # 결함을 ts 별로 묶기
    defects_by_ts: dict[str, list] = defaultdict(list)
    for d in run.defects:
        defects_by_ts[d.ts_id].append(d)

    # cross-check 판정을 ts 별로 묶기
    cc_by_ts: dict[str, list] = defaultdict(list)
    for t in run.tcs:
        if t.is_cross_check:
            cc_by_ts[t.ts_id].append(t)

    rewards: dict[str, ScenarioReward] = {}
    for ts_id in sorted(run.ts_ids()):
        comp: dict[str, float] = {}
        found_product = False

        # (+) 결함 보상
        defect_score = 0.0
        for d in defects_by_ts.get(ts_id, []):
            if is_product_defect(d.category, cfg):
                found_product = True
                defect_score += cfg.w_product_defect
                defect_score += cfg.w_confidence * max(0.0, min(1.0, d.root_cause_confidence))
            elif d.category.upper() in {c.upper() for c in cfg.test_defect_categories}:
                # 테스트 자체 오탐 → 생성 품질 패널티
                defect_score -= cfg.p_test_defect
            # 환경결함(ENV/INFRA)은 중립(0)
        comp["defect"] = defect_score

        # (+/−) cross-check 판정 유효성
        verdict_score = 0.0
        for t in cc_by_ts.get(ts_id, []):
            if t.is_confirmed_verdict:
                verdict_score += cfg.w_verdict
            elif t.is_unverified:
                verdict_score -= cfg.p_unverified
        comp["verdict"] = verdict_score

        # (+) 커버리지
        comp["coverage"] = cfg.w_coverage * _modality_coverage(run, ts_id)

        # (+) novelty
        comp["novelty"] = cfg.w_novelty * max(0.0, min(1.0, novelty_by_ts.get(ts_id, 0.0)))

        # (+) HITL 승인
        comp["hitl"] = cfg.w_hitl if ts_id in hitl_approved_ts else 0.0

        # (−) 중복
        comp["redundancy"] = -cfg.p_redundancy * max(0.0, min(1.0, redundancy_by_ts.get(ts_id, 0.0)))

        # (−) 비용
        tokens = cost_tokens_by_ts.get(ts_id, 0)
        comp["cost"] = -cfg.p_cost_per_1k_tokens * (tokens / 1000.0)

        total = sum(comp.values())
        rewards[ts_id] = ScenarioReward(
            ts_id=ts_id,
            area=run.area_of(ts_id),
            total=round(total, 6),
            components={k: round(v, 6) for k, v in comp.items()},
            found_product_defect=found_product,
        )
    return rewards


def compute_area_stats(
    scenario_rewards: dict[str, ScenarioReward],
) -> dict[str, AreaStat]:
    """시나리오 보상을 도메인 영역 단위로 집계한다 (밴딧 arm 업데이트용).

    success = 제품결함을 잡은 시나리오. trial = 시도한 시나리오.
    """
    stats: dict[str, AreaStat] = {}
    for sr in scenario_rewards.values():
        st = stats.get(sr.area)
        if st is None:
            st = AreaStat(area=sr.area)
            stats[sr.area] = st
        st.trials += 1
        st.total_reward += sr.total
        if sr.found_product_defect:
            st.successes += 1
    return stats
