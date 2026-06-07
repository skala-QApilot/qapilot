"""시나리오 벡터 인덱스 — Qdrant scenario_index 컬렉션.

TS/TC 제목을 BGE-M3로 사전 임베딩해 저장하고, 챗봇 쿼리 시 벡터 검색으로
매칭 후보를 반환한다. 인메모리 재인코딩 방식 대비 요청당 encode 횟수를 절감한다.

컬렉션 구조:
  - TS 레벨: id = "{service_id}_{ts_id}"
             vector = BGE-M3(ts_title)
             payload = { service_id, ts_id, title, level: "ts" }
  - TC 레벨: id = "{service_id}_{ts_id}_{tc_id}"
             vector = BGE-M3(tc_title)
             payload = { service_id, ts_id, tc_id, title, level: "tc" }

업데이트 트리거: _save_scenarios (pipeline), POST /api/scenarios/reindex (Spring CRUD)

Author: A
Created: 2026-06-05
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from qapilot.shared.logger import get_logger
from qapilot.tools.domain_knowledge._embedder import EMBED_DIM, get_embedder

_COLLECTION = "scenario_index"
_logger = get_logger("scenario_index")


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


def _point_id(service_id: str, ts_id: str, tc_id: str | None = None) -> str:
    """결정론적 UUID — 같은 (service_id, ts_id, tc_id) 조합은 항상 동일 ID."""
    key = f"{service_id}:{ts_id}:{tc_id or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


async def _ensure_collection() -> bool:
    """컬렉션이 없으면 생성한다. 실패 시 False 반환."""
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import Distance, VectorParams

        client = AsyncQdrantClient(url=_qdrant_url())
        collections = await client.get_collections()
        existing = {c.name for c in collections.collections}
        if _COLLECTION not in existing:
            await client.create_collection(
                collection_name=_COLLECTION,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )
            _logger.info("scenario_index_collection_created", name=_COLLECTION)
        await client.close()
        return True
    except Exception as e:
        _logger.warning("scenario_index_collection_error", error=str(e))
        return False


class ScenarioVectorStore:
    """시나리오 TS/TC의 벡터 인덱스를 Qdrant에서 관리한다."""

    # ── 인덱싱 ──────────────────────────────────────────────────────────────

    async def upsert_scenario(self, service_id: str, ts: dict) -> bool:
        """TS 1개와 그 하위 TC 전체를 Qdrant에 upsert한다.

        기존 TS/TC 벡터는 덮어씌워지므로 수정·삭제 후 재호출해도 멱등하다.
        """
        if not service_id or not ts.get("ts_id"):
            return False
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import PointStruct

            embedder = get_embedder()
            ts_id = ts["ts_id"]
            tcs = ts.get("test_cases") or []

            # 인코딩할 텍스트 목록 구성
            ts_title = ts.get("title") or ts.get("name") or ts_id
            tc_items = [
                (tc.get("tc_id", ""), tc.get("name") or tc.get("title") or tc.get("tc_id", ""))
                for tc in tcs
                if tc.get("tc_id")
            ]
            texts = [ts_title] + [title for _, title in tc_items]

            vecs = await asyncio.to_thread(
                embedder.encode, texts, normalize_embeddings=True
            )

            points: list[PointStruct] = []
            # TS 포인트
            points.append(PointStruct(
                id=_point_id(service_id, ts_id),
                vector=vecs[0].tolist(),
                payload={"service_id": service_id, "ts_id": ts_id, "title": ts_title, "level": "ts"},
            ))
            # TC 포인트
            for i, (tc_id, tc_title) in enumerate(tc_items, start=1):
                points.append(PointStruct(
                    id=_point_id(service_id, ts_id, tc_id),
                    vector=vecs[i].tolist(),
                    payload={
                        "service_id": service_id,
                        "ts_id": ts_id,
                        "tc_id": tc_id,
                        "title": tc_title,
                        "level": "tc",
                    },
                ))

            await _ensure_collection()
            client = AsyncQdrantClient(url=_qdrant_url())
            await client.upsert(collection_name=_COLLECTION, points=points)
            await client.close()
            _logger.info("scenario_index_upsert", ts_id=ts_id, ts=1, tcs=len(tc_items))
            return True
        except Exception as e:
            _logger.warning("scenario_index_upsert_failed", error=str(e))
            return False

    async def delete_scenario(self, service_id: str, ts_id: str, tc_ids: list[str]) -> bool:
        """TS와 그 하위 TC 포인트를 Qdrant에서 삭제한다."""
        if not service_id or not ts_id:
            return False
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import PointIdsList

            ids = [_point_id(service_id, ts_id)] + [
                _point_id(service_id, ts_id, tc_id) for tc_id in tc_ids
            ]
            client = AsyncQdrantClient(url=_qdrant_url())
            await client.delete(
                collection_name=_COLLECTION,
                points_selector=PointIdsList(points=ids),
            )
            await client.close()
            return True
        except Exception as e:
            _logger.warning("scenario_index_delete_failed", error=str(e))
            return False

    # ── 검색 ──────────────────────────────────────────────────────────────

    async def search_ts(
        self,
        service_id: str,
        query_vec: list[float],
        top_n: int = 5,
        threshold: float = 0.40,
    ) -> list[dict]:
        """TS 레벨에서 유사 시나리오를 검색한다.

        Returns:
            [{ ts_id, title, _similarity }, ...] (유사도 내림차순)
        """
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            client = AsyncQdrantClient(url=_qdrant_url())
            results = await client.search(
                collection_name=_COLLECTION,
                query_vector=query_vec,
                query_filter=Filter(must=[
                    FieldCondition(key="service_id", match=MatchValue(value=service_id)),
                    FieldCondition(key="level", match=MatchValue(value="ts")),
                ]),
                limit=top_n,
                score_threshold=threshold,
                with_payload=True,
            )
            await client.close()
            return [
                {
                    "ts_id": r.payload["ts_id"],
                    "title": r.payload["title"],
                    "_similarity": round(r.score, 4),
                }
                for r in results
            ]
        except Exception as e:
            _logger.warning("scenario_index_search_ts_failed", error=str(e))
            return []

    async def search_tc(
        self,
        service_id: str,
        ts_id: str,
        query_vec: list[float],
        threshold: float = 0.50,
    ) -> list[dict]:
        """특정 TS 내 TC 레벨에서 유사 TC를 검색한다.

        Returns:
            [{ tc_id, title, ts_id, _similarity }, ...] (유사도 내림차순)
        """
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            client = AsyncQdrantClient(url=_qdrant_url())
            results = await client.search(
                collection_name=_COLLECTION,
                query_vector=query_vec,
                query_filter=Filter(must=[
                    FieldCondition(key="service_id", match=MatchValue(value=service_id)),
                    FieldCondition(key="ts_id", match=MatchValue(value=ts_id)),
                    FieldCondition(key="level", match=MatchValue(value="tc")),
                ]),
                limit=10,
                score_threshold=threshold,
                with_payload=True,
            )
            await client.close()
            return [
                {
                    "tc_id": r.payload["tc_id"],
                    "title": r.payload["title"],
                    "ts_id": r.payload["ts_id"],
                    "_similarity": round(r.score, 4),
                }
                for r in results
            ]
        except Exception as e:
            _logger.warning("scenario_index_search_tc_failed", error=str(e))
            return []
