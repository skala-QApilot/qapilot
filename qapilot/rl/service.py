"""RLService — RL 모듈의 두 진입점.

- `learn_from_run(run_id, ...)`  : **사후 학습**. 완료된 런의 결함·판정으로 보상을
  계산해 밴딧·도메인 뱅크·경험 저장소를 갱신한다. (생성에 영향 X → 안전)
- `plan(service_id, candidate_areas, ...)` : **생성 전 강화 계획**. 밴딧으로 영역
  우선순위를 매기고, 도메인 뱅크에서 과거 실패 패턴을 retrieve 해 `EnrichmentPlan`
  으로 돌려준다. 파이프라인이 시나리오 생성 컨텍스트에 주입(opt-in seam).

설계서 §7·§8 의 "RL 메타루프" 를 코드로 옮긴 것.
"""

from __future__ import annotations

import logging

from qapilot.rl import db as rl_db
from qapilot.rl import experience as rl_exp
from qapilot.rl.domain_bank import DomainKnowledgeBank, extract_patterns
from qapilot.rl.reward import (
    compute_area_stats,
    compute_scenario_rewards,
    is_product_defect,
)
from qapilot.rl.schemas import EnrichmentPlan, RewardConfig, RunOutcome

logger = logging.getLogger("qapilot.rl.service")


class _DBStore:
    """모듈 함수(DB 경로)를 InMemoryExperienceStore 와 같은 인터페이스로 감싼다."""

    def record_experiences(self, run_id, service_id, rewards, action="generate_scenario"):
        return rl_exp.record_experiences(run_id, service_id, rewards, action)

    def load_bandit(self, service_id):
        return rl_exp.load_bandit(service_id)

    def save_bandit(self, service_id, bandit):
        return rl_exp.save_bandit(service_id, bandit)


class RLService:
    def __init__(
        self,
        store=None,
        bank: DomainKnowledgeBank | None = None,
        cfg: RewardConfig | None = None,
    ) -> None:
        self.store = store or _DBStore()
        self.bank = bank if bank is not None else DomainKnowledgeBank()
        self.cfg = cfg or RewardConfig()

    # ------------------------------------------------------------------
    # 사후 학습
    # ------------------------------------------------------------------
    def learn_from_run(
        self,
        run_id: str,
        service_id: str | None = None,
        domain: str | None = None,
        run_outcome: RunOutcome | None = None,
    ) -> dict:
        """완료된 런으로부터 학습. 요약 dict 반환."""
        rl_db.ensure_schema()  # best-effort (인메모리면 no-op)

        outcome = run_outcome or rl_db.fetch_run_outcome(run_id, service_id, domain)
        if outcome is None:
            return {"run_id": run_id, "status": "skipped", "reason": "관측을 읽을 수 없음(DB 미연결)"}

        domain = outcome.domain or domain or service_id

        # 1) novelty 를 먼저 측정 (뱅크 upsert 이전 = 사전 지식 대비 새로움)
        novelty_by_ts = self._novelty_by_ts(outcome, domain)

        # 2) 보상 계산
        rewards = compute_scenario_rewards(outcome, self.cfg, novelty_by_ts=novelty_by_ts)
        stats = compute_area_stats(rewards)

        # 3) 밴딧 갱신·저장
        bandit = self.store.load_bandit(service_id or "")
        bandit.update_from_stats(stats)
        self.store.save_bandit(service_id or "", bandit)

        # 4) 도메인 패턴 추출·뱅크 적재
        patterns = extract_patterns(outcome, rewards, self.cfg)
        patterns_added = self.bank.upsert_patterns(patterns)

        # 5) 경험 적재
        recorded = self.store.record_experiences(run_id, service_id, rewards)

        total_reward = round(sum(r.total for r in rewards.values()), 6)
        return {
            "run_id": run_id,
            "service_id": service_id,
            "status": "ok",
            "scenarios": len(rewards),
            "total_reward": total_reward,
            "product_defect_scenarios": sum(1 for r in rewards.values() if r.found_product_defect),
            "areas": {
                a: {
                    "successes": s.successes,
                    "trials": s.trials,
                    "mean_reward": round(s.mean_reward, 4),
                    "posterior": round(bandit.posterior_mean(a), 4),
                }
                for a, s in sorted(stats.items())
            },
            "patterns_added": patterns_added,
            "experiences_recorded": recorded,
        }

    def _novelty_by_ts(self, outcome: RunOutcome, domain: str | None) -> dict[str, float]:
        """제품결함을 잡은 시나리오의 새로움(뱅크 대비)을 계산."""
        out: dict[str, float] = {}
        by_ts: dict[str, list] = {}
        for d in outcome.defects:
            if is_product_defect(d.category, self.cfg):
                by_ts.setdefault(d.ts_id, []).append(d)
        for ts_id, defects in by_ts.items():
            rep = max(defects, key=lambda x: x.root_cause_confidence)
            area = outcome.area_of(ts_id)
            cause = (rep.root_cause_top1 or "").strip()
            text = f"[{area}] {rep.category}" + (f" — {cause}" if cause else "")
            try:
                out[ts_id] = self.bank.novelty(text, domain=domain, area=area)
            except Exception:  # noqa: BLE001
                out[ts_id] = 0.0
        return out

    # ------------------------------------------------------------------
    # 생성 전 강화 계획
    # ------------------------------------------------------------------
    def plan(
        self,
        service_id: str | None,
        candidate_areas: list[str],
        query: str | None = None,
        domain: str | None = None,
        n: int | None = None,
        patterns_per_area: int = 3,
        seed: int | None = None,
    ) -> EnrichmentPlan:
        """밴딧 우선순위 + 도메인 패턴 retrieve 로 강화 계획 생성."""
        bandit = self.store.load_bandit(service_id or "")
        prioritized = bandit.select(candidate_areas, n=n, seed=seed)

        patterns_by_area: dict[str, list[str]] = {}
        for area in prioritized:
            try:
                hits = self.bank.search(
                    query=query or area,
                    domain=domain or service_id,
                    area=area,
                    top_k=patterns_per_area,
                )
            except Exception:  # noqa: BLE001
                hits = []
            if hits:
                patterns_by_area[area] = [h.text for h in hits]

        notes = []
        if not prioritized:
            notes.append("후보 영역 없음 — 강화 계획 비어 있음")
        return EnrichmentPlan(
            service_id=service_id,
            domain=domain or service_id,
            prioritized_areas=prioritized,
            patterns_by_area=patterns_by_area,
            notes=notes,
        )
