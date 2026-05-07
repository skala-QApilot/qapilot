"""Trace ID 모듈.

UUID v4를 발급하고 UI → API → DB 전 계층에 전파한다.

담당: A
Created: 2026-05-07
"""

import uuid


class TraceModule:
    """Trace ID 발급 및 전파 모듈."""

    @staticmethod
    def generate_trace_id() -> str:
        """UUID v4 형식의 trace_id를 발급한다."""
        return str(uuid.uuid4())
