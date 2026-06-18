"""강화학습(RL) 기반 시나리오 강화 모듈 (Phase 1).

설계서 `강화학습_시나리오강화_설계서.md` 의 P1 슬라이스 구현:

- **보상 서비스**(`reward`): 완료된 런의 `defects` / `tc_results` 로부터 시나리오·영역
  단위 보상을 계산한다 (LLM 가중치를 건드리지 않음).
- **경험 저장소**(`experience`): (영역, 행동, 보상) 경험과 밴딧 통계를 적재한다.
- **컨텍스트 밴딧**(`bandit`): Beta-Bernoulli Thompson Sampling 으로 "다음에 어떤
  도메인 영역을 우선·다양하게 칠지"를 선택한다.
- **도메인 지식 뱅크**(`domain_bank`): 도메인별 실패 패턴을 누적·검색한다 (cross-domain).
- **서비스**(`service`): 위를 묶어 `learn_from_run()` (사후 학습) 과 `plan()`
  (생성 전 강화 계획) 두 진입점을 제공한다.

모든 IO(DB / Qdrant / 임베딩)는 **그레이스풀하게 degrade** 한다 — 인프라가 없으면
인메모리/no-op 로 동작하므로, 순수 로직은 인프라 없이도 단위 테스트된다.
"""

from __future__ import annotations

from qapilot.rl.bandit import ThompsonBandit
from qapilot.rl.reward import (
    compute_area_stats,
    compute_scenario_rewards,
    is_product_defect,
)
from qapilot.rl.schemas import (
    AreaStat,
    DefectOutcome,
    DomainPattern,
    EnrichmentPlan,
    RewardConfig,
    RunOutcome,
    ScenarioReward,
    TCOutcome,
)
from qapilot.rl.service import RLService

__all__ = [
    "RLService",
    "ThompsonBandit",
    "RewardConfig",
    "RunOutcome",
    "TCOutcome",
    "DefectOutcome",
    "ScenarioReward",
    "AreaStat",
    "DomainPattern",
    "EnrichmentPlan",
    "compute_scenario_rewards",
    "compute_area_stats",
    "is_product_defect",
]
