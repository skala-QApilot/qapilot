"""파이프라인 실행 엔트리포인트.

Author: A
Created: 2026-05-07
"""

import tempfile
from pathlib import Path

import structlog

from qapilot.modules.trace_module import TraceModule
from qapilot.orchestrator.pipeline import build_pipeline
from qapilot.orchestrator.state import PipelineState
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import RunOptions


async def run_pipeline(
    options: RunOptions,
    service_id: str = "",
    trace_id: str | None = None,
    staging_url: str | None = None,
    test_account: dict | None = None,
    domain_files: list[dict] | None = None,
    target_root: str | None = None,
    # CLI 하위 호환 — qapilot_dir 를 직접 지정하면 그대로 사용, 없으면 temp dir 사용.
    qapilot_dir: str | Path | None = None,
) -> PipelineState:
    """파이프라인을 실행하고 최종 상태를 반환한다.

    Args:
        options: 실행 옵션.
        service_id: 서비스 ID. DB 에서 시나리오 등을 조회할 때 사용.
        trace_id: 외부에서 발급된 trace_id. None 이면 새로 발급한다.
        staging_url: SUT base URL.
        test_account: SUT 인증 정보.
        domain_files: 도메인 문서 메타 리스트.
        target_root: SUT 코드베이스 root (Spring service.target_root). `_resolve_project_root`
            가 cfg.project.root 다음, qapilot_dir derive 전 우선 사용.
        qapilot_dir: CLI 하위 호환 — 지정 시 해당 디렉토리 사용, 없으면 temp dir.
    """
    trace_id = trace_id or TraceModule.generate_trace_id()
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    logger = get_logger(source="orchestrator", trace_id=trace_id)
    logger.info(
        "pipeline_start",
        command=options["command"],
        trigger=options.get("trigger"),
        filter=options.get("filter"),
    )

    # 작업 디렉토리: 명시적으로 지정된 경우(CLI) 그대로 사용, 아니면 temp dir.
    if qapilot_dir is not None:
        work_dir = str(Path(qapilot_dir).resolve())
    else:
        work_dir = tempfile.mkdtemp(prefix=f"qapilot_{trace_id[:8]}_")

    graph = build_pipeline()
    app = graph.compile()

    initial_state: PipelineState = {
        "run_options": options,
        "trace_id": trace_id,
        "service_id": service_id,
        "status": "running",
        "current_layer": "",
        "error": None,
        "qapilot_dir": work_dir,
        "staging_url": staging_url or "",
        "target_root": target_root if isinstance(target_root, str) and target_root else None,
        "test_account": test_account if isinstance(test_account, dict) else None,
        "domain_files": domain_files if isinstance(domain_files, list) else None,
        # Layer 1A
        "scan_result": None,
        "domain_rules": [],
        "requirements": [],
        "scenarios": [],
        "saved_scenario_paths": [],
        # Layer 1B
        "action_mappings": [],
        "generated_codes": [],
        "saved_code_paths": [],
        # Layer 2
        "ui_results": [],
        "api_results": [],
        "db_results": [],
        "cross_check_results": [],
        "has_mismatch": False,
        # Layer 3
        "root_cause_results": [],
        "fix_results": [],
        # 리포트
        "report_path": None,
        # 메타
        "agent_logs": [],
        "total_cost": 0.0,
    }

    try:
        result = await app.ainvoke(initial_state)
    except Exception as e:
        logger.error("pipeline_failed", error=f"{type(e).__name__}: {e}")
        raise

    result["total_cost"] = round(
        sum(log.get("cost_usd", 0.0) for log in result.get("agent_logs", [])), 6
    )
    logger.info(
        "pipeline_complete",
        status=result.get("status"),
        total_cost_usd=result["total_cost"],
    )
    return result
