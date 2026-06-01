"""tc_results / tc_artifacts UPSERT — 디스크 (ui_result.json 등) 와 dual-write.

tc_results UNIQUE(run_id, ts_id, tc_id, kind) — 같은 TC 의 같은 kind 결과는 단일 행.
INSERT … ON CONFLICT DO UPDATE 로 신규/resume 양쪽 흡수.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.tc_result_writer")


def upsert_tc_result(
    run_id: str,
    ts_id: str,
    tc_id: str,
    kind: str,            # ui / api / db
    payload: dict | None,
    status: str | None = None,
) -> str | None:
    """tc_results UPSERT. 반환: 해당 행의 id (UUID 문자열). 실패/비활성 시 None.

    status 미지정 시 payload 에서 "status" / "tc_status" 키로 자동 추출.
    """
    pool = get_pool()
    if pool is None:
        return None
    if not (run_id and ts_id and tc_id and kind):
        return None

    if status is None and isinstance(payload, dict):
        status = payload.get("status") or payload.get("tc_status")

    new_id = str(uuid.uuid4())
    payload_json = _json(payload)

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tc_results (
                    id, run_id, ts_id, tc_id, kind, status, payload, artifact_count
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb, 0
                )
                ON CONFLICT (run_id, ts_id, tc_id, kind) DO UPDATE SET
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload
                RETURNING id
                """,
                (new_id, run_id, ts_id, tc_id, kind, status, payload_json),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
    except Exception as e:
        _logger.warning(
            "tc_result_db_mirror_failed",
            run_id=run_id,
            tc_id=tc_id,
            kind=kind,
            error=str(e),
        )
        return None


def insert_tc_artifact(
    tc_result_id: str,
    step_index: int,
    kind: str,            # png / html
    s3_key: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> bool:
    """tc_artifacts 1행 INSERT + tc_results.artifact_count += 1.

    실패 시 warn 만 — 스크린샷 자체는 S3 에 이미 올라간 상태일 수 있으나
    DB row 만 누락. 다음 폴링/조회 때 다시 시도하지 않음 (재시도는 phase 2).
    """
    pool = get_pool()
    if pool is None:
        return False

    new_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tc_artifacts (
                    id, tc_result_id, step_index, kind, s3_key, bytes, sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (new_id, tc_result_id, step_index, kind, s3_key, size_bytes, sha256),
            )
            cur.execute(
                "UPDATE tc_results SET artifact_count = artifact_count + 1 WHERE id = %s",
                (tc_result_id,),
            )
        return True
    except Exception as e:
        _logger.warning(
            "tc_artifact_db_mirror_failed",
            tc_result_id=tc_result_id,
            kind=kind,
            error=str(e),
        )
        return False


def _json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
