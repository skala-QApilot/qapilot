"""도메인 지식 Tool — Qdrant 벡터 저장·검색·인덱스 관리.

청크를 배치로 임베딩하여 Qdrant에 upsert하고,
쿼리 임베딩으로 유사도 검색을 수행한다.
임베딩 로직은 _embedder 모듈에 위임한다.

Author: 전아린
Created: 2026-05-07
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from qapilot.shared.schemas import DomainRule
from qapilot.tools.domain_knowledge._embedder import EMBED_DIM, embed_with_retry, get_embedder

_QDRANT_COLLECTION = "domain_knowledge"
_EMBED_BATCH = 20
_TOP_K_DEFAULT = 5

_DOMAIN_DIR = Path(".qapilot/domain")


class VectorStore:
    """Qdrant 임베딩 저장 및 유사도 검색을 담당한다.

    역할: 청크 임베딩·upsert, 컬렉션 생성, 유사도 검색, 인덱스 파일 저장
    입력: 청크 목록 또는 검색 쿼리
    출력: 저장 결과 (int, int) 또는 List[DomainRule]
    """

    def __init__(self, logger: Any) -> None:
        """VectorStore를 초기화한다.

        Args:
            logger: structlog 로거 인스턴스.
        """
        self._logger = logger

    async def embed_and_store(self, chunks: list[dict]) -> tuple[int, int]:
        """청크를 배치로 임베딩하여 Qdrant에 upsert한다.

        임베딩 실패 시 배치 단위로 최대 MAX_EMBED_RETRY회 재시도한다.

        Args:
            chunks: chunk_id, source, section, text 키를 가진 청크 목록.

        Returns:
            tuple[int, int]: (저장 성공 건수, 저장 실패 건수).
        """
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import PointStruct

        client = AsyncQdrantClient(url=self._qdrant_url())

        try:
            await self._ensure_collection(client)
            stored, failed = 0, 0

            for i in range(0, len(chunks), _EMBED_BATCH):
                batch = chunks[i : i + _EMBED_BATCH]
                vectors = await embed_with_retry([c["text"] for c in batch], self._logger)

                if vectors is None:
                    self._logger.error("embed_batch_failed", batch_start=i, count=len(batch))
                    failed += len(batch)
                    continue

                points = [
                    PointStruct(
                        id=chunk["chunk_id"],
                        vector=vector,
                        payload={
                            "source": chunk["source"],
                            "section": chunk["section"],
                            "text": chunk["text"],
                            "category": "document",
                        },
                    )
                    for chunk, vector in zip(batch, vectors)
                ]
                await client.upsert(collection_name=_QDRANT_COLLECTION, points=points)
                stored += len(batch)
        finally:
            await client.close()

        return stored, failed

    async def search(self, query: str, top_k: int = _TOP_K_DEFAULT) -> list[DomainRule]:
        """쿼리를 임베딩하여 Qdrant에서 유사 도메인 규칙을 검색한다.

        Args:
            query: 검색 쿼리 문자열.
            top_k: 반환할 최대 결과 수.

        Returns:
            list[DomainRule]: 유사도 순으로 정렬된 도메인 규칙 목록.
        """
        from qdrant_client import AsyncQdrantClient

        client = AsyncQdrantClient(url=self._qdrant_url())

        try:
            embedder = get_embedder()
            vecs = await asyncio.to_thread(embedder.encode, [query], normalize_embeddings=True)
            vector = vecs[0].tolist()
            response = await client.query_points(
                collection_name=_QDRANT_COLLECTION,
                query=vector,
                limit=top_k,
            )
            hits = response.points
        finally:
            await client.close()

        return [
            {
                "rule_id": str(hit.id),
                "source": hit.payload.get("source", ""),
                "category": hit.payload.get("category", ""),
                "content": hit.payload.get("text", ""),
                "similarity_score": round(hit.score, 4),
            }
            for hit in hits
        ]

    async def upsert_point(self, point_id: str, text: str, payload: dict) -> None:
        """단일 포인트를 임베딩하여 Qdrant에 upsert한다.

        Args:
            point_id: Qdrant 포인트 ID (UUID 문자열).
            text: 임베딩할 텍스트.
            payload: 포인트에 저장할 메타데이터.
        """
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import PointStruct

        client = AsyncQdrantClient(url=self._qdrant_url())

        try:
            await self._ensure_collection(client)
            embedder = get_embedder()
            vecs = await asyncio.to_thread(embedder.encode, [text], normalize_embeddings=True)
            vector = vecs[0].tolist()
            await client.upsert(
                collection_name=_QDRANT_COLLECTION,
                points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            )
        finally:
            await client.close()

    def save_index(self, file_path: Path, chunk_count: int) -> None:
        """.qapilot/domain/<stem>.index.json 에 임포트 인덱스를 저장한다.

        Args:
            file_path: 임포트한 원본 파일 경로.
            chunk_count: 생성된 청크 총 개수.
        """
        _DOMAIN_DIR.mkdir(parents=True, exist_ok=True)
        index_path = _DOMAIN_DIR / f"{file_path.stem}.index.json"
        index_path.write_text(
            json.dumps(
                {
                    "file": str(file_path),
                    "chunks": chunk_count,
                    "collection": _QDRANT_COLLECTION,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    async def _ensure_collection(self, client: Any) -> None:
        """Qdrant 컬렉션이 없으면 생성한다.

        Args:
            client: AsyncQdrantClient 인스턴스.
        """
        from qdrant_client.models import Distance, VectorParams

        resp = await client.get_collections()
        if _QDRANT_COLLECTION not in {c.name for c in resp.collections}:
            await client.create_collection(
                collection_name=_QDRANT_COLLECTION,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )
            self._logger.info("qdrant_collection_created", name=_QDRANT_COLLECTION)

    @staticmethod
    def _qdrant_url() -> str:
        """환경변수에서 Qdrant 서버 URL을 읽는다.

        Returns:
            str: Qdrant 서버 URL.
        """
        return os.getenv("QDRANT_URL", "http://localhost:6333")
