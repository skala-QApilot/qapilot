"""도메인 지식 Tool — BGE-M3 임베딩 싱글턴·재시도 로직.

SentenceTransformer 모델 로드와 임베딩 재시도 로직을 담당한다.
Qdrant 클라이언트 코드와 분리하여 모델 교체 시 이 파일만 수정한다.

Author: 전아린
Created: 2026-05-11
"""

from __future__ import annotations

import asyncio
from typing import Any

EMBED_DIM = 1024        # BAAI/bge-m3 dense vector 차원
BGE_MODEL = "BAAI/bge-m3"
MAX_EMBED_RETRY = 3     # 임베딩 실패 시 최대 재시도 횟수

_embedder: Any = None


def get_embedder() -> Any:
    """SentenceTransformer 모델을 첫 호출 시 로드하고 이후 재사용한다.

    Returns:
        SentenceTransformer: 로드된 임베딩 모델 인스턴스.
    """
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(BGE_MODEL)
    return _embedder


async def embed_with_retry(texts: list[str], logger: Any) -> list | None:
    """텍스트 목록을 임베딩하고 실패 시 최대 MAX_EMBED_RETRY회 재시도한다.

    Args:
        texts: 임베딩할 텍스트 목록.
        logger: structlog 로거 인스턴스 (재시도 경고 로깅에 사용).

    Returns:
        list: 성공 시 벡터 목록. 재시도 소진 시 None.
    """
    embedder = get_embedder()
    for attempt in range(MAX_EMBED_RETRY):
        try:
            vecs = await asyncio.to_thread(
                embedder.encode, texts, normalize_embeddings=True
            )
            return [v.tolist() for v in vecs]
        except Exception as e:
            logger.warning("embed_retry", attempt=attempt, error=str(e))
            if attempt < MAX_EMBED_RETRY - 1:
                await asyncio.sleep(2**attempt)
    return None
