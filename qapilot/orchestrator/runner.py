"""파이프라인 실행 엔트리포인트.

CLI와 API에서 호출하는 진입점.

담당: A
Created: 2026-05-07
"""

from qapilot.orchestrator.pipeline import build_pipeline
from qapilot.orchestrator.state import PipelineState
from qapilot.shared.schemas import RunOptions


async def run_pipeline(options: RunOptions) -> PipelineState:
    """파이프라인을 실행하고 최종 상태를 반환한다."""
    graph = build_pipeline()
    app = graph.compile()

    initial_state: PipelineState = {
        "run_options": options,
        "trace_id": "",  # trace_module에서 발급
        "status": "running",
        "current_layer": "",
        "error": None,
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
        "defect_results": [],
        "root_cause_results": [],
        "fix_results": [],
        # 리포트
        "report_path": None,
        # 메타
        "agent_logs": [],
        "total_cost": 0.0,
    }

    result = await app.ainvoke(initial_state)
    return result
