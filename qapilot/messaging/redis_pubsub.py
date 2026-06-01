"""Redis pub/sub — sync publish (worker 측) + async subscribe (SSE 측).

채널 규칙: `run:<trace_id>` — 한 run 의 모든 이벤트 (tc_result/status/screenshot).

REDIS_URL 미설정 시 graceful no-op. SSE 도 1초 폴링 fallback 으로 살아남는다.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, AsyncIterator

from qapilot.shared.logger import get_logger

_logger = get_logger("messaging.redis")

_sync_client: Any = None  # redis.Redis | None
_sync_lock = threading.Lock()
_sync_failed = False


def _get_sync_client() -> Any:
    global _sync_client, _sync_failed
    if _sync_client is not None:
        return _sync_client
    if _sync_failed:
        return None
    with _sync_lock:
        if _sync_client is not None:
            return _sync_client
        if _sync_failed:
            return None
        url = os.environ.get("REDIS_URL")
        if not url:
            _logger.info("redis_disabled", reason="REDIS_URL not set")
            _sync_failed = True
            return None
        try:
            import redis

            _sync_client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2.0)
            _sync_client.ping()
            _logger.info("redis_sync_ready", url_host=_redact(url))
            return _sync_client
        except Exception as e:
            _logger.warning("redis_sync_open_failed", error=str(e))
            _sync_failed = True
            return None


def publish(channel: str, payload: dict) -> None:
    """worker → Redis. 실패 시 warn 만, 호출자는 신경 안 씀."""
    client = _get_sync_client()
    if client is None:
        return
    try:
        client.publish(channel, json.dumps(payload, ensure_ascii=False, default=str))
    except Exception as e:
        _logger.warning("redis_publish_failed", channel=channel, error=str(e))


def publish_run_event(trace_id: str, event_type: str, data: dict) -> None:
    """`run:<trace_id>` 채널에 표준 envelope 로 발행."""
    if not trace_id:
        return
    publish(f"run:{trace_id}", {"type": event_type, "data": data})


async def subscribe_run_events(trace_id: str) -> AsyncIterator[dict]:
    """SSE 핸들러용 async iterator. REDIS_URL 없으면 즉시 종료.

    yield value 는 publish 시 보낸 envelope dict ({"type": ..., "data": ...}).
    """
    url = os.environ.get("REDIS_URL")
    if not url or not trace_id:
        return
    try:
        from redis.asyncio import Redis as AsyncRedis

        client = AsyncRedis.from_url(url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(f"run:{trace_id}")
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                raw = msg.get("data")
                try:
                    yield json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    continue
        finally:
            try:
                await pubsub.unsubscribe(f"run:{trace_id}")
                await pubsub.close()
                await client.close()
            except Exception:
                pass
    except Exception as e:
        _logger.warning("redis_subscribe_failed", trace_id=trace_id, error=str(e))


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    prefix, host = url.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else ""
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"
