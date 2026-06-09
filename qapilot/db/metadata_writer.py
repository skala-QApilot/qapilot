"""metadata_indices DB + S3 mirror writer — PoC 3 (본인 영역).

기존 `code_writer.upsert_codebase_index()` 패턴 (#240) 그대로 차용. 차이:
- UNIQUE (service_id, commit_hash, kind, sub_kind) → ON CONFLICT DO UPDATE
- kind/sub_kind 2축 (codebase_indices 는 kind 1축)
- confidence + extraction_method 컬럼 추가

cache skip 정책:
- head_object 로 같은 commit_sha 의 S3 객체 존재 확인
- 존재 + DB row 도 있으면 PUT/INSERT 모두 skip
- DB 만 갱신해야 할 경우 (예: confidence 재계산) 는 force=True

본 모듈은 writer 만. reader 는 metadata_reader.py.

본인 영역 (주환).
Created: 2026-06-09
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger
from qapilot.storage import s3_client

_logger = get_logger("db.metadata_writer")


def _build_s3_key(service_id: str, commit_sha: str, kind: str, sub_kind: str) -> str:
    """services/{sid}/metadata-index/{sha}/{kind}-{sub_kind}.json — spec 정합."""
    return f"services/{service_id}/metadata-index/{commit_sha}/{kind}-{sub_kind}.json"


def upsert_metadata_index(
    index: BaseModel | dict[str, Any],
    *,
    service_id: str,
    commit_hash: str,
    file_count: int | None = None,
    confidence: float | None = None,
    extraction_method: str | None = None,
    force: bool = False,
) -> bool:
    """metadata_indices 의 한 (service+commit+kind+sub_kind) record 를 upsert.

    Args:
        index: Pydantic MetadataIndex (FrontendSelectorsIndex 등) 또는 dict.
               kind/sub_kind 가 본 객체에 있어야 함.
        service_id: service UUID (문자열).
        commit_hash: 풀 git SHA (40자) — DB 의 commit_hash 컬럼.
        file_count: 추출 대상 파일 수.
        confidence: AST=1.0 / LLM 의미라벨=0.85 / LLM 추론=0.65.
                    None 이면 0.0 (caller 가 누락한 정보 표시용).
        extraction_method: "ast" | "llm" | "hybrid".
        force: True 면 cache skip 무시 + 무조건 PUT/UPDATE.

    Returns:
        성공/실패. 부분 실패 (S3 OK + DB 실패) 시 False — caller 가 재시도 가능.

    cache skip 정책 (force=False):
        - 동일 (service_id, commit_hash, kind, sub_kind) 의 S3 객체가 head_object 로 확인되면
          PUT skip + DB upsert 만 (scanned_at + 메타데이터 갱신)
        - DB row 도 있으면 ON CONFLICT DO UPDATE — scanned_at 만 갱신
    """
    pool = get_pool()
    if pool is None or not (service_id and commit_hash):
        _logger.info("metadata_index_skip", reason="pool_or_args_missing")
        return False

    kind, sub_kind = _extract_kind_sub_kind(index)
    if not (kind and sub_kind):
        _logger.warning("metadata_index_skip", reason="kind_sub_kind_missing")
        return False

    body = _to_json_bytes(index)
    sha256 = hashlib.sha256(body).hexdigest()
    s3_key = _build_s3_key(service_id, commit_hash, kind, sub_kind)
    new_id = str(uuid.uuid4())

    # ── S3 PUT (force=False + 객체 존재 + sha256 일치 시 skip) ────────────
    skip_put = False
    if not force:
        head = s3_client.head_object(s3_key)
        if head is not None and head.get("bytes") == len(body):
            skip_put = True
            _logger.info("metadata_index_s3_skip", s3_key=s3_key,
                         reason="head_object_match_bytes")

    if not skip_put:
        if s3_client.put_bytes(s3_key, body, "application/json") is None:
            _logger.warning("metadata_index_s3_failed",
                            service_id=service_id, kind=kind, sub_kind=sub_kind)
            return False

    # ── DB upsert (ON CONFLICT UPDATE — 재스캔 시 scanned_at + 메타 갱신) ──
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metadata_indices (
                    id, service_id, commit_hash, kind, sub_kind, s3_key,
                    bytes, sha256, file_count, confidence, extraction_method
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (service_id, commit_hash, kind, sub_kind) DO UPDATE SET
                    s3_key            = EXCLUDED.s3_key,
                    bytes             = EXCLUDED.bytes,
                    sha256            = EXCLUDED.sha256,
                    file_count        = EXCLUDED.file_count,
                    confidence        = EXCLUDED.confidence,
                    extraction_method = EXCLUDED.extraction_method,
                    scanned_at        = NOW()
                """,
                (new_id, service_id, commit_hash, kind, sub_kind, s3_key,
                 len(body), sha256, file_count, confidence, extraction_method),
            )
        return True
    except Exception as e:
        _logger.warning("metadata_index_db_failed",
                        service_id=service_id, kind=kind, sub_kind=sub_kind,
                        error=str(e))
        return False


def upsert_source_file(
    *,
    service_id: str,
    commit_hash: str,
    relative_path: str,
    content: bytes,
    content_type: str = "text/plain; charset=utf-8",
    force: bool = False,
) -> str | None:
    """코드베이스 원본 1 파일 → S3 PUT.

    경로: services/{service_id}/source/{commit_sha}/{relative_path}
    DB 에 별도 row 없음 — source 는 S3 만 (path 자체가 카탈로그).

    cache skip:
        - force=False + head_object 의 bytes 일치 시 PUT skip.

    Returns:
        성공 시 s3_key, 실패 시 None.
    """
    if not (service_id and commit_hash and relative_path):
        return None

    safe_rel = relative_path.lstrip("/").replace("..", "")
    s3_key = f"services/{service_id}/source/{commit_hash}/{safe_rel}"

    if not force:
        head = s3_client.head_object(s3_key)
        if head is not None and head.get("bytes") == len(content):
            return s3_key

    if s3_client.put_bytes(s3_key, content, content_type) is None:
        return None
    return s3_key


# ────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────

def _extract_kind_sub_kind(index: BaseModel | dict[str, Any]) -> tuple[str, str]:
    """Pydantic model 또는 dict 에서 kind/sub_kind 추출."""
    if isinstance(index, BaseModel):
        kind = getattr(index, "kind", None)
        sub_kind = getattr(index, "sub_kind", None)
    elif isinstance(index, dict):
        kind = index.get("kind")
        sub_kind = index.get("sub_kind")
    else:
        kind = sub_kind = None
    return str(kind) if kind else "", str(sub_kind) if sub_kind else ""


def _to_json_bytes(index: BaseModel | dict[str, Any]) -> bytes:
    """Pydantic model 또는 dict → 안정적 JSON bytes (sha256 결정성 보장)."""
    if isinstance(index, BaseModel):
        # Pydantic v2 — model_dump_json 은 indent X. 본인은 indent=2 로
        # human-readable + sha 결정성 유지 (같은 model → 같은 bytes).
        payload = index.model_dump(mode="json")
    else:
        payload = index
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
