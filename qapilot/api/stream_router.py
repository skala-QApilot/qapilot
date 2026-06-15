"""UI 테스트 실행 중 CDP Screencast 실시간 스트리밍 WebSocket 엔드포인트.

클라이언트가 ws://.../api/agent/ws/stream/{trace_id} 로 연결하면
테스트 실행 중 브라우저 CDP 프레임(base64 JPEG)을 실시간으로 수신한다.

인증: trace_id 가 암묵적 접근 제어 역할을 한다 (실행 측에서 발급한 ID 를 아는
경우만 의미 있는 프레임 수신). HTTP 미들웨어(_trace_id_middleware)는 WebSocket
scope 에 적용되지 않으므로 별도 인증 우회 처리 불필요.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from qapilot.shared.logger import get_logger
from qapilot.shared.stream_store import register_subscriber, unregister_subscriber

router = APIRouter()
logger = get_logger("api.stream")


@router.websocket("/ws/stream/{trace_id}")
async def stream_ws(ws: WebSocket, trace_id: str) -> None:
    """CDP Screencast 프레임을 실시간으로 WebSocket 클라이언트에 전달한다.

    프레임은 base64 인코딩된 JPEG 문자열로 전송된다. 프레임이 없을 때는
    짧은 슬라이스로 폴링하며 연결을 유지한다.

    Python 3.12+ 주의: ``asyncio.CancelledError`` 는 ``BaseException`` 이라
    ``except Exception`` 에 잡히지 않는다. 또한 ``asyncio.wait_for`` 는 취소를
    내부에서 즉시 re-raise 하므로(연결 직후 1005 close 의 원인), 여기서는
    ``get_nowait`` + ``sleep`` 폴링으로 그 동작을 우회한다.
    """
    await ws.accept()
    logger.info("cdp_ws_connected", trace_id=trace_id)
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=5)
    register_subscriber(trace_id, q)
    try:
        while True:
            try:
                frame_b64 = q.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue

            try:
                await ws.send_text(frame_b64)
            except WebSocketDisconnect:
                logger.info("cdp_ws_disconnected", trace_id=trace_id)
                break
            except Exception as exc:
                logger.warning("cdp_ws_send_error", trace_id=trace_id, error=repr(exc))
                break

    except asyncio.CancelledError:
        logger.info("cdp_ws_cancelled", trace_id=trace_id)
        raise
    except WebSocketDisconnect:
        logger.info("cdp_ws_disconnected", trace_id=trace_id)
    except Exception as exc:
        logger.warning("cdp_ws_error", trace_id=trace_id, error=repr(exc))
    finally:
        unregister_subscriber(trace_id, q)
        logger.info("cdp_ws_cleaned", trace_id=trace_id)
