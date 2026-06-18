"""RLService 엔드투엔드 테스트 (인메모리 — DB/Qdrant 불필요).

learn_from_run(run_outcome=...) 으로 DB 읽기를 건너뛰고, InMemoryExperienceStore +
인메모리 도메인 뱅크로 전체 학습→계획 루프를 검증한다.
"""

from __future__ import annotations

from qapilot.rl.domain_bank import DomainKnowledgeBank
from qapilot.rl.experience import InMemoryExperienceStore
from qapilot.rl.schemas import DefectOutcome, RunOutcome, TCOutcome
from qapilot.rl.service import RLService


def _service() -> RLService:
    return RLService(
        store=InMemoryExperienceStore(),
        bank=DomainKnowledgeBank(enable_qdrant=False),
    )


def _run() -> RunOutcome:
    return RunOutcome(
        run_id="run-1",
        service_id="svc",
        domain="telecom-bss",
        area_by_ts={"TS-1": "결제", "TS-2": "청구"},
        tcs=[
            TCOutcome("TS-1-TC-1", "TS-1", "cross_check", "pass"),
            TCOutcome("TS-2-TC-1", "TS-2", "cross_check", "fail"),
        ],
        defects=[
            DefectOutcome("TS-1-TC-1", "TS-1", "DATA_MISMATCH", 0.9, "결제 금액 불일치"),
        ],
    )


class TestLearnFromRun:
    def test_summary_ok(self):
        svc = _service()
        summary = svc.learn_from_run("run-1", service_id="svc", run_outcome=_run())
        assert summary["status"] == "ok"
        assert summary["scenarios"] == 2
        assert summary["product_defect_scenarios"] == 1
        assert summary["patterns_added"] == 1
        assert summary["experiences_recorded"] == 2
        # 결제 영역이 성공(결함검출), 청구는 실패
        assert summary["areas"]["결제"]["successes"] == 1
        assert summary["areas"]["청구"]["successes"] == 0

    def test_bandit_persisted_and_learns(self):
        store = InMemoryExperienceStore()
        svc = RLService(store=store, bank=DomainKnowledgeBank(enable_qdrant=False))
        svc.learn_from_run("run-1", service_id="svc", run_outcome=_run())
        bandit = store.load_bandit("svc")
        # 결제(성공) posterior > 청구(실패)
        assert bandit.posterior_mean("결제") > bandit.posterior_mean("청구")

    def test_patterns_stored_in_bank(self):
        bank = DomainKnowledgeBank(enable_qdrant=False)
        svc = RLService(store=InMemoryExperienceStore(), bank=bank)
        svc.learn_from_run("run-1", service_id="svc", run_outcome=_run())
        assert len(bank._mem) == 1
        assert bank._mem[0].area == "결제"


class TestPlan:
    def test_plan_prioritizes_learned_area(self):
        svc = _service()
        # 결제에 성공 학습을 반복 누적 → 우선순위 위로
        for i in range(5):
            svc.learn_from_run(f"run-{i}", service_id="svc", run_outcome=_run())
        plan = svc.plan(
            "svc",
            candidate_areas=["청구", "결제", "회원"],
            query="결제",
            domain="telecom-bss",
            seed=0,
        )
        assert set(plan.prioritized_areas) == {"청구", "결제", "회원"}
        assert plan.prioritized_areas[0] == "결제"  # 학습된 고적중 영역
        # 결제 영역 패턴 힌트가 retrieve 됨
        assert "결제" in plan.patterns_by_area
        hints = plan.as_prompt_hints()
        assert "결제" in hints

    def test_plan_empty_areas(self):
        svc = _service()
        plan = svc.plan("svc", candidate_areas=[])
        assert plan.prioritized_areas == []
        assert plan.notes  # "후보 영역 없음" 안내
