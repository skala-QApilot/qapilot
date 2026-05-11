"""LangGraph StateGraph 정의.

단일 그래프에서 command 값에 따라 진입점이 분기된다.
- generate_scenarios: 시나리오 생성 (코드스캔 → 도메인지식 → 요구사항추출 → 시나리오생성 → HITL 리뷰 큐 적재 → END)
- generate_code: 코드 생성 (승인된 시나리오 로드 → 액션매핑 → 코드생성 → END)
- test: Layer 2~3 (시나리오 로드 → 테스트 → 리포트 → END)

담당: A
Created: 2026-05-07
"""

from langgraph.graph import END, START, StateGraph

from qapilot.orchestrator.state import PipelineState


def build_pipeline() -> StateGraph:
    """파이프라인 그래프를 구성하고 반환한다."""
    graph = StateGraph(PipelineState)

    # ── 노드 등록 ──
    # Layer 1 - Scenarios
    graph.add_node("codebase_scan", _codebase_scan)
    graph.add_node("domain_knowledge", _domain_knowledge)
    graph.add_node("requirement_extract", _requirement_extract)
    graph.add_node("scenario_generate", _scenario_generate)
    graph.add_node("hitl_review", _hitl_review)
    
    # Layer 1 - Code
    graph.add_node("load_approved_scenarios", _load_approved_scenarios)
    graph.add_node("action_mapping", _action_mapping)
    graph.add_node("code_generate", _code_generate)

    # Layer 2~3
    graph.add_node("load_scenarios", _load_scenarios)
    graph.add_node("test_execution", _test_execution)
    graph.add_node("cross_check", _cross_check)
    graph.add_node("defect_classify", _defect_classify)
    graph.add_node("root_cause", _root_cause)
    graph.add_node("fix_recommend", _fix_recommend)
    graph.add_node("report", _report)

    # ── 진입점 분기 ──
    def route_start(state: PipelineState):
        cmd = state["run_options"]["command"]
        if cmd == "generate_scenarios":
            return "codebase_scan"
        elif cmd == "generate_code":
            return "load_approved_scenarios"
        else:
            return "load_scenarios"

    graph.add_conditional_edges(START, route_start)

    # ── Layer 1 - Scenarios 엣지 ──
    graph.add_edge("codebase_scan", "domain_knowledge")
    graph.add_edge("domain_knowledge", "requirement_extract")
    graph.add_edge("requirement_extract", "scenario_generate")
    graph.add_edge("scenario_generate", "hitl_review")
    graph.add_edge("hitl_review", END)

    # ── Layer 1 - Code 엣지 ──
    graph.add_edge("load_approved_scenarios", "action_mapping")
    graph.add_edge("action_mapping", "code_generate")
    graph.add_edge("code_generate", END)

    # ── Layer 2~3 엣지 ──
    graph.add_edge("load_scenarios", "test_execution")
    graph.add_edge("test_execution", "cross_check")
    graph.add_conditional_edges(
        "cross_check",
        lambda state: "defect_classify" if state["has_mismatch"] else "report",
    )
    graph.add_edge("defect_classify", "root_cause")
    graph.add_edge("root_cause", "fix_recommend")
    graph.add_edge("fix_recommend", "report")
    graph.add_edge("report", END)

    return graph


# ── 노드 함수 (스켈레톤) ──


async def _codebase_scan(state: PipelineState) -> dict:
    raise NotImplementedError


async def _domain_knowledge(state: PipelineState) -> dict:
    raise NotImplementedError


async def _requirement_extract(state: PipelineState) -> dict:
    raise NotImplementedError


async def _scenario_generate(state: PipelineState) -> dict:
    raise NotImplementedError


async def _hitl_review(state: PipelineState) -> dict:
    raise NotImplementedError


async def _load_approved_scenarios(state: PipelineState) -> dict:
    raise NotImplementedError


async def _action_mapping(state: PipelineState) -> dict:
    raise NotImplementedError


async def _code_generate(state: PipelineState) -> dict:
    raise NotImplementedError


async def _load_scenarios(state: PipelineState) -> dict:
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
