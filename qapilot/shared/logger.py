"""구조화 JSON 로깅 모듈.

모든 로그에 trace_id를 필수 포함한다.

Author: 공통
Created: 2026-05-07
"""

import structlog


def setup_logger() -> None:
    """structlog 초기 설정."""
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
    )


def get_logger(source: str, trace_id: str | None = None) -> structlog.BoundLogger:
    """source와 trace_id가 바인딩된 로거를 반환한다."""
    logger = structlog.get_logger()
    logger = logger.bind(source=source)
    if trace_id:
        logger = logger.bind(trace_id=trace_id)
    return logger
