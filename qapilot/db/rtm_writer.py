"""rtm_versions / rtm_requirements / rtm_requirement_tc_links INSERT — file 과 dual-write.

호출 시점: _write_initial_rtm_version 의 디스크 저장 직후.
한 트랜잭션에서 version 1행 + N requirements + M links 일괄 INSERT.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import json
import uuid
from typing import Iterable

from qapilot.db.connection import get_pool
from qapilot.shared.logger import get_logger

_logger = get_logger("db.rtm_writer")


def write_rtm_version(
    *,
    service_id: str,
    trace_id: str | None,
    label: str,
    requirements: list[dict],
    summary: dict | None = None,
) -> str | None:
    """RTM 1버전을 DB 에 기록. 반환: rtm_version_id 또는 None (graceful no-op).

    requirements 각 항목 shape:
        {"frId" / "req_id": str, "content": str, "linkedTcIds": list[str]}

    linkedTcIds 의 각 요소는 "TC-..." 형식. ts_id 는 별도 필드 없으므로 같은 시나리오 안의 TC 라고
    가정 — 현재 RTM 구조가 ts_id 를 저장하지 않으므로 ts_id 는 빈 문자열로 두고 (tc_id, "") 매핑.
    추후 RTM 구조에 ts_id 추가하면 정밀화.
    """
    pool = get_pool()
    if pool is None:
        return None
    if not (service_id and label and requirements):
        return None

    rtm_version_id = str(uuid.uuid4())
    summary_json = json.dumps(summary or {}, ensure_ascii=False) if summary else None

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rtm_versions (id, service_id, label, trace_id, summary)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (rtm_version_id, service_id, label, trace_id, summary_json),
            )
            for req in requirements:
                req_id = req.get("frId") or req.get("req_id")
                if not req_id:
                    continue
                requirement_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO rtm_requirements (id, rtm_version_id, req_id, content)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (requirement_id, rtm_version_id, req_id, req.get("content")),
                )
                # linkedTcIds 는 TC ID 만의 리스트 — RTM 구조상 ts_id 가 없으므로
                # tc_results 와 JOIN 할 때 tc_id 기준만 매칭 (ts_id 는 빈 문자열).
                for tc_id in _normalize_tc_ids(req.get("linkedTcIds")):
                    cur.execute(
                        """
                        INSERT INTO rtm_requirement_tc_links (rtm_requirement_id, ts_id, tc_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (requirement_id, "", tc_id),
                    )
        return rtm_version_id
    except Exception as e:
        _logger.warning(
            "rtm_db_mirror_failed",
            service_id=service_id,
            label=label,
            error=str(e),
        )
        return None


def _normalize_tc_ids(value: object) -> Iterable[str]:
    if not isinstance(value, list):
        return ()
    return (str(v) for v in value if v)
