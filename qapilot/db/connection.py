"""sync psycopg3 connection pool — DATABASE_URL 미설정 시 graceful no-op.

dual-write 의 'mirror 측' 으로 사용. DB 가 죽어도 file 기록은 살아남음.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import os
import threading
from typing import Any

from qapilot.shared.logger import get_logger

_logger = get_logger("db.connection")
_pool: Any = None  # psycopg_pool.ConnectionPool | None
_pool_lock = threading.Lock()
_pool_failed = False  # 첫 실패 후 재시도 안 함


def get_pool() -> Any:
    """psycopg ConnectionPool 또는 None.

    None 인 경우:
    - DATABASE_URL 미설정
    - 초기 연결 실패 (이후엔 영영 None 반환 — DB 죽었다고 매 호출마다 재시도하지 않음)
    """
    global _pool, _pool_failed
    if _pool is not None:
        return _pool
    if _pool_failed:
        return None

    with _pool_lock:
        if _pool is not None:
            return _pool
        if _pool_failed:
            return None

        url = os.environ.get("DATABASE_URL")
        if not url:
            _logger.info("db_disabled", reason="DATABASE_URL not set")
            _pool_failed = True
            return None

        try:
            from psycopg_pool import ConnectionPool

            _pool = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=4,
                open=True,
                timeout=5.0,
            )
            _logger.info("db_pool_opened", url_host=_redact(url))
            return _pool
        except Exception as e:
            _logger.warning("db_pool_open_failed", error=str(e))
            _pool_failed = True
            return None


def _redact(url: str) -> str:
    """user:pass@host 부분의 자격증명 제거 — 로그용."""
    if "@" not in url:
        return url
    prefix, host = url.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else ""
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"
