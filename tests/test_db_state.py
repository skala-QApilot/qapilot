"""db_state TTL cache 단위 테스트 — PoC 8 (데이터 layer).

mock DBTool 으로 격리 — 실 DB 의존 없이 cache 동작 검증.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.shared.db_state import (
    _TTLCache,
    cache_stats,
    clear_db_cache,
    get_db_snapshot_cached,
    list_db_tables_cached,
)


SERVICE_A = "service-aaaa"
SERVICE_B = "service-bbbb"


@pytest.fixture(autouse=True)
def _reset_cache():
    """각 테스트 격리."""
    clear_db_cache()
    yield
    clear_db_cache()


def _mock_db_tool(tables=None, snapshots=None):
    """mock DBTool — _get_tables / _get_snapshot async."""
    tables = tables or ["customers", "orders"]
    snapshots = snapshots or {}
    tool = MagicMock()
    tool._get_tables = AsyncMock(return_value=tables)

    async def _get_snap(table):
        if table in snapshots:
            return snapshots[table]
        return {"table": table, "rows": []}

    tool._get_snapshot = AsyncMock(side_effect=_get_snap)
    return tool


# ────────────────────────────────────────────────────────────────────────
# _TTLCache (단위)
# ────────────────────────────────────────────────────────────────────────

def test_ttl_cache_hit_within_ttl():
    c = _TTLCache()
    c.set("k", "v", ttl_seconds=10)
    hit, val = c.get("k")
    assert hit is True
    assert val == "v"


def test_ttl_cache_miss_after_expiry():
    c = _TTLCache()
    c.set("k", "v", ttl_seconds=0.01)
    time.sleep(0.05)
    hit, val = c.get("k")
    assert hit is False
    assert val is None


def test_ttl_cache_missing_key():
    c = _TTLCache()
    hit, val = c.get("no_such")
    assert hit is False
    assert val is None


def test_ttl_cache_clear():
    c = _TTLCache()
    c.set("a", 1, ttl_seconds=10)
    c.set("b", 2, ttl_seconds=10)
    c.clear()
    assert c.get("a")[0] is False
    assert c.get("b")[0] is False


def test_ttl_cache_stats():
    c = _TTLCache()
    c.set("a", 1, ttl_seconds=10)
    c.set("b", 2, ttl_seconds=0.01)
    time.sleep(0.05)
    stats = c.stats()
    assert stats["total"] == 2
    assert stats["active"] == 1
    assert stats["expired"] == 1


# ────────────────────────────────────────────────────────────────────────
# list_db_tables_cached
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tables_cache_hit():
    tool = _mock_db_tool(tables=["a", "b"])
    out1 = await list_db_tables_cached(SERVICE_A, db_tool=tool)
    out2 = await list_db_tables_cached(SERVICE_A, db_tool=tool)
    assert out1 == ["a", "b"]
    assert out2 == ["a", "b"]
    tool._get_tables.assert_awaited_once()  # cache hit — 1번만 호출


@pytest.mark.asyncio
async def test_list_tables_cache_isolation_by_service():
    tool_a = _mock_db_tool(tables=["a1"])
    tool_b = _mock_db_tool(tables=["b1"])
    out_a = await list_db_tables_cached(SERVICE_A, db_tool=tool_a)
    out_b = await list_db_tables_cached(SERVICE_B, db_tool=tool_b)
    assert out_a == ["a1"]
    assert out_b == ["b1"]
    # 같은 service 재호출 = cache hit
    await list_db_tables_cached(SERVICE_A, db_tool=tool_a)
    tool_a._get_tables.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_tables_failure_returns_none():
    tool = MagicMock()
    tool._get_tables = AsyncMock(side_effect=RuntimeError("connection refused"))
    out = await list_db_tables_cached(SERVICE_A, db_tool=tool)
    assert out is None


@pytest.mark.asyncio
async def test_list_tables_failure_does_not_cache():
    """실패는 cache 안 함 — 다음 호출에서 재시도."""
    tool = MagicMock()
    tool._get_tables = AsyncMock(side_effect=[RuntimeError("x"), ["recovered"]])
    out1 = await list_db_tables_cached(SERVICE_A, db_tool=tool)
    out2 = await list_db_tables_cached(SERVICE_A, db_tool=tool)
    assert out1 is None
    assert out2 == ["recovered"]
    assert tool._get_tables.await_count == 2


@pytest.mark.asyncio
async def test_list_tables_ttl_expiry():
    tool = _mock_db_tool(tables=["a"])
    await list_db_tables_cached(SERVICE_A, db_tool=tool, ttl_seconds=0.01)
    time.sleep(0.05)
    await list_db_tables_cached(SERVICE_A, db_tool=tool, ttl_seconds=0.01)
    assert tool._get_tables.await_count == 2  # ttl 만료 후 재호출


# ────────────────────────────────────────────────────────────────────────
# get_db_snapshot_cached
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_cache_hit():
    snap = {"table": "customers", "rows": [{"id": 1}, {"id": 2}]}
    tool = _mock_db_tool(snapshots={"customers": snap})
    out1 = await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool)
    out2 = await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool)
    assert out1 == snap
    assert out2 == snap
    tool._get_snapshot.assert_awaited_once_with("customers")


@pytest.mark.asyncio
async def test_snapshot_cache_isolation_by_table():
    """같은 service, 다른 table → 별개 cache entry."""
    snap_c = {"table": "customers", "rows": []}
    snap_o = {"table": "orders", "rows": []}
    tool = _mock_db_tool(snapshots={"customers": snap_c, "orders": snap_o})
    await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool)
    await get_db_snapshot_cached(SERVICE_A, "orders", db_tool=tool)
    assert tool._get_snapshot.await_count == 2


@pytest.mark.asyncio
async def test_snapshot_cache_isolation_by_service():
    tool_a = _mock_db_tool(snapshots={"customers": {"rows": ["a-row"]}})
    tool_b = _mock_db_tool(snapshots={"customers": {"rows": ["b-row"]}})
    out_a = await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool_a)
    out_b = await get_db_snapshot_cached(SERVICE_B, "customers", db_tool=tool_b)
    assert out_a["rows"] == ["a-row"]
    assert out_b["rows"] == ["b-row"]


@pytest.mark.asyncio
async def test_snapshot_failure_returns_none():
    tool = MagicMock()
    tool._get_snapshot = AsyncMock(side_effect=RuntimeError("table not found"))
    out = await get_db_snapshot_cached(SERVICE_A, "x", db_tool=tool)
    assert out is None


@pytest.mark.asyncio
async def test_snapshot_empty_args_returns_none():
    assert await get_db_snapshot_cached("", "customers") is None
    assert await get_db_snapshot_cached(SERVICE_A, "") is None


@pytest.mark.asyncio
async def test_snapshot_ttl_expiry():
    snap = {"table": "x", "rows": []}
    tool = _mock_db_tool(snapshots={"x": snap})
    await get_db_snapshot_cached(SERVICE_A, "x", db_tool=tool, ttl_seconds=0.01)
    time.sleep(0.05)
    await get_db_snapshot_cached(SERVICE_A, "x", db_tool=tool, ttl_seconds=0.01)
    assert tool._get_snapshot.await_count == 2


# ────────────────────────────────────────────────────────────────────────
# cache 관리
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_stats():
    tool = _mock_db_tool()
    await list_db_tables_cached(SERVICE_A, db_tool=tool)
    await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool)
    stats = cache_stats()
    assert "tables" in stats and "snapshot" in stats
    assert stats["tables"]["active"] >= 1
    assert stats["snapshot"]["active"] >= 1


@pytest.mark.asyncio
async def test_clear_db_cache_resets_both():
    tool = _mock_db_tool()
    await list_db_tables_cached(SERVICE_A, db_tool=tool)
    await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool)
    clear_db_cache()
    # 다시 호출 → cache miss → DB 재호출
    await list_db_tables_cached(SERVICE_A, db_tool=tool)
    await get_db_snapshot_cached(SERVICE_A, "customers", db_tool=tool)
    assert tool._get_tables.await_count == 2
    assert tool._get_snapshot.await_count == 2
