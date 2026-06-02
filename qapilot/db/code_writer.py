"""generated_code / action_mappings / codebase_indices DB+S3 mirror.

`_save_codes` / `_save_codebase_index_to_disk` 의 디스크 쓰기와 dual-write.
디스크 쓰기는 PR-17 이후에도 유지 — 다른 도메인들과 동일한 graceful 패턴.

Author: C
Created: 2026-06-02
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger
from qapilot.storage import s3_client

_logger = get_logger("db.code_writer")


# ─────────────────────────────────────────────────────────────────────────────
# generated_code — TC 별 .js 코드. S3 에 본문, DB 에 메타.
# ─────────────────────────────────────────────────────────────────────────────

def upsert_generated_code(service_id: str, tc_id: str, code_text: str) -> bool:
    """한 TC 의 generated code 를 S3 + DB 에 기록. 호출마다 새 version 증가.

    같은 (service_id, tc_id) 에 호출 시: scenario_writer 와 동일하게 MAX(version)+1.
    실패는 graceful — 디스크는 이미 쓰여있고, DB 가 비어도 다음 실행이 회복.
    """
    pool = get_pool()
    if pool is None or not (service_id and tc_id):
        return False

    code_bytes = code_text.encode("utf-8")
    sha256 = hashlib.sha256(code_bytes).hexdigest()
    new_id = str(uuid.uuid4())

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            # 다음 version 결정
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM generated_code "
                "WHERE service_id = %s AND tc_id = %s",
                (service_id, tc_id),
            )
            next_version = int(cur.fetchone()[0])
            s3_key = f"services/{service_id}/generated-code/{tc_id}/v{next_version}.js"

            # S3 PUT — DB INSERT 전에. 실패 시 DB 안 씀.
            if s3_client.put_bytes(s3_key, code_bytes, "application/javascript") is None:
                _logger.warning("generated_code_s3_skip", tc_id=tc_id)
                return False

            cur.execute(
                """
                INSERT INTO generated_code (id, service_id, tc_id, version, s3_key, bytes, sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (service_id, tc_id, version) DO NOTHING
                """,
                (new_id, service_id, tc_id, next_version, s3_key, len(code_bytes), sha256),
            )
        return True
    except Exception as e:
        _logger.warning("generated_code_db_mirror_failed", tc_id=tc_id, error=str(e))
        return False


# ─────────────────────────────────────────────────────────────────────────────
# action_mappings — TC 별 action mapping. JSONB 인라인 (S3 없음).
# ─────────────────────────────────────────────────────────────────────────────

def upsert_action_mapping(service_id: str, tc_id: str, payload: dict) -> bool:
    """한 TC 의 ActionMapping 을 DB 에 JSONB 인라인 기록. version 자동 증가."""
    pool = get_pool()
    if pool is None or not (service_id and tc_id):
        return False

    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    new_id = str(uuid.uuid4())

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM action_mappings "
                "WHERE service_id = %s AND tc_id = %s",
                (service_id, tc_id),
            )
            next_version = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO action_mappings (id, service_id, tc_id, version, payload)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (service_id, tc_id, version) DO NOTHING
                """,
                (new_id, service_id, tc_id, next_version, payload_json),
            )
        return True
    except Exception as e:
        _logger.warning("action_mapping_db_mirror_failed", tc_id=tc_id, error=str(e))
        return False


# ─────────────────────────────────────────────────────────────────────────────
# codebase_indices — kind 별 (endpoints/models/functions/callgraph/manifest/frontend).
# 같은 commit_hash 의 index 는 dedup (ON CONFLICT skip).
# ─────────────────────────────────────────────────────────────────────────────

def upsert_codebase_index(
    service_id: str,
    commit_hash: str | None,
    kind: str,
    payload: Any,
    file_count: int | None = None,
) -> bool:
    """한 kind (endpoints/models/...) 의 인덱스를 S3 + DB 에 기록."""
    pool = get_pool()
    if pool is None or not (service_id and kind):
        return False

    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()
    commit = commit_hash or "no-commit"
    s3_key = f"services/{service_id}/codebase-index/{commit}/{kind}.json"
    new_id = str(uuid.uuid4())

    if s3_client.put_bytes(s3_key, body, "application/json") is None:
        _logger.warning("codebase_index_s3_skip", service_id=service_id, kind=kind)
        return False

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO codebase_indices (id, service_id, commit_hash, kind, s3_key, bytes, sha256, file_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (new_id, service_id, commit_hash, kind, s3_key, len(body), sha256, file_count),
            )
        return True
    except Exception as e:
        _logger.warning("codebase_index_db_mirror_failed", service_id=service_id, kind=kind, error=str(e))
        return False
