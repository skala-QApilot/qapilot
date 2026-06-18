"""RL 보상 계산 단위 테스트 (인프라 없이 순수 로직)."""

from __future__ import annotations

import pytest

from qapilot.rl.reward import (
    compute_area_stats,
    compute_scenario_rewards,
    is_product_defect,
)
from qapilot.rl.schemas import DefectOutcome, RewardConfig, RunOutcome, TCOutcome


def _sample_run() -> RunOutcome:
    return RunOutcome(
        run_id="run-1",
        service_id="svc",
        domain="telecom-bss",
        area_by_ts={"TS-1": "결제", "TS-2": "청구"},
        tcs=[
            TCOutcome("TS-1-TC-1", "TS-1", "ui", "pass"),
            TCOutcome("TS-1-TC-1", "TS-1", "api", "pass"),
            TCOutcome("TS-1-TC-1", "TS-1", "db", "pass"),
            TCOutcome("TS-1-TC-1", "TS-1", "cross_check", "pass", has_mismatch=True),
            TCOutcome("TS-2-TC-1", "TS-2", "cross_check", "fail"),
        ],
        defects=[
            DefectOutcome("TS-1-TC-1", "TS-1", "DATA_MISMATCH", root_cause_confidence=0.9),
            DefectOutcome("TS-2-TC-1", "TS-2", "TEST_DEFECT_MAPPING", root_cause_confidence=0.5),
        ],
    )


class TestProductDefectClassification:
    def test_product_categories(self):
        cfg = RewardConfig()
        assert is_product_defect("DATA_MISMATCH", cfg)
        assert is_product_defect("DOMAIN_RULE", cfg)
        assert is_product_defect("PRODUCT_DEFECT_CANDIDATE", cfg)  # 메트릭 트랙 명칭도 인정

    def test_non_product(self):
        cfg = RewardConfig()
        assert not is_product_defect("TEST_DEFECT_MAPPING", cfg)
        assert not is_product_defect("ENV_TIMEOUT", cfg)
        assert not is_product_defect("INFRA", cfg)
        assert not is_product_defect("", cfg)

    def test_whitelist_override(self):
        cfg = RewardConfig(product_defect_categories=frozenset({"DATA_MISMATCH"}))
        assert is_product_defect("DATA_MISMATCH", cfg)
        assert not is_product_defect("DOMAIN_RULE", cfg)  # 화이트리스트에 없으면 제외


class TestScenarioReward:
    def test_product_defect_scenario_positive(self):
        rewards = compute_scenario_rewards(_sample_run())
        ts1 = rewards["TS-1"]
        assert ts1.found_product_defect is True
        # defect 1.0 + conf 0.27 + verdict 0.2 + coverage 0.3 = 1.77
        assert ts1.components["defect"] == pytest.approx(1.27)
        assert ts1.components["verdict"] == pytest.approx(0.2)
        assert ts1.components["coverage"] == pytest.approx(0.3)
        assert ts1.total == pytest.approx(1.77)

    def test_test_defect_scenario_penalized(self):
        rewards = compute_scenario_rewards(_sample_run())
        ts2 = rewards["TS-2"]
        assert ts2.found_product_defect is False
        # defect -0.5 (테스트오탐) + verdict 0.2 (fail 확정) + coverage 0.075 = -0.225
        assert ts2.components["defect"] == pytest.approx(-0.5)
        assert ts2.components["verdict"] == pytest.approx(0.2)
        assert ts2.total == pytest.approx(-0.225)

    def test_unverified_penalty(self):
        run = RunOutcome(
            run_id="r",
            area_by_ts={"TS-9": "기타"},
            tcs=[TCOutcome("TS-9-TC-1", "TS-9", "cross_check", "unverified")],
        )
        rewards = compute_scenario_rewards(run)
        assert rewards["TS-9"].components["verdict"] == pytest.approx(-0.3)

    def test_novelty_injected(self):
        rewards = compute_scenario_rewards(_sample_run(), novelty_by_ts={"TS-1": 1.0})
        # novelty 0.4*1.0 추가 → 1.77 + 0.4 = 2.17
        assert rewards["TS-1"].total == pytest.approx(2.17)


class TestAreaStats:
    def test_aggregation(self):
        rewards = compute_scenario_rewards(_sample_run())
        stats = compute_area_stats(rewards)
        assert stats["결제"].successes == 1
        assert stats["결제"].trials == 1
        assert stats["청구"].successes == 0
        assert stats["청구"].trials == 1
        assert stats["청구"].failures == 1
