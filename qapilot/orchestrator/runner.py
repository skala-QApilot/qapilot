"""파이프라인 실행 엔트리포인트.

CLI와 API에서 호출하는 진입점. trace_id 는 호출부에서 주입할 수 있고,
주지 않으면 여기서 발급한다 (API 미들웨어 등이 발급한 값을 전달하는 경로 대비).

담당: A
Created: 2026-05-07
"""

from pathlib import Path

import structlog

from qapilot.modules.trace_module import TraceModule
from qapilot.orchestrator.pipeline import build_pipeline
from qapilot.orchestrator.state import PipelineState
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import RunOptions


async def run_pipeline(
    options: RunOptions,
    qapilot_dir: str | Path,
    trace_id: str | None = None,
    staging_url: str | None = None,
) -> PipelineState:
    """파이프라인을 실행하고 최종 상태를 반환한다.

    Args:
        options: 실행 옵션.
        qapilot_dir: 산출물 루트 (절대경로 권장). 파이프라인 전 단계가 이 디렉토리
            기준으로 scenarios / generated-code / results 등을 read/write 한다.
            CLI: ``Path.cwd() / ".qapilot"``, API: 서비스별 등록 경로.
        trace_id: 외부에서 발급된 trace_id. None 이면 새로 발급한다.
        staging_url: SUT base URL. SaaS 호출 경로에서 service.stagingUrl 을 그대로 흘려준다.
            None/빈 문자열이면 Layer 2 의 _test_execution 이 cfg.project.target_url 로 fallback.

    Returns:
        파이프라인 최종 상태.
    """
    trace_id = trace_id or TraceModule.generate_trace_id()
    # 실행 스코프 전체에 trace_id 를 노출한다 (이후 호출되는 모든 로그에 자동 포함).
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    logger = get_logger(source="orchestrator", trace_id=trace_id)
    logger.info(
        "pipeline_start",
        command=options["command"],
        trigger=options.get("trigger"),
        filter=options.get("filter"),
    )

    graph = build_pipeline()
    app = graph.compile()

    initial_state: PipelineState = {
        "run_options": options,
        "trace_id": trace_id,
        "status": "running",
        "current_layer": "",
        "error": None,
        "qapilot_dir": str(Path(qapilot_dir).resolve()),
        "staging_url": staging_url or "",
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

    # Agent 실행 로그의 비용을 합산해 파이프라인 총 비용으로 집계한다.
    result["total_cost"] = round(
        sum(log.get("cost_usd", 0.0) for log in result.get("agent_logs", [])), 6
    )
    logger.info(
        "pipeline_complete",
        status=result.get("status"),
        total_cost_usd=result["total_cost"],
    )
    return result
