"""구조화 로깅 모듈.

로그를 3개 sink로 출력한다:
- 콘솔(stdout): 사람이 읽기 좋은 형식 (레벨/이벤트 색상)
- ``.qapilot/logs/qapilot.log``: JSON Lines, 자정마다 ``qapilot-{date}.log`` 로 회전(30일 보관)
- ``.qapilot/logs/traces/{trace_id}.jsonl``: trace_id 별 JSON Lines

모든 로그에 ``source`` 와 ``trace_id`` 를 바인딩한다. trace 파일에는 trace_id 가
있는 이벤트만 기록한다.

Author: A
Created: 2026-05-07
"""

import json
import logging
import logging.handlers
from pathlib import Path

import structlog

_LOGS_DIR = Path(".qapilot/logs")
_TRACES_DIR = _LOGS_DIR / "traces"
_BACKUP_DAYS = 30
_configured = False


def _trace_file_sink(logger, method_name, event_dict):
    """structlog processor: trace_id 가 있으면 해당 trace 의 .jsonl 에 append 한다.

    event_dict 를 변형하지 않고 그대로 반환한다 (pass-through). 로깅 자체가
    실패해도 본 작업을 막지 않도록 OSError 는 무시한다.
    """
    trace_id = event_dict.get("trace_id")
    if trace_id:
        try:
            _TRACES_DIR.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event_dict, ensure_ascii=False, default=str)
            with open(_TRACES_DIR / f"{trace_id}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return event_dict


def _rotated_namer(default_name: str) -> str:
    """회전 파일명을 ``qapilot.log.2026-05-11`` → ``qapilot-2026-05-11.log`` 로 바꾼다."""
    base, ext, date = default_name.rsplit(".", 2)
    return f"{base}-{date}.{ext}"


def setup_logger(level: int = logging.INFO) -> None:
    """structlog + 표준 logging 을 3-sink 로 설정한다. 중복 호출은 무시한다."""
    global _configured
    if _configured:
        return

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # structlog 로거에서 곧장 발생한 로그에 적용되는 프로세서 체인.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _trace_file_sink,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 표준 logging 으로 흘러든 외부 로그(uvicorn 등)에 적용되는 사전 체인.
    foreign_pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
    ]

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
    )
    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        _LOGS_DIR / "qapilot.log",
        when="midnight",
        backupCount=_BACKUP_DAYS,
        encoding="utf-8",
    )
    file_handler.setFormatter(json_formatter)
    file_handler.namer = _rotated_namer

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    _configured = True


def get_logger(source: str, trace_id: str | None = None) -> structlog.stdlib.BoundLogger:
    """source 와 trace_id 가 바인딩된 로거를 반환한다.

    ``setup_logger()`` 가 아직 호출되지 않았다면 기본 설정으로 자동 호출한다
    (단일 Agent 단독 실행 등에서 로깅이 무음 처리되지 않도록).
    """
    if not _configured:
        setup_logger()
    logger = structlog.get_logger()
    logger = logger.bind(source=source)
    if trace_id:
        logger = logger.bind(trace_id=trace_id)
    return logger
