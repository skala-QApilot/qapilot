"""RL 모듈 공용 데이터 구조 (순수 dataclass, IO 의존 없음)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 환경(런)에서 관측되는 원시 결과 — DB 행을 그대로 담는다
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TCOutcome:
    """`tc_results` 한 행. kind ∈ {ui, api, db, cross_check}, status ∈ {pass, fail, unverified}."""

    tc_id: str
    ts_id: str
    kind: str
    status: str | None = None
    has_mismatch: bool = False

    @property
    def is_cross_check(self) -> bool:
        return self.kind == "cross_check"

    @property
    def is_confirmed_verdict(self) -> bool:
        """보류(unverified)로 새지 않고 확정 판정에 도달했는가."""
        return (self.status or "").lower() in {"pass", "fail"}

    @property
    def is_unverified(self) -> bool:
        return (self.status or "").lower() == "unverified"


@dataclass(frozen=True)
class DefectOutcome:
    """`defects` 한 행."""

    tc_id: str
    ts_id: str
    category: str
    root_cause_confidence: float = 0.0
    root_cause_top1: str | None = None


@dataclass
class RunOutcome:
    """한 런(run_id)의 전체 관측. 보상 계산의 입력."""

    run_id: str
    service_id: str | None = None
    domain: str | None = None
    tcs: list[TCOutcome] = field(default_factory=list)
    defects: list[DefectOutcome] = field(default_factory=list)
    # ts_id -> 도메인 영역(domain_area). 시나리오 payload 에서 추출.
    area_by_ts: dict[str, str] = field(default_factory=dict)

    def ts_ids(self) -> set[str]:
        ids: set[str] = set()
        ids.update(t.ts_id for t in self.tcs)
        ids.update(d.ts_id for d in self.defects)
        ids.update(self.area_by_ts.keys())
        return {i for i in ids if i}

    def area_of(self, ts_id: str) -> str:
        return self.area_by_ts.get(ts_id) or "unknown"

    # ----- 편의 생성자: DB 행(dict) 으로부터 -----
    @classmethod
    def from_rows(
        cls,
        run_id: str,
        tc_rows: list[dict[str, Any]],
        defect_rows: list[dict[str, Any]],
        area_by_ts: dict[str, str] | None = None,
        service_id: str | None = None,
        domain: str | None = None,
    ) -> RunOutcome:
        tcs: list[TCOutcome] = []
        for r in tc_rows:
            payload = r.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            tcs.append(
                TCOutcome(
                    tc_id=str(r.get("tc_id") or ""),
                    ts_id=str(r.get("ts_id") or ""),
                    kind=str(r.get("kind") or ""),
                    status=r.get("status"),
                    has_mismatch=bool(payload.get("has_mismatch", False)),
                )
            )
        defects: list[DefectOutcome] = []
        for r in defect_rows:
            conf = r.get("root_cause_confidence")
            defects.append(
                DefectOutcome(
                    tc_id=str(r.get("tc_id") or ""),
                    ts_id=str(r.get("ts_id") or ""),
                    category=str(r.get("category") or ""),
                    root_cause_confidence=float(conf) if conf is not None else 0.0,
                    root_cause_top1=r.get("root_cause_top1"),
                )
            )
        return cls(
            run_id=run_id,
            service_id=service_id,
            domain=domain,
            tcs=tcs,
            defects=defects,
            area_by_ts=dict(area_by_ts or {}),
        )


# ---------------------------------------------------------------------------
# 보상 설정 (§4 보상 함수의 가중치)
# ---------------------------------------------------------------------------


@dataclass
class RewardConfig:
    """보상 가중치/패널티. 모두 양수로 두고 부호는 보상 함수가 적용한다."""

    # (+) 항목
    w_product_defect: float = 1.0       # 확정 제품결함 1건당
    w_confidence: float = 0.3           # 원인추론 신뢰도(0~1) 가중
    w_verdict: float = 0.2              # cross-check 확정 판정 1건당
    w_coverage: float = 0.3             # 모달리티 커버리지(0~1)
    w_novelty: float = 0.4              # 도메인 뱅크 대비 새로움(0~1)
    w_hitl: float = 0.5                 # HITL 승인

    # (−) 항목
    p_test_defect: float = 0.5          # 테스트 자체 결함(오탐) 1건당
    p_unverified: float = 0.3           # 보류(검증 못함) 1건당
    p_redundancy: float = 0.3           # 중복도(0~1)
    p_cost_per_1k_tokens: float = 0.0001  # 토큰 비용

    # 결함 카테고리 분류 — 라이브 택소노미에 강건하도록 집합으로 관리.
    # (메트릭 트랙은 'PRODUCT_DEFECT_CANDIDATE' 를 쓰고, defect_writer CHECK 는
    #  UI_ERROR/API_ERROR/DATA_MISMATCH/DOMAIN_RULE 등을 쓴다. 둘 다 '제품결함'으로 본다.)
    # (라이브 DB 실측 카테고리 기준: PRODUCT_DEFECT_CANDIDATE/UI_ERROR/API_ERROR/
    #  DATA_MISMATCH/DOMAIN_RULE = 제품결함 ; 아래 = 테스트오탐/환경결함)
    test_defect_categories: frozenset[str] = frozenset(
        {"TEST_DEFECT_MAPPING", "TEST_DEFECT_UNVERIFIABLE"}
    )
    env_categories: frozenset[str] = frozenset(
        {"ENV_TIMEOUT", "ENV_UNVERIFIED", "INFRA"}
    )
    # 명시적 제품결함 화이트리스트(있으면 이것만 제품결함으로 인정). 비우면
    # "test/env 가 아닌 모든 카테고리 = 제품결함" 규칙을 쓴다.
    product_defect_categories: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# 보상 계산 결과
# ---------------------------------------------------------------------------


@dataclass
class ScenarioReward:
    """시나리오(ts_id) 단위 보상."""

    ts_id: str
    area: str
    total: float
    components: dict[str, float] = field(default_factory=dict)
    found_product_defect: bool = False


@dataclass
class AreaStat:
    """도메인 영역 단위 집계 — 밴딧 arm 업데이트에 사용."""

    area: str
    successes: int = 0      # 제품결함을 잡은 시나리오 수
    trials: int = 0         # 시도한 시나리오 수
    total_reward: float = 0.0

    @property
    def failures(self) -> int:
        return max(self.trials - self.successes, 0)

    @property
    def mean_reward(self) -> float:
        return self.total_reward / self.trials if self.trials else 0.0


# ---------------------------------------------------------------------------
# 도메인 지식 뱅크 / 강화 계획
# ---------------------------------------------------------------------------


@dataclass
class DomainPattern:
    """도메인별 실패 패턴 (cross-domain 축적 단위)."""

    domain: str
    area: str
    failure_type: str               # 예: DATA_MISMATCH, DOMAIN_RULE ...
    text: str                       # 검색/임베딩 대상 자연어 요약
    evidence: dict[str, Any] = field(default_factory=dict)  # {run_id, tc_id, ts_id}
    reward: float = 0.0

    def pattern_id(self) -> str:
        """결정적 UUID — 같은 (domain, area, failure_type, evidence) 는 같은 ID.

        Qdrant 포인트 ID 는 unsigned int 또는 UUID 만 허용하므로 uuid5 를 쓴다
        (해시 hex 는 거부됨). 결정적이라 idempotent upsert 가 보장된다.
        """
        import uuid

        raw = "|".join(
            [
                self.domain,
                self.area,
                self.failure_type,
                str(self.evidence.get("run_id", "")),
                str(self.evidence.get("tc_id", "")),
            ]
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


@dataclass
class EnrichmentPlan:
    """생성 전 정책이 내놓는 강화 계획 — 파이프라인이 시나리오 생성 컨텍스트에 주입."""

    service_id: str | None
    domain: str | None
    # 밴딧이 우선순위를 매긴 도메인 영역 (앞쪽일수록 먼저/풍부하게)
    prioritized_areas: list[str] = field(default_factory=list)
    # 영역별로 retrieve 된 과거 실패 패턴(자연어 힌트)
    patterns_by_area: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_prompt_hints(self) -> str:
        """생성 LLM 프롬프트에 끼워넣을 수 있는 자연어 힌트 블록."""
        lines: list[str] = []
        if self.prioritized_areas:
            lines.append("우선 검증 영역(결함 적중률 높은 순): " + ", ".join(self.prioritized_areas))
        for area, pats in self.patterns_by_area.items():
            if pats:
                lines.append(f"[{area}] 이 도메인에서 과거 발견된 실패 유형 — 반드시 엣지로 포함:")
                lines.extend(f"  · {p}" for p in pats)
        return "\n".join(lines)
