"""scenarios / scenario_versions UPSERT — 파일 기반 scenarios/<ts>.json 와 dual-write.

저장 정책:
  - _save_scenarios 가 ts 별로 호출 → UPSERT scenarios (service_id, ts_id),
    INSERT scenario_versions (version_number = MAX+1), UPDATE scenarios.current_version_id.
  - DB 미연결 / service_id 미존재 시 graceful no-op — file 기록이 source of truth.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.scenario_writer")


def upsert_scenario_version(service_id: str, ts_id: str, payload: dict) -> str | None:
    """한 시나리오의 새 버전을 기록. 반환: 새 scenario_version_id (UUID str) 또는 None.

    INSERT scenario (없으면) → INSERT scenario_version (next version_number) → UPDATE current_version_id.
    같은 트랜잭션에서 처리.
    """
    pool = get_pool()
    if pool is None:
        return None
    if not (service_id and ts_id):
        return None

    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    scenario_id_new = str(uuid.uuid4())
    version_id = str(uuid.uuid4())

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            # 1) scenarios UPSERT — 첫 호출 시 INSERT, 이후엔 기존 id 재사용
            cur.execute(
                """
                INSERT INTO scenarios (id, service_id, ts_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (service_id, ts_id) DO UPDATE SET updated_at = now()
                RETURNING id
                """,
                (scenario_id_new, service_id, ts_id),
            )
            row = cur.fetchone()
            scenario_id = str(row[0]) if row else None
            if not scenario_id:
                return None

            # 2) 다음 version_number 결정
            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM scenario_versions WHERE scenario_id = %s",
                (scenario_id,),
            )
            next_version = int(cur.fetchone()[0])

            # 3) scenario_versions INSERT
            cur.execute(
                """
                INSERT INTO scenario_versions (id, scenario_id, version_number, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (version_id, scenario_id, next_version, payload_json),
            )

            # 4) current_version_id 갱신
            cur.execute(
                "UPDATE scenarios SET current_version_id = %s, updated_at = now() WHERE id = %s",
                (version_id, scenario_id),
            )
        return version_id
    except Exception as e:
        _logger.warning(
            "scenario_db_mirror_failed",
            service_id=service_id,
            ts_id=ts_id,
            error=str(e),
        )
        return None
