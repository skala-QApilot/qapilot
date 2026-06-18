"""컨텍스트 밴딧 — Beta-Bernoulli Thompson Sampling (순수, 결정적 시드 지원).

각 '도메인 영역(area)'을 하나의 arm 으로 두고, 그 영역에서 시나리오가 제품결함을
잡으면 success, 아니면 failure 로 본다. Thompson Sampling 으로 "결함 적중률이 높을
법하면서도(활용) 아직 덜 시도된(탐험)" 영역을 우선·다양하게 고른다.

→ 설계서 §6.1 (1단계, LLM 미세조정 없이 즉시 적용 가능).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from qapilot.rl.schemas import AreaStat


@dataclass
class _Arm:
    alpha: float = 1.0   # success + 1 (Beta 사전분포)
    beta: float = 1.0    # failure + 1


@dataclass
class ThompsonBandit:
    """영역별 Beta-Bernoulli 밴딧.

    - `update(area, successes, failures)` 로 관측 반영.
    - `select(areas, n, seed)` 로 우선순위 영역 선택(탐험-활용).
    - `to_rows()/from_rows()` 로 영속화(DB) 직렬화.
    """

    arms: dict[str, _Arm] = field(default_factory=dict)
    prior_alpha: float = 1.0
    prior_beta: float = 1.0

    # ------------------------------------------------------------------
    def _arm(self, area: str) -> _Arm:
        arm = self.arms.get(area)
        if arm is None:
            arm = _Arm(alpha=self.prior_alpha, beta=self.prior_beta)
            self.arms[area] = arm
        return arm

    def update(self, area: str, successes: int = 0, failures: int = 0) -> None:
        arm = self._arm(area)
        arm.alpha += max(0, successes)
        arm.beta += max(0, failures)

    def update_from_stats(self, stats: dict[str, AreaStat]) -> None:
        for area, st in stats.items():
            self.update(area, successes=st.successes, failures=st.failures)

    # ------------------------------------------------------------------
    def posterior_mean(self, area: str) -> float:
        arm = self._arm(area)
        return arm.alpha / (arm.alpha + arm.beta)

    def sample_theta(self, area: str, rng: random.Random) -> float:
        arm = self._arm(area)
        return rng.betavariate(arm.alpha, arm.beta)

    def select(
        self,
        areas: list[str],
        n: int | None = None,
        seed: int | None = None,
    ) -> list[str]:
        """주어진 후보 영역을 Thompson 표본값 내림차순으로 정렬해 상위 n 개 반환.

        매번 사전분포에서 표본을 뽑으므로 같은 입력이라도(시드 없으면) 탐험성이 생긴다.
        `seed` 를 주면 결정적(테스트/재현용).
        """
        if not areas:
            return []
        rng = random.Random(seed)
        scored = [(self.sample_theta(a, rng), a) for a in dict.fromkeys(areas)]
        # 표본값 내림차순, 동률은 사후평균으로
        scored.sort(key=lambda x: (x[0], self.posterior_mean(x[1])), reverse=True)
        ordered = [a for _, a in scored]
        return ordered if n is None else ordered[:n]

    # ------------------------------------------------------------------
    def to_rows(self, service_id: str) -> list[dict]:
        return [
            {"service_id": service_id, "domain_area": area, "alpha": arm.alpha, "beta": arm.beta}
            for area, arm in self.arms.items()
        ]

    @classmethod
    def from_rows(cls, rows: list[dict]) -> ThompsonBandit:
        bandit = cls()
        for r in rows:
            bandit.arms[str(r["domain_area"])] = _Arm(
                alpha=float(r.get("alpha", 1.0)),
                beta=float(r.get("beta", 1.0)),
            )
        return bandit
