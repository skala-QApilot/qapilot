"""요구사항 Repository — PostgreSQL CRUD.

Author: 전아린
Created: 2026-05-11
"""

from __future__ import annotations

from qapilot.shared.database import AsyncSessionLocal
from qapilot.shared.models import RequirementRecord
from qapilot.shared.schemas import RequirementItem


async def save_requirements(requirements: list[RequirementItem], trace_id: str) -> None:
    """요구사항 목록을 requirements 테이블에 upsert한다.

    동일 req_id가 이미 존재하면 내용을 덮어쓴다.

    Args:
        requirements: 저장할 RequirementItem 목록.
        trace_id: 실행 추적 ID.
    """
    from sqlalchemy.dialects.postgresql import insert

    if not requirements:
        return

    async with AsyncSessionLocal() as session:
        rows = [
            {
                "req_id": r["req_id"],
                "trace_id": trace_id,
                "req_type": r["req_type"],
                "content": r["content"],
                "priority": r["priority"],
                "domain_area": r["domain_area"],
            }
            for r in requirements
        ]
        stmt = insert(RequirementRecord).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["req_id"],
            set_={
                "trace_id": stmt.excluded.trace_id,
                "req_type": stmt.excluded.req_type,
                "content": stmt.excluded.content,
                "priority": stmt.excluded.priority,
                "domain_area": stmt.excluded.domain_area,
            },
        )
        await session.execute(stmt)
        await session.commit()


async def load_requirements(trace_id: str) -> list[RequirementItem]:
    """특정 trace_id로 저장된 요구사항을 req_id 순으로 조회한다.

    Args:
        trace_id: 조회할 실행 추적 ID.

    Returns:
        list[RequirementItem]: 저장된 요구사항 목록.
    """
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RequirementRecord)
            .where(RequirementRecord.trace_id == trace_id)
            .order_by(RequirementRecord.req_id)
        )
        records = result.scalars().all()

    return [
        {
            "req_id": r.req_id,
            "req_type": r.req_type,
            "content": r.content,
            "priority": r.priority,
            "domain_area": r.domain_area,
        }
        for r in records
    ]
