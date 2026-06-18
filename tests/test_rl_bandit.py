"""Thompson Sampling 밴딧 단위 테스트."""

from __future__ import annotations

from qapilot.rl.bandit import ThompsonBandit
from qapilot.rl.schemas import AreaStat


class TestThompsonBandit:
    def test_posterior_shifts_with_success(self):
        b = ThompsonBandit()
        b.update("결제", successes=8, failures=2)
        b.update("청구", successes=1, failures=9)
        assert b.posterior_mean("결제") > b.posterior_mean("청구")
        # 결제 = 9/(9+3)=0.75, 청구 = 2/(2+10)=0.166...
        assert round(b.posterior_mean("결제"), 3) == 0.75

    def test_update_from_stats(self):
        b = ThompsonBandit()
        b.update_from_stats(
            {
                "결제": AreaStat("결제", successes=3, trials=4),  # 1 failure
                "청구": AreaStat("청구", successes=0, trials=2),  # 2 failures
            }
        )
        # 결제 alpha=1+3=4, beta=1+1=2 ; 청구 alpha=1, beta=1+2=3
        assert b.posterior_mean("결제") > b.posterior_mean("청구")

    def test_select_deterministic_with_seed(self):
        b = ThompsonBandit()
        b.update("결제", successes=20, failures=1)
        b.update("청구", successes=1, failures=20)
        areas = ["청구", "결제", "회원"]
        first = b.select(areas, seed=42)
        second = b.select(areas, seed=42)
        assert first == second  # 같은 시드 → 결정적
        assert set(first) == set(areas)  # 모든 후보 보존
        # 결제가 압도적이므로 맨 앞
        assert first[0] == "결제"

    def test_select_topn(self):
        b = ThompsonBandit()
        for a in ["a", "b", "c", "d"]:
            b.update(a, successes=1)
        top2 = b.select(["a", "b", "c", "d"], n=2, seed=0)
        assert len(top2) == 2

    def test_select_empty(self):
        assert ThompsonBandit().select([]) == []

    def test_serialization_roundtrip(self):
        b = ThompsonBandit()
        b.update("결제", successes=5, failures=2)
        rows = b.to_rows("svc-1")
        assert rows[0]["service_id"] == "svc-1"
        restored = ThompsonBandit.from_rows(rows)
        assert restored.posterior_mean("결제") == b.posterior_mean("결제")
