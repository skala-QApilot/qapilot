"""QApilot 내부 PostgreSQL — 비동기 엔진·세션 팩토리.

요구사항·시나리오 등 QApilot 자체 데이터를 저장한다.
system-under-test DB와 별개의 연결이다.

Author: 전아린
Created: 2026-05-11
"""

from __future__ import annotations

import os
import re

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def _async_url() -> str:
    """DATABASE_URL의 드라이버를 asyncpg로 변환한다."""
    url = os.getenv("DATABASE_URL", "postgresql://qapilot:qapilot@localhost:5432/qapilot")
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


engine = create_async_engine(_async_url(), pool_pre_ping=True, echo=False)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def create_tables() -> None:
    """애플리케이션 기동 시 테이블을 생성한다 (없는 경우에만)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
