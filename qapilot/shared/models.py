"""QApilot 내부 ORM 모델.

Author: 전아린
Created: 2026-05-11
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from qapilot.shared.database import Base

_KST = ZoneInfo("Asia/Seoul")


class TraceTimestampMixin:
    """모든 QApilot 산출물 테이블 공통 컬럼: 실행 추적 ID + 생성 시각.

    새 산출물 모델은 ``class FooRecord(TraceTimestampMixin, Base)`` 로 정의해
    trace_id 누락을 방지한다.
    """

    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(_KST),
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


class MetadataIndexRecord(Base):
    """코드 스캔 메타데이터 인덱스 — TC/TV 생성 보조용.

    `codebase_indices` 와 별도 namespace. PoC writer (`qapilot/db/metadata_writer.py`) 는
    raw SQL 로 동작하지만, schema 자체는 본 ORM 으로 등록하여 `create_tables()`
    호출 시 자동 생성된다.

    DDL 원본: docs/scan-enhancement/migrations/001_metadata_indices.sql
    """

    __tablename__ = "metadata_indices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    commit_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sub_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "service_id", "commit_hash", "kind", "sub_kind",
            name="metadata_indices_service_id_commit_hash_kind_sub_kind_key",
        ),
        CheckConstraint(
            "kind IN ('frontend', 'backend', 'sut_tests')",
            name="metadata_indices_kind_check",
        ),
        CheckConstraint(
            "sub_kind IN ('selectors', 'routes', 'schemas', 'patterns')",
            name="metadata_indices_sub_kind_check",
        ),
        CheckConstraint(
            "extraction_method IN ('ast', 'llm', 'hybrid')",
            name="metadata_indices_extraction_method_check",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="metadata_indices_confidence_check",
        ),
        Index(
            "idx_metadata_indices_lookup",
            "service_id", "kind", "sub_kind", "scanned_at",
        ),
        Index("idx_metadata_indices_commit", "commit_hash"),
    )
