"""DB snapshot + tables 의 TTL cache helper — PoC 8 (데이터 layer).

기존 DBTestTool (qapilot/tools/db_test_tool.py) 의 _get_tables / _get_snapshot 을
wrap + per-process TTL cache. PoC 4 의 LRU 패턴 + TTL.

호출자 (유빈 TC/TV agent):
    from qapilot.shared.db_state import get_db_snapshot_cached, list_db_tables_cached

    tables = await list_db_tables_cached(service_id)
    snap = await get_db_snapshot_cached(service_id, "customers", ttl_seconds=60)
    if snap:
        rows = snap.get("rows", [])
        result = validator.validate(tv_field, intent, db_snapshot=rows, schemas=...)

회의 결정 5 (2026-06-09): PoC = snapshot + client filter, 큰 SUT 시점 (B) 별도.
본 모듈은 (A) PoC. (B) /db/query endpoint 협업 (E 영역) 은 후속.

graceful:
- DB 연결 실패 → None (caller 가 schema 검증만으로 진행 가능)
- 같은 (service_id, table) 의 ttl 내 호출 = cache hit

cache key 에 service_id 가 있는 이유:
- 현재 DBTestTool 은 단일 QAPILOT_SUT_DB_URL 가정 (multi-service X)
- 향후 multi-service 시점에 DBTestTool 의 _module_url() 을 service 별로 분리하면
  본 cache 의 key (service_id, table) 가 그대로 격리 보장

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from qapilot.shared.logger import get_logger

_logger = get_logger("shared.db_state")

# 기본 TTL
DEFAULT_TABLES_TTL_SECONDS = 300   # 5분 — 테이블 목록 변경 빈도 낮음
DEFAULT_SNAPSHOT_TTL_SECONDS = 60  # 1분 — 데이터 변경 빈도 (TC 간) 가정


# ────────────────────────────────────────────────────────────────────────
# TTL cache (process-local, thread-safe)
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _TTLEntry:
    value: Any
    expires_at: float


class _TTLCache:
    """단순 TTL cache — key → (value, expires_at)."""

    def __init__(self) -> None:
        self._store: dict[Any, _TTLEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> tuple[bool, Any]:
        """(hit, value) — hit=False 면 expired/미존재."""
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            if entry.expires_at < now:
                self._store.pop(key, None)
                return False, None
            return True, entry.value

    def set(self, key: Any, value: Any, ttl_seconds: float) -> None:
        with self._lock:
            self._store[key] = _TTLEntry(value=value, expires_at=time.monotonic() + ttl_seconds)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            total = len(self._store)
            active = sum(1 for e in self._store.values() if e.expires_at >= now)
            return {"total": total, "active": active, "expired": total - active}


_tables_cache = _TTLCache()
_snapshot_cache = _TTLCache()


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────

async def list_db_tables_cached(
    service_id: str,
    *,
    ttl_seconds: float = DEFAULT_TABLES_TTL_SECONDS,
    db_tool: Any = None,  # DBTestTool 또는 None (None 이면 새 instance)
) -> list[str] | None:
    """SUT 의 테이블 목록 — TTL cache.

    Args:
        service_id: cache key 격리 + 향후 multi-service 분기 용.
        ttl_seconds: 기본 300초 (테이블 목록은 변경 빈도 낮음).
        db_tool: 외부에서 주입 가능 (test 시 mock). None 이면 기본 DBTestTool 사용.

    Returns:
        테이블 이름 list 또는 None (DB 연결 실패 등 graceful).
    """
    key = ("tables", service_id)
    hit, value = _tables_cache.get(key)
    if hit:
        return value

    tool = db_tool or _default_db_tool()
    try:
        tables = await tool._get_tables()  # noqa: SLF001 — wrap
    except Exception as e:
        _logger.warning("db_tables_fetch_failed", service_id=service_id, error=str(e))
        return None

    _tables_cache.set(key, tables, ttl_seconds)
    return tables


async def get_db_snapshot_cached(
    service_id: str,
    table: str,
    *,
    ttl_seconds: float = DEFAULT_SNAPSHOT_TTL_SECONDS,
    db_tool: Any = None,
) -> dict | None:
    """한 table 의 snapshot — TTL cache.

    Args:
        service_id: cache key 격리.
        table: 테이블 이름.
        ttl_seconds: 기본 60초.
        db_tool: 외부 주입 가능 (test mock).

    Returns:
        {"table": "...", "rows": [...], ...} 형식 dict 또는 None (실패 graceful).

    cache:
        같은 (service_id, table) 의 ttl 내 호출 = cache hit.
        다른 table 호출은 별개 entry.
    """
    if not (service_id and table):
        return None

    key = ("snapshot", service_id, table)
    hit, value = _snapshot_cache.get(key)
    if hit:
        return value

    tool = db_tool or _default_db_tool()
    try:
        snapshot = await tool._get_snapshot(table)  # noqa: SLF001
    except Exception as e:
        _logger.warning("db_snapshot_fetch_failed",
                        service_id=service_id, table=table, error=str(e))
        return None

    _snapshot_cache.set(key, snapshot, ttl_seconds)
    return snapshot


def clear_db_cache() -> None:
    """tables + snapshot 모두 비움 — 강제 새로고침 / 테스트 격리."""
    _tables_cache.clear()
    _snapshot_cache.clear()


def cache_stats() -> dict[str, dict[str, int]]:
    """디버깅용 cache 상태."""
    return {
        "tables": _tables_cache.stats(),
        "snapshot": _snapshot_cache.stats(),
    }


# ────────────────────────────────────────────────────────────────────────
# 기본 DBTestTool 제공 (의존성 격리)
# ────────────────────────────────────────────────────────────────────────

def _default_db_tool() -> Any:
    """기본 DBTestTool instance — lazy import 으로 의존성 격리.

    test 시 db_tool 인자로 mock 주입하면 본 함수 호출 안 됨.
    """
    from qapilot.tools.db_test_tool import DBTestTool
    return DBTestTool()
