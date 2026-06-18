"""Celery 앱 생성 — broker / result backend 모두 REDIS_URL.

워커 가동:
    celery -A qapilot.worker.celery_app worker --loglevel=info --concurrency=4

로컬 디버깅 (별 워커 프로세스 없이 동기 실행):
    CELERY_TASK_ALWAYS_EAGER=true qapilot ui
    → send_task 호출이 즉시 실행되어 결과 반환 (Celery 코드 경로 검증용).

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import os

from celery import Celery

_BROKER_URL = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true"

celery_app = Celery(
    "qapilot",
    broker=_BROKER_URL,
    backend=_BROKER_URL,
    include=["qapilot.worker.tasks", "qapilot.rl.tasks"],
)

celery_app.conf.update(
    # 한 태스크의 진행 상황은 Redis pub/sub (qapilot.messaging) 로 별도 발행되므로
    # Celery state tracking 은 최소로.
    task_track_started=True,
    task_acks_late=True,                       # 워커 죽어도 task 가 다시 다른 워커로 재할당
    worker_prefetch_multiplier=1,              # 긴 파이프라인 — 1 task per worker slot
    task_default_queue="qapilot",
    # 로컬 디버깅용 eager 모드
    task_always_eager=_EAGER,
    task_eager_propagates=_EAGER,              # eager 시 예외도 호출자로 직전파
    timezone="UTC",
    enable_utc=True,
)
