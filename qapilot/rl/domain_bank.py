"""도메인 지식 뱅크 (cross-domain) — §5.

도메인별 실패 패턴을 누적·검색한다.

- **패턴 추출**(`extract_patterns`): 완료된 런의 제품결함에서 재사용 가능한 실패
  패턴을 뽑는다 (순수 함수, 테스트 가능).
- **저장/검색**(`DomainKnowledgeBank`): Qdrant 컬렉션 `rl_domain_patterns` 에
  upsert / search. Qdrant·임베더가 없으면 **인메모리** 로 degrade (토큰 자카드 유사도).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from qapilot.rl.reward import is_product_defect
from qapilot.rl.schemas import (
    DomainPattern,
    RewardConfig,
    RunOutcome,
    ScenarioReward,
)

logger = logging.getLogger("qapilot.rl.domain_bank")

_COLLECTION = "rl_domain_patterns"
_EMBED_DIM = 1024  # BAAI/bge-m3 (도메인지식 Tool 과 동일)


# ---------------------------------------------------------------------------
# 패턴 추출 (순수)
# ---------------------------------------------------------------------------


def extract_patterns(
    run: RunOutcome,
    scenario_rewards: dict[str, ScenarioReward] | None = None,
    cfg: RewardConfig | None = None,
) -> list[DomainPattern]:
    """런의 제품결함에서 도메인 실패 패턴을 추출한다.

    하나의 제품결함 → 하나의 패턴 (domain, area, failure_type=category, 요약 text).
    같은 (area, category) 가 여러 번이면 대표 1건 + evidence 누적.
    """
    cfg = cfg or RewardConfig()
    domain = run.domain or run.service_id or "unknown"

    # (area, category) -> 대표 결함 + 증거 목록
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for d in run.defects:
        if not is_product_defect(d.category, cfg):
            continue
        area = run.area_of(d.ts_id)
        grouped[(area, d.category)].append(d)

    patterns: list[DomainPattern] = []
    for (area, category), defects in grouped.items():
        rep = max(defects, key=lambda x: x.root_cause_confidence)
        cause = (rep.root_cause_top1 or "").strip()
        text = f"[{area}] {category}" + (f" — {cause}" if cause else "")
        reward = 0.0
        if scenario_rewards:
            reward = max(
                (scenario_rewards[d.ts_id].total for d in defects if d.ts_id in scenario_rewards),
                default=0.0,
            )
        patterns.append(
            DomainPattern(
                domain=domain,
                area=area,
                failure_type=category,
                text=text,
                evidence={
                    "run_id": run.run_id,
                    "tc_id": rep.tc_id,
                    "ts_id": rep.ts_id,
                    "count": len(defects),
                },
                reward=round(reward, 6),
            )
        )
    return patterns


# ---------------------------------------------------------------------------
# 인메모리 유사도 (Qdrant 부재 시 fallback)
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {t for t in text.lower().replace("·", " ").replace("[", " ").replace("]", " ").split() if t}


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# 뱅크 (Qdrant + 인메모리 fallback)
# ---------------------------------------------------------------------------


@dataclass
class DomainKnowledgeBank:
    """도메인 패턴 저장/검색. Qdrant 우선, 없으면 인메모리."""

    qdrant_url: str | None = None
    enable_qdrant: bool = True
    _mem: list[DomainPattern] = field(default_factory=list)
    _client: object | None = None
    _embedder: object | None = None
    _qdrant_ready: bool = False

    def __post_init__(self) -> None:
        import os

        self.qdrant_url = self.qdrant_url or os.getenv("QDRANT_URL", "http://localhost:6333")
        if self.enable_qdrant:
            self._try_init_qdrant()

    # ------------------------------------------------------------------
    def _try_init_qdrant(self) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            client = QdrantClient(url=self.qdrant_url, timeout=5.0)
            existing = {c.name for c in client.get_collections().collections}
            if _COLLECTION not in existing:
                client.create_collection(
                    collection_name=_COLLECTION,
                    vectors_config=VectorParams(size=_EMBED_DIM, distance=Distance.COSINE),
                )
            self._client = client
            self._qdrant_ready = True
            logger.info("DomainKnowledgeBank: Qdrant 연결 (%s)", self.qdrant_url)
        except Exception as e:  # noqa: BLE001 — degrade gracefully
            self._client = None
            self._qdrant_ready = False
            logger.info("DomainKnowledgeBank: Qdrant 미사용 → 인메모리 fallback (%s)", e)

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            if self._embedder is None:
                from qapilot.tools.domain_knowledge._embedder import get_embedder

                self._embedder = get_embedder()
            vecs = self._embedder.encode(texts, normalize_embeddings=True)  # type: ignore[attr-defined]
            return [list(map(float, v)) for v in vecs]
        except Exception as e:  # noqa: BLE001
            logger.info("DomainKnowledgeBank: 임베더 미사용 (%s)", e)
            return None

    # ------------------------------------------------------------------
    def upsert_patterns(self, patterns: list[DomainPattern]) -> int:
        """패턴을 저장한다. 반환: 저장 건수."""
        if not patterns:
            return 0
        # 인메모리는 항상 갱신(검색 fallback·novelty 계산용)
        self._mem_upsert(patterns)

        if not self._qdrant_ready or self._client is None:
            return len(patterns)
        vecs = self._embed([p.text for p in patterns])
        if vecs is None:
            return len(patterns)
        try:
            from qdrant_client.models import PointStruct

            points = [
                PointStruct(
                    id=p.pattern_id(),
                    vector=vecs[i],
                    payload={
                        "domain": p.domain,
                        "area": p.area,
                        "failure_type": p.failure_type,
                        "text": p.text,
                        "reward": p.reward,
                        **p.evidence,
                    },
                )
                for i, p in enumerate(patterns)
            ]
            self._client.upsert(collection_name=_COLLECTION, points=points)  # type: ignore[attr-defined]
            return len(points)
        except Exception as e:  # noqa: BLE001
            logger.warning("DomainKnowledgeBank: Qdrant upsert 실패 → 인메모리만 (%s)", e)
            return len(patterns)

    def _mem_upsert(self, patterns: list[DomainPattern]) -> None:
        index = {p.pattern_id(): i for i, p in enumerate(self._mem)}
        for p in patterns:
            pid = p.pattern_id()
            if pid in index:
                self._mem[index[pid]] = p
            else:
                index[pid] = len(self._mem)
                self._mem.append(p)

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        domain: str | None = None,
        area: str | None = None,
        top_k: int = 5,
    ) -> list[DomainPattern]:
        """질의와 유사한 과거 실패 패턴을 반환."""
        if self._qdrant_ready and self._client is not None:
            vecs = self._embed([query])
            if vecs is not None:
                try:
                    flt = self._build_filter(domain, area)
                    resp = self._client.query_points(  # type: ignore[attr-defined]
                        collection_name=_COLLECTION,
                        query=vecs[0],
                        query_filter=flt,
                        limit=top_k,
                        with_payload=True,
                    )
                    return [self._hit_to_pattern(h) for h in resp.points]
                except Exception as e:  # noqa: BLE001
                    logger.warning("DomainKnowledgeBank: Qdrant search 실패 → 인메모리 (%s)", e)
        # 인메모리 fallback
        cand = [
            p
            for p in self._mem
            if (domain is None or p.domain == domain) and (area is None or p.area == area)
        ]
        cand.sort(key=lambda p: _jaccard(query, p.text), reverse=True)
        return cand[:top_k]

    def novelty(self, text: str, domain: str | None = None, area: str | None = None) -> float:
        """기존 패턴 대비 새로움(0~1). 1 = 완전히 새로움."""
        nearest = self.search(text, domain=domain, area=area, top_k=1)
        if not nearest:
            return 1.0
        return round(1.0 - _jaccard(text, nearest[0].text), 6)

    # ------------------------------------------------------------------
    def _build_filter(self, domain: str | None, area: str | None):
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            must = []
            if domain:
                must.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
            if area:
                must.append(FieldCondition(key="area", match=MatchValue(value=area)))
            return Filter(must=must) if must else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _hit_to_pattern(hit) -> DomainPattern:
        p = hit.payload or {}
        return DomainPattern(
            domain=str(p.get("domain", "")),
            area=str(p.get("area", "")),
            failure_type=str(p.get("failure_type", "")),
            text=str(p.get("text", "")),
            evidence={k: p[k] for k in ("run_id", "tc_id", "ts_id", "count") if k in p},
            reward=float(p.get("reward", 0.0)),
        )
