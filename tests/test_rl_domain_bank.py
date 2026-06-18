"""도메인 지식 뱅크 (패턴 추출 + 인메모리 저장/검색/novelty) 테스트."""

from __future__ import annotations

from qapilot.rl.domain_bank import DomainKnowledgeBank, extract_patterns
from qapilot.rl.schemas import DefectOutcome, DomainPattern, RunOutcome


def _run_with_defects() -> RunOutcome:
    return RunOutcome(
        run_id="run-1",
        service_id="svc",
        domain="telecom-bss",
        area_by_ts={"TS-1": "결제", "TS-2": "청구"},
        defects=[
            DefectOutcome("TS-1-TC-1", "TS-1", "DATA_MISMATCH", 0.9, "결제 금액 불일치"),
            DefectOutcome("TS-1-TC-2", "TS-1", "DATA_MISMATCH", 0.7, "결제 금액 불일치(중복)"),
            DefectOutcome("TS-2-TC-1", "TS-2", "DOMAIN_RULE", 0.8, "청구 룰 위반"),
            DefectOutcome("TS-2-TC-2", "TS-2", "TEST_DEFECT_MAPPING", 0.5, "테스트 오탐"),
        ],
    )


class TestExtractPatterns:
    def test_groups_by_area_and_category(self):
        patterns = extract_patterns(_run_with_defects())
        # 제품결함만: (결제,DATA_MISMATCH), (청구,DOMAIN_RULE) → 2개. 테스트오탐 제외.
        assert len(patterns) == 2
        keys = {(p.area, p.failure_type) for p in patterns}
        assert ("결제", "DATA_MISMATCH") in keys
        assert ("청구", "DOMAIN_RULE") in keys

    def test_representative_uses_highest_confidence(self):
        patterns = extract_patterns(_run_with_defects())
        pay = next(p for p in patterns if p.area == "결제")
        assert "결제 금액 불일치" in pay.text
        assert pay.evidence["count"] == 2  # 같은 (area,cat) 2건 누적
        assert pay.domain == "telecom-bss"

    def test_pattern_id_deterministic(self):
        p = DomainPattern("d", "결제", "DATA_MISMATCH", "t", {"run_id": "r", "tc_id": "tc"})
        assert p.pattern_id() == p.pattern_id()


class TestInMemoryBank:
    def _bank(self) -> DomainKnowledgeBank:
        return DomainKnowledgeBank(enable_qdrant=False)  # 인메모리 강제

    def test_upsert_and_search(self):
        bank = self._bank()
        patterns = extract_patterns(_run_with_defects())
        added = bank.upsert_patterns(patterns)
        assert added == 2
        hits = bank.search("결제 금액", domain="telecom-bss", area="결제")
        assert hits and hits[0].area == "결제"

    def test_upsert_idempotent(self):
        bank = self._bank()
        patterns = extract_patterns(_run_with_defects())
        bank.upsert_patterns(patterns)
        bank.upsert_patterns(patterns)  # 같은 패턴 재적재
        assert len(bank._mem) == 2  # 중복 안 쌓임

    def test_novelty_high_for_unknown(self):
        bank = self._bank()
        # 비어있으면 무엇이든 새로움 1.0
        assert bank.novelty("[보안] AUTH_BYPASS — 토큰 우회") == 1.0

    def test_novelty_low_for_known(self):
        bank = self._bank()
        bank.upsert_patterns(extract_patterns(_run_with_defects()))
        nov = bank.novelty("[결제] DATA_MISMATCH — 결제 금액 불일치", domain="telecom-bss", area="결제")
        assert nov < 0.5  # 이미 아는 패턴 → 새로움 낮음

    def test_area_filter(self):
        bank = self._bank()
        bank.upsert_patterns(extract_patterns(_run_with_defects()))
        hits = bank.search("불일치", domain="telecom-bss", area="청구")
        assert all(h.area == "청구" for h in hits)
