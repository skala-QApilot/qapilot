"""RL Trainer — Celery 태스크 (설계서 §7 'Trainer (배치)').

런이 완료되면 백그라운드로 사후 학습을 돌린다. 파이프라인 완료 지점에서

    from qapilot.rl.tasks import learn_from_run_task
    learn_from_run_task.delay(run_id, service_id)

한 줄로 트리거하면 된다 (생성 경로에 영향 없음 = 안전). 동기 디버깅은
`CELERY_TASK_ALWAYS_EAGER=true`.

Author: juhwan
"""

from __future__ import annotations

from typing import Any

from qapilot.rl.service import RLService
from qapilot.shared.logger import get_logger
from qapilot.worker.celery_app import celery_app

_logger = get_logger("rl.tasks")


@celery_app.task(name="qapilot.rl.learn_from_run", bind=True, max_retries=2, default_retry_delay=30)
def learn_from_run_task(
    self,
    run_id: str,
    service_id: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """완료된 런으로부터 RL 사후 학습(밴딧/도메인뱅크/경험 갱신)."""
    try:
        summary = RLService().learn_from_run(run_id, service_id=service_id, domain=domain)
        _logger.info(
            "rl_learn_done",
            run_id=run_id,
            status=summary.get("status"),
            scenarios=summary.get("scenarios"),
            patterns_added=summary.get("patterns_added"),
        )
        return summary
    except Exception as exc:  # noqa: BLE001
        _logger.warning("rl_learn_failed", run_id=run_id, error=str(exc))
        raise self.retry(exc=exc)
