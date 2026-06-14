"""CDP Screencast 프레임 브로드캐스트 공유 저장소.

pipeline (UI 테스트 실행) 와 API WebSocket 엔드포인트가 공유하는 구독자 레지스트리.
두 모듈 간 import cycle 방지를 위해 shared 패키지에 위치한다.

흐름: pipeline 이 page.on('screencastFrame') 콜백에서 broadcast_frame() 호출 →
trace_id 별 모든 구독자 큐에 base64 JPEG 프레임을 넣음 → stream_router 의
WebSocket 핸들러가 큐에서 꺼내 클라이언트로 전송.
"""
from __future__ import annotations

import asyncio

# trace_id → [asyncio.Queue(maxsize=N), ...]
_subscribers: dict[str, list[asyncio.Queue[str]]] = {}


def broadcast_frame(trace_id: str, frame_b64: str) -> None:
    """CDP 프레임(base64 JPEG)을 해당 trace_id 의 모든 구독자 큐에 전달한다.

    큐가 가득 차면 가장 오래된 프레임을 드롭하고 새 프레임을 넣는다 (백프레셔 방지 —
    느린 클라이언트가 실행 파이프라인을 블로킹하지 않도록).
    """
    for q in list(_subscribers.get(trace_id) or []):
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(frame_b64)
        except asyncio.QueueFull:
            pass


def register_subscriber(trace_id: str, q: asyncio.Queue[str]) -> None:
    """WebSocket 클라이언트 연결 시 구독자 큐를 등록한다."""
    _subscribers.setdefault(trace_id, []).append(q)


def unregister_subscriber(trace_id: str, q: asyncio.Queue[str]) -> None:
    """WebSocket 클라이언트 연결 종료 시 구독자 큐를 해제한다."""
    subs = _subscribers.get(trace_id, [])
    try:
        subs.remove(q)
    except ValueError:
        pass
    if not subs:
        _subscribers.pop(trace_id, None)


def has_subscribers(trace_id: str) -> bool:
    """해당 trace_id 를 보고 있는 WebSocket 클라이언트가 있는지."""
    return bool(_subscribers.get(trace_id))
