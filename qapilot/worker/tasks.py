"""Celery 태스크 — 파이프라인 1회 실행을 분산 처리한다.

asyncio.create_task 직접 호출의 분산 버전. trace_store / DB / S3 mirror 흐름은 그대로.

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import asyncio
from typing import Any

from qapilot.orchestrator.runner import run_pipeline
from qapilot.shared.logger import get_logger
from qapilot.worker.celery_app import celery_app

_logger = get_logger("worker.tasks")


@celery_app.task(name="qapilot.run_pipeline", bind=True, max_retries=0)
def run_pipeline_task(
    self,
    qapilot_dir: str,
    trace_id: str,
    options: dict[str, Any],
    staging_url: str | None = None,
) -> dict[str, Any]:
    """파이프라인 1회 실행. self.request.id 가 Celery task id (runs.task_id 에 저장).

    내부 run_pipeline 은 async — 워커 컨텍스트에서 새 event loop 로 돌린다.
    각 워커 프로세스는 자체 loop 를 가지므로 asyncio.run 안전.
    """
    _logger.info("celery_task_started", trace_id=trace_id, task_id=self.request.id)
    try:
        return asyncio.run(run_pipeline(qapilot_dir=qapilot_dir, trace_id=trace_id, options=options, staging_url=staging_url))
    except Exception as e:
        _logger.error("celery_task_failed", trace_id=trace_id, error=str(e))
        raise


def revoke_pipeline(task_id: str) -> bool:
    """워커가 실행 중인 태스크를 중단. broadcast 라 어느 워커가 갖고 있든 시그널 도달.

    terminate=True → 즉시 SIGTERM (asyncio 의 CancelledError 와 유사 효과).
    """
    if not task_id:
        return False
    try:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        return True
    except Exception as e:
        _logger.warning("celery_revoke_failed", task_id=task_id, error=str(e))
        return False
