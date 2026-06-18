"""경험 저장소 + 밴딧 영속화.

- `record_experiences`: (영역, 행동, 보상) 경험을 `rl_experiences` 에 적재.
- `load_bandit` / `save_bandit`: `rl_bandit_arms` 로 밴딧 사후분포 보존/복원.

풀이 없으면 인메모리 fallback(`InMemoryExperienceStore`) 으로 degrade — 학습 로직은
인프라 없이도 단위 테스트된다.
"""

from __future__ import annotations

import json
import logging

from qapilot.rl.bandit import ThompsonBandit
from qapilot.rl.schemas import ScenarioReward

logger = logging.getLogger("qapilot.rl.experience")


def _get_pool():
    try:
        from qapilot.db.connection import get_pool

        return get_pool()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 경험 적재
# ---------------------------------------------------------------------------


def record_experiences(
    run_id: str,
    service_id: str | None,
    rewards: dict[str, ScenarioReward],
    action: str = "generate_scenario",
) -> int:
    """시나리오 보상을 rl_experiences 에 적재. 반환: 적재 행 수(풀 없으면 0)."""
    if not rewards:
        return 0
    pool = _get_pool()
    if pool is None:
        return 0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for sr in rewards.values():
                    cur.execute(
                        "INSERT INTO rl_experiences "
                        "(run_id, service_id, ts_id, domain_area, action, reward, components) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            run_id,
                            service_id,
                            sr.ts_id,
                            sr.area,
                            action,
                            sr.total,
                            json.dumps(sr.components, ensure_ascii=False),
                        ),
                    )
            conn.commit()
        return len(rewards)
    except Exception as e:  # noqa: BLE001
        logger.warning("RL: record_experiences 실패 (%s)", e)
        return 0


# ---------------------------------------------------------------------------
# 밴딧 영속화
# ---------------------------------------------------------------------------


def load_bandit(service_id: str) -> ThompsonBandit:
    """rl_bandit_arms 에서 밴딧 복원. 없으면 빈 밴딧."""
    pool = _get_pool()
    if pool is None:
        return ThompsonBandit()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT domain_area, alpha, beta FROM rl_bandit_arms WHERE service_id = %s",
                    (service_id,),
                )
                rows = [
                    {"domain_area": r[0], "alpha": r[1], "beta": r[2]} for r in cur.fetchall()
                ]
        return ThompsonBandit.from_rows(rows)
    except Exception as e:  # noqa: BLE001
        logger.info("RL: load_bandit 실패 → 빈 밴딧 (%s)", e)
        return ThompsonBandit()


def save_bandit(service_id: str, bandit: ThompsonBandit) -> bool:
    """밴딧 arm 상태를 upsert. 성공 시 True."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for row in bandit.to_rows(service_id):
                    cur.execute(
                        "INSERT INTO rl_bandit_arms (service_id, domain_area, alpha, beta, updated_at) "
                        "VALUES (%s, %s, %s, %s, now()) "
                        "ON CONFLICT (service_id, domain_area) "
                        "DO UPDATE SET alpha = EXCLUDED.alpha, beta = EXCLUDED.beta, updated_at = now()",
                        (service_id, row["domain_area"], row["alpha"], row["beta"]),
                    )
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("RL: save_bandit 실패 (%s)", e)
        return False


# ---------------------------------------------------------------------------
# 인메모리 store (인프라 없는 테스트/로컬용)
# ---------------------------------------------------------------------------


class InMemoryExperienceStore:
    """DB 없이 동작하는 경험 저장소 + 밴딧 보관 (테스트/로컬)."""

    def __init__(self) -> None:
        self.experiences: list[dict] = []
        self.bandits: dict[str, ThompsonBandit] = {}

    def record_experiences(
        self,
        run_id: str,
        service_id: str | None,
        rewards: dict[str, ScenarioReward],
        action: str = "generate_scenario",
    ) -> int:
        for sr in rewards.values():
            self.experiences.append(
                {
                    "run_id": run_id,
                    "service_id": service_id,
                    "ts_id": sr.ts_id,
                    "domain_area": sr.area,
                    "action": action,
                    "reward": sr.total,
                    "components": sr.components,
                }
            )
        return len(rewards)

    def load_bandit(self, service_id: str) -> ThompsonBandit:
        return self.bandits.setdefault(service_id, ThompsonBandit())

    def save_bandit(self, service_id: str, bandit: ThompsonBandit) -> bool:
        self.bandits[service_id] = bandit
        return True
