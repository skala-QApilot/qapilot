"""LangGraph StateGraph 정의.

단일 그래프에서 command 값에 따라 진입점이 분기된다.
- generate_scenarios: Layer 1A (코드 스캔 → 시나리오 생성 → 저장 → END)
- generate_code: Layer 1B (시나리오 로드 → 액션 매핑 → 코드 생성 → 저장 → END)
- test: Layer 2~3 (시나리오+코드 로드 → 테스트 실행 → 리포트 → END)

HITL은 별도 모듈로 두지 않고, generate_scenarios 와 generate_code 명령 사이에서
사용자가 대시보드를 통해 자유롭게 시나리오를 수정·삭제할 수 있도록 한다.

3-Layer 영속화 정책 (Phase 1, project_qapilot_pipeline_persistence_layers.md):
- L1 메모리 PipelineState: 모든 노드 결과 (단일 실행 흐름)
- L2 디스크 캐시 (.qapilot/): codebase-index, scenarios, generated-code, results, reports
- L3 서버 DB: Phase 2 별도 PR

담당: A
Created: 2026-05-07
"""

import json
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


# ── Layer 1A 헬퍼: spec §6.1 디스크 캐시 ──────────────────────────────────────
def _save_codebase_index_to_disk(scan: dict) -> None:
    """spec §6.1 의 .qapilot/codebase-index/ 4파일 저장 (L2 디스크 캐시).

    Tool 본체의 `.qapilot/manifest.json` (증분 분석 추적용) 과 독립.
    본 manifest 는 spec §6.1 정합용으로 codebase-index/ 안에 둔다.
    """
    cache_dir = Path(".qapilot") / "codebase-index"
    cache_dir.mkdir(parents=True, exist_ok=True)

    endpoints: list[dict] = []
    models: list[dict] = []
    callgraph: dict[str, list[str]] = {}
    for fi in scan.get("files", []) or []:
        file_path = fi.get("path", "")
        for ep in fi.get("endpoints", []) or []:
            endpoints.append({"file": file_path, **ep})
        for md in fi.get("models", []) or []:
            models.append({"file": file_path, **md})
        callgraph[file_path] = list(fi.get("dependencies", []) or [])

    manifest = {
        "scan_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "commit_hash": (scan.get("git_diff") or {}).get("commit_hash", ""),
        "framework": scan.get("framework"),
        "language": scan.get("language"),
        "file_count": len(scan.get("files", []) or []),
        "endpoint_count": int(scan.get("endpoint_count", 0) or 0),
    }

    for filename, payload in (
        ("endpoints.json", endpoints),
        ("models.json", models),
        ("callgraph.json", callgraph),
        ("manifest.json", manifest),
    ):
        (cache_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── Layer 1A 노드 (generate_scenarios) ────────────────────────────────────────


async def _codebase_scan(state: PipelineState) -> dict:
    """FR-000 CodebaseScannerTool 호출 + spec §6.1 디스크 캐시."""
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool

    trace_id = state.get("trace_id") or str(_uuid.uuid4())
    trigger = state["run_options"].get("trigger") or "init"

    tool = CodebaseScannerTool(trace_id=trace_id)
    result = await tool.run(
        ToolInput(trace_id=trace_id, params={"trigger": trigger})
    )
    scan: dict[str, Any] = result.result["scan_result"]

    # L2: spec §6.1 정합 디스크 캐시 (Tool 본체 무수정)
    _save_codebase_index_to_disk(scan)

    return {
        "trace_id": trace_id,
        "scan_result": scan,
        "current_layer": "L1A",
    }


async def _domain_knowledge(state: PipelineState) -> dict:
    """FR-001 DomainKnowledgeTool search 호출 → domain_rules.

    scan_result 의 framework + 첫 endpoint 경로들을 합쳐 query 생성.
    Qdrant 가 L2 역할 수행(별도 디스크 저장 X).
    """
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    scan = state.get("scan_result") or {}
    framework = scan.get("framework") or ""
    paths: list[str] = []
    for fi in (scan.get("files") or [])[:5]:
        for ep in (fi.get("endpoints") or [])[:2]:
            path = ep.get("path") or ep.get("name") or ""
            if path:
                paths.append(path)
    query = " ".join(filter(None, [framework] + paths[:5])) or "테스트 시나리오"

    tool = DomainKnowledgeTool(trace_id=state["trace_id"])
    try:
        result = await tool.run(
            ToolInput(
                trace_id=state["trace_id"],
                params={"action": "search", "query": query, "top_k": 10},
            )
        )
        rules = result.result.get("rules", []) or []
    except Exception:
        # 도메인 인덱스 미준비 시 rules 빈 채로 진행 (시나리오 품질 ↓ 가능)
        rules = []

    return {"domain_rules": rules}


async def _requirement_extract(state: PipelineState) -> dict:
    """FR-024 RequirementExtractorAgent 호출.

    run_options.user_input 이 있으면 PRD 텍스트로 사용. 없으면 skip (빈 requirements).
    RequirementExtractorAgent 는 document_text 누락 시 AgentExecutionError 발생하므로
    노드 측에서 미리 분기.
    """
    user_input = (state["run_options"].get("user_input") or "").strip()
    if not user_input:
        return {"requirements": []}

    from qapilot.agents.requirement_extractor_agent import RequirementExtractorAgent
    from qapilot.shared.schemas import AgentInput

    agent = RequirementExtractorAgent(trace_id=state["trace_id"])
    output = await agent.run(
        AgentInput(
            trace_id=state["trace_id"],
            context={"domain_rules": state.get("domain_rules") or []},
            params={"document_text": user_input, "existing_count": 0},
        )
    )
    requirements = output.result.get("requirements", []) or []
    return {"requirements": requirements}


async def _scenario_generate(state: PipelineState) -> dict:
    """FR-002 ScenarioGeneratorAgent 호출 → TS/TC/TV 시나리오 목록."""
    from qapilot.agents.scenario_generator.agent import ScenarioGeneratorAgent
    from qapilot.shared.schemas import AgentInput

    trigger = state["run_options"].get("trigger") or "code_change"
    affected_only = trigger == "code_change"

    agent = ScenarioGeneratorAgent(trace_id=state["trace_id"])
    output = await agent.run(
        AgentInput(
            trace_id=state["trace_id"],
            context={
                "scan_result": state.get("scan_result"),
                "domain_rules": state.get("domain_rules") or [],
                "requirements": state.get("requirements") or [],
            },
            params={"trigger": trigger, "affected_only": affected_only},
        )
    )
    scenarios = output.result.get("scenarios", []) or []
    return {"scenarios": scenarios}


async def _save_scenarios(state: PipelineState) -> dict:
    """생성된 시나리오를 .qapilot/scenarios/{ts_id}.json 에 저장 (spec §6.1, L2)."""
    scenarios = state.get("scenarios") or []
    scenarios_dir = Path(".qapilot") / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    for idx, ts in enumerate(scenarios, start=1):
        ts_id = ts.get("ts_id") or f"TS-{idx:03d}"
        path = scenarios_dir / f"{ts_id}.json"
        path.write_text(
            json.dumps(ts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        saved_paths.append(str(path))

    return {
        "saved_scenario_paths": saved_paths,
        "status": "completed",
    }


async def _load_scenarios_for_codegen(state: PipelineState) -> dict:
    """.qapilot/scenarios/ 에서 시나리오를 로드한다 (코드 생성용)."""
    import json
    from pathlib import Path
    
    scenarios_dir = Path(".qapilot") / "scenarios"
    if not scenarios_dir.exists():
        return {"scenarios": [], "error": "시나리오 디렉토리가 없습니다."}

    scenario_ids = state["run_options"].get("scenario_ids") or []
    scenarios = []

    for path in scenarios_dir.glob("*.json"):
        if path.name == "raw": continue
        if path.name == "regression": continue
        
        ts_id = path.stem
        if scenario_ids and ts_id not in scenario_ids:
            continue
        try:
            ts = json.loads(path.read_text(encoding="utf-8"))
            scenarios.append(ts)
        except Exception:
            pass

    scenarios.sort(key=lambda x: x.get("ts_id", ""))
    
    # scan_result 를 디스크 캐시에서 복원 (ActionMapperAgent 가 사용)
    scan_result = None
    try:
        endpoints_path = Path(".qapilot") / "codebase-index" / "endpoints.json"
        if endpoints_path.exists():
            endpoints = json.loads(endpoints_path.read_text(encoding="utf-8"))
            scan_result = {"files": [{"path": "mock", "endpoints": endpoints}]}
    except Exception:
        pass

    return {
        "scenarios": scenarios,
        "scan_result": scan_result,
        "current_layer": "L1B",
    }


async def _action_mapping(state: PipelineState) -> dict:
    from qapilot.agents.action_mapper_agent import ActionMapperAgent
    from qapilot.shared.schemas import AgentInput

    agent = ActionMapperAgent(trace_id=state.get("trace_id"))
    result = await agent.run(
        AgentInput(
            trace_id=state.get("trace_id") or "",
            context={
                "scenarios": state.get("scenarios") or [],
                "scan_result": state.get("scan_result")
            },
            params={},
        )
    )

    agent_logs = state.get("agent_logs", []) + [result.metadata.model_dump()]

    return {
        "action_mappings": result.result.get("action_mappings", []),
        "agent_logs": agent_logs,
    }


async def _code_generate(state: PipelineState) -> dict:
    from qapilot.agents.code_generator_agent import CodeGeneratorAgent
    from qapilot.shared.schemas import AgentInput

    agent = CodeGeneratorAgent(trace_id=state.get("trace_id"))
    result = await agent.run(
        AgentInput(
            trace_id=state.get("trace_id") or "",
            context={
                "action_mappings": state.get("action_mappings", []),
                "scenarios": state.get("scenarios", [])
            },
            params={},
        )
    )

    agent_logs = state.get("agent_logs", []) + [result.metadata.model_dump()]

    return {
        "generated_codes": result.result.get("generated_codes", []),
        "agent_logs": agent_logs,
    }


async def _save_codes(state: PipelineState) -> dict:
    """생성된 Playwright 코드를 .qapilot/generated-code/ 에 저장한다."""
    import json
    from pathlib import Path

    generated_codes = state.get("generated_codes") or []
    out_dir = Path(".qapilot") / "generated-code"
    out_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []
    for code_obj in generated_codes:
        tc_id = code_obj.get("tc_id")
        if not tc_id:
            continue
        path = out_dir / f"{tc_id}.js"
        path.write_text(code_obj.get("code", ""), encoding="utf-8")
        saved_paths.append(str(path))

    return {
        "saved_code_paths": saved_paths,
        "status": "completed",
    }


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
