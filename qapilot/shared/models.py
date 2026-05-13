"""QApilot 내부 ORM 모델.

Author: 전아린
Created: 2026-05-11
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from qapilot.shared.database import Base


class TraceTimestampMixin:
    """모든 QApilot 산출물 테이블 공통 컬럼: 실행 추적 ID + 생성 시각.

    새 산출물 모델은 ``class FooRecord(TraceTimestampMixin, Base)`` 로 정의해
    trace_id 누락을 방지한다.
    """

    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class RequirementRecord(TraceTimestampMixin, Base):
    """추출된 요구사항 영속 모델."""

    __tablename__ = "requirements"

    req_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    req_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(10), nullable=False)
    domain_area: Mapped[str] = mapped_column(String(100), nullable=False)
