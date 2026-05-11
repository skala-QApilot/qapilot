"""LangGraph StateGraph 정의.

단일 그래프에서 command 값에 따라 진입점이 분기된다.
- generate_scenarios: Layer 1A (코드 스캔 → 시나리오 생성 → 저장 → END)
- generate_code: Layer 1B (시나리오 로드 → 액션 매핑 → 코드 생성 → 저장 → END)
- test: Layer 2~3 (시나리오+코드 로드 → 테스트 실행 → 리포트 → END)

HITL은 별도 모듈로 두지 않고, generate_scenarios 와 generate_code 명령 사이에서
사용자가 대시보드를 통해 자유롭게 시나리오를 수정·삭제할 수 있도록 한다.

담당: A
Created: 2026-05-07
"""

from langgraph.graph import END, START, StateGraph

from qapilot.orchestrator.state import PipelineState


def build_pipeline() -> StateGraph:
    """파이프라인 그래프를 구성하고 반환한다."""
    graph = StateGraph(PipelineState)

    # ═══════════════════════════════════════════════════
    # Layer 1A — generate_scenarios
    # ═══════════════════════════════════════════════════
    graph.add_node("codebase_scan", _codebase_scan)
    graph.add_node("domain_knowledge", _domain_knowledge)
    graph.add_node("requirement_extract", _requirement_extract)
    graph.add_node("scenario_generate", _scenario_generate)
    graph.add_node("save_scenarios", _save_scenarios)

    graph.add_edge("codebase_scan", "domain_knowledge")
    graph.add_edge("domain_knowledge", "requirement_extract")
    graph.add_edge("requirement_extract", "scenario_generate")
    graph.add_edge("scenario_generate", "save_scenarios")
    graph.add_edge("save_scenarios", END)

    # ═══════════════════════════════════════════════════
    # Layer 1B — generate_code
    # ═══════════════════════════════════════════════════
    graph.add_node("load_scenarios_for_codegen", _load_scenarios_for_codegen)
    graph.add_node("action_mapping", _action_mapping)
    graph.add_node("code_generate", _code_generate)
    graph.add_node("save_codes", _save_codes)

    graph.add_edge("load_scenarios_for_codegen", "action_mapping")
    graph.add_edge("action_mapping", "code_generate")
    graph.add_edge("code_generate", "save_codes")
    graph.add_edge("save_codes", END)

    # ═══════════════════════════════════════════════════
    # Layer 2~3 — test
    # ═══════════════════════════════════════════════════
    graph.add_node("load_scenarios_for_test", _load_scenarios_for_test)
    graph.add_node("test_execution", _test_execution)
    graph.add_node("cross_check", _cross_check)
    graph.add_node("defect_classify", _defect_classify)
    graph.add_node("root_cause", _root_cause)
    graph.add_node("fix_recommend", _fix_recommend)
    graph.add_node("report", _report)

    graph.add_edge("load_scenarios_for_test", "test_execution")
    graph.add_edge("test_execution", "cross_check")
    graph.add_conditional_edges(
        "cross_check",
        lambda state: "defect_classify" if state["has_mismatch"] else "report",
    )
    graph.add_edge("defect_classify", "root_cause")
    graph.add_edge("root_cause", "fix_recommend")
    graph.add_edge("fix_recommend", "report")
    graph.add_edge("report", END)

    # ═══════════════════════════════════════════════════
    # 진입점 분기 (3개 명령)
    # ═══════════════════════════════════════════════════
    graph.add_conditional_edges(
        START,
        lambda state: _entry_point(state["run_options"]["command"]),
    )

    return graph


def _entry_point(command: str) -> str:
    """command 값에 따른 진입 노드를 반환한다."""
    entry_map = {
        "generate_scenarios": "codebase_scan",
        "generate_code": "load_scenarios_for_codegen",
        "test": "load_scenarios_for_test",
    }
    if command not in entry_map:
        raise ValueError(f"지원하지 않는 command: {command}")
    return entry_map[command]


# ═══════════════════════════════════════════════════
# 노드 함수 (스켈레톤)
# ═══════════════════════════════════════════════════


async def _codebase_scan(state: PipelineState) -> dict:
    raise NotImplementedError


async def _domain_knowledge(state: PipelineState) -> dict:
    raise NotImplementedError


async def _requirement_extract(state: PipelineState) -> dict:
    raise NotImplementedError


async def _scenario_generate(state: PipelineState) -> dict:
    raise NotImplementedError


async def _save_scenarios(state: PipelineState) -> dict:
    """생성된 시나리오를 .qapilot/scenarios/ 에 저장한다."""
    raise NotImplementedError


async def _load_scenarios_for_codegen(state: PipelineState) -> dict:
    """.qapilot/scenarios/ 에서 시나리오를 로드한다 (코드 생성용)."""
    raise NotImplementedError


async def _load_approved_scenarios(state: PipelineState) -> dict:
    raise NotImplementedError


async def _action_mapping(state: PipelineState) -> dict:
    raise NotImplementedError


async def _code_generate(state: PipelineState) -> dict:
    raise NotImplementedError


async def _save_codes(state: PipelineState) -> dict:
    """생성된 Playwright 코드를 .qapilot/generated-code/ 에 저장한다."""
    raise NotImplementedError


async def _load_scenarios_for_test(state: PipelineState) -> dict:
    """.qapilot/scenarios/ + .qapilot/generated-code/ 를 로드한다 (테스트용)."""
    raise NotImplementedError


async def _test_execution(state: PipelineState) -> dict:
    raise NotImplementedError


async def _cross_check(state: PipelineState) -> dict:
    raise NotImplementedError


async def _defect_classify(state: PipelineState) -> dict:
    raise NotImplementedError


async def _root_cause(state: PipelineState) -> dict:
    raise NotImplementedError


async def _fix_recommend(state: PipelineState) -> dict:
    raise NotImplementedError


async def _report(state: PipelineState) -> dict:
    raise NotImplementedError
