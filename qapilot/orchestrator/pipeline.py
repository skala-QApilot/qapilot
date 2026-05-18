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
import re
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
    graph.add_node("doc_import", _doc_import)
    graph.add_node("codebase_scan", _codebase_scan)
    graph.add_node("domain_knowledge", _domain_knowledge)
    graph.add_node("requirement_extract", _requirement_extract)
    graph.add_node("scenario_generate", _scenario_generate)
    graph.add_node("save_scenarios", _save_scenarios)

    graph.add_edge("doc_import", "codebase_scan")
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
        "generate_scenarios": "doc_import",
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
    functions: list[dict] = []
    callgraph: dict[str, list[str]] = {}
    for fi in scan.get("files", []) or []:
        file_path = fi.get("path", "")
        for ep in fi.get("endpoints", []) or []:
            endpoints.append({"file": file_path, **ep})
        for md in fi.get("models", []) or []:
            models.append({"file": file_path, **md})
        for fn in fi.get("functions", []) or []:
            fn_dict = {"file": file_path, **fn}
            line_start = fn.get("line_start", 0)
            line_end = fn.get("line_end", line_start)
            if file_path and line_start:
                try:
                    src_lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
                    excerpt = src_lines[line_start - 1 : min(line_end, line_start + 40) - 1]
                    fn_dict["body_excerpt"] = "\n".join(excerpt)
                except Exception:
                    pass
            functions.append(fn_dict)
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
        ("functions.json", functions),
        ("callgraph.json", callgraph),
        ("manifest.json", manifest),
    ):
        (cache_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── Layer 1A 노드 (generate_scenarios) ────────────────────────────────────────

_SUPPORTED_DOC_SUFFIXES = frozenset({".md", ".pdf", ".docx", ".xlsx", ".xls"})
_DOMAIN_INDEX_DIR = Path(".qapilot") / "domain"
_VERSION_RE = re.compile(r'^(.+?)_v(\d+(?:\.\d+)*)$', re.IGNORECASE)


def _filter_latest_doc_versions(paths: list[Path]) -> list[Path]:
    """버전 접미사(_vN 또는 _vN.M)가 있는 파일은 각 그룹에서 최신 버전만 남긴다."""
    versioned: dict[str, list[tuple[tuple[int, ...], Path]]] = {}
    unversioned: list[Path] = []
    for path in paths:
        m = _VERSION_RE.match(path.stem)
        if m:
            version = tuple(int(x) for x in m.group(2).split("."))
            versioned.setdefault(m.group(1), []).append((version, path))
        else:
            unversioned.append(path)
    result = list(unversioned)
    for _, versions in versioned.items():
        result.append(max(versions, key=lambda x: x[0])[1])
    return result


async def _doc_import(state: PipelineState) -> dict:
    """docs/ 디렉토리의 문서를 Qdrant에 임포트한다.

    이미 임포트된 파일(index.json 존재 + 경로 일치)은 건너뛴다.
    Qdrant 미가동 시 예외를 삼키고 진행한다.
    """
    from qapilot.shared.config import load_config
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    trace_id = state.get("trace_id") or str(_uuid.uuid4())
    config = load_config()
    proj = config.project
    repo_root = Path(proj.root or proj.repo_path or ".")
    docs_dir = repo_root / "docs"

    if not docs_dir.exists():
        return {}

    doc_files = _filter_latest_doc_versions([
        p for p in docs_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _SUPPORTED_DOC_SUFFIXES
    ])

    tool = DomainKnowledgeTool(trace_id=trace_id)
    logger = tool.logger

    for doc_path in sorted(doc_files):
        index_path = _DOMAIN_INDEX_DIR / f"{doc_path.stem}.index.json"
        if index_path.exists():
            try:
                saved = json.loads(index_path.read_text(encoding="utf-8"))
                if saved.get("file") == str(doc_path):
                    logger.info("doc_import_skip", file=str(doc_path))
                    continue
            except Exception:
                pass

        try:
            await tool.run(
                ToolInput(
                    trace_id=trace_id,
                    params={"action": "import", "file_path": str(doc_path)},
                )
            )
        except Exception as e:
            logger.warning("doc_import_failed", file=str(doc_path), error=str(e))

    return {}


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

    user_input이 있으면 사용자가 직접 입력한 시나리오 요구사항을 파싱한다.
    user_input이 없으면 Qdrant에 임포트된 PRD 문서에서 요구사항을 검색한다.
    """
    user_input = (state["run_options"].get("user_input") or "").strip()
    if user_input:
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

    # user_input 없음: Qdrant에 저장된 PRD 문서에서 요구사항 검색
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    # docs/ 에서 최신 PRD 파일명만 추출 (버전 필터 적용)
    from qapilot.shared.config import load_config
    config = load_config()
    proj = config.project
    repo_root = Path(proj.root or proj.repo_path or ".")
    docs_dir = repo_root / "docs"
    latest_prd_sources: set[str] = set()
    if docs_dir.exists():
        all_docs = [p for p in docs_dir.rglob("*") if p.is_file() and p.suffix.lower() in _SUPPORTED_DOC_SUFFIXES]
        latest_prd_sources = {
            p.name for p in _filter_latest_doc_versions(all_docs)
            if "prd" in p.name.lower()
        }

    tool = DomainKnowledgeTool(trace_id=state["trace_id"])
    requirements = []
    try:
        result = await tool.run(
            ToolInput(
                trace_id=state["trace_id"],
                params={"action": "search", "query": "기능 요구사항 시스템", "top_k": 30},
            )
        )
        rules = result.result.get("rules", []) or []

        # 최신 PRD 문서 청크만 필터링 (이전 버전 제외)
        prd_rules = [
            r for r in rules
            if r.get("source", "") in latest_prd_sources
        ] if latest_prd_sources else [
            r for r in rules if "prd" in r.get("source", "").lower()
        ]

        _NON_FUNC_KEYWORDS = {"비기능", "성능", "보안", "가용성", "안정성", "확장성"}
        for i, rule in enumerate(prd_rules, start=1):
            section = rule.get("section", "")
            req_type = (
                "non_functional"
                if any(k in section for k in _NON_FUNC_KEYWORDS)
                else "functional"
            )
            domain_area = section or rule.get("source", "").replace(".md", "")
            requirements.append({
                "req_id": f"REQ-{i:03d}",
                "req_type": req_type,
                "content": rule["content"],
                "priority": "medium",
                "domain_area": domain_area,
            })
    except Exception:
        pass

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
    """Layer 1B 산출물을 디스크에 저장한다.

    spec §6.1 정합 + ActionMapping 영속화 (Layer 2 의 전제):
    - .qapilot/generated-code/{tc_id}.js (기존)
    - .qapilot/action-mappings/{tc_id}.json (신규, PR #87 — ActionMapping 디스크 영속화)

    상세 의미·옵션 비교: 이슈 #87 / memory/project_qapilot_pipeline_persistence_layers.md
    """
    import json
    from pathlib import Path

    generated_codes = state.get("generated_codes") or []
    action_mappings = state.get("action_mappings") or []

    code_dir = Path(".qapilot") / "generated-code"
    am_dir = Path(".qapilot") / "action-mappings"
    code_dir.mkdir(parents=True, exist_ok=True)
    am_dir.mkdir(parents=True, exist_ok=True)

    saved_code_paths: list[str] = []
    for code_obj in generated_codes:
        tc_id = code_obj.get("tc_id")
        if not tc_id:
            continue
        path = code_dir / f"{tc_id}.js"
        path.write_text(code_obj.get("code", ""), encoding="utf-8")
        saved_code_paths.append(str(path))

    # 신규 — ActionMapping 디스크 영속화 (Layer 2 의 _load_scenarios_for_test 가 읽음)
    for am in action_mappings:
        tc_id = am.get("tc_id")
        if not tc_id:
            continue
        path = am_dir / f"{tc_id}.json"
        path.write_text(json.dumps(am, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "saved_code_paths": saved_code_paths,
        "status": "completed",
    }


# ── Layer 2 헬퍼: 디스크 로드 + 토폴로지 정렬 ──────────────────────────────────


def _load_json_files(directory: Path) -> list[dict]:
    """디렉토리의 *.json 모두 로드 (정렬). 실패한 파일은 skip."""
    if not directory.exists():
        return []
    items: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return items


def _topo_sort_scenarios(scenarios: list[dict]) -> list[dict]:
    """`depends_on` (PR #82 신규) 기반 토폴로지 정렬.

    순환 의존 또는 unknown 의존은 graceful — 정렬 불가 항목은 마지막에 둠.
    """
    by_id = {s.get("ts_id"): s for s in scenarios if s.get("ts_id")}
    sorted_list: list[dict] = []
    visited: set[str] = set()

    def visit(ts_id: str, stack: set[str]) -> None:
        if ts_id in visited or ts_id not in by_id or ts_id in stack:
            return
        stack.add(ts_id)
        for dep in by_id[ts_id].get("depends_on") or []:
            visit(dep, stack)
        stack.discard(ts_id)
        visited.add(ts_id)
        sorted_list.append(by_id[ts_id])

    for ts in scenarios:
        ts_id = ts.get("ts_id")
        if ts_id:
            visit(ts_id, set())
    # 의존성 정보 없는 항목 (ts_id 없는 등) 도 마지막에 포함
    for ts in scenarios:
        if ts not in sorted_list:
            sorted_list.append(ts)
    return sorted_list


def _collect_tc_tags(scenarios: list[dict]) -> dict[str, list[str]]:
    """TC id → tag 목록 dict."""
    result: dict[str, list[str]] = {}
    for ts in scenarios:
        for tc in ts.get("test_cases") or []:
            tc_id = tc.get("tc_id")
            if tc_id:
                result[tc_id] = tc.get("tags") or []
    return result


def _ts_id_of_tc(tc_id: str, scenarios: list[dict]) -> str:
    """TC id → 소속 TS id. 매칭 실패 시 'unknown'."""
    for ts in scenarios:
        for tc in ts.get("test_cases") or []:
            if tc.get("tc_id") == tc_id:
                return ts.get("ts_id") or "unknown"
    return "unknown"


async def _load_scenarios_for_test(state: PipelineState) -> dict:
    """Layer 2 진입 노드 — 디스크에서 scenarios + action_mappings + generated_codes 로드.

    spec §6.1 정합. PipelineState 가 휘발성이므로 `qapilot test` 단독 실행 시 디스크에서 채움.
    필터 적용: run_options.scenario_ids / tags. depends_on 기반 토폴로지 정렬.
    """
    import uuid as _uuid

    trace_id = state.get("trace_id") or str(_uuid.uuid4())

    scenarios = _load_json_files(Path(".qapilot") / "scenarios")
    action_mappings = _load_json_files(Path(".qapilot") / "action-mappings")

    # generated_codes 는 .js 파일 — 검증·디버그용 (실행에 필수 X)
    codes_dir = Path(".qapilot") / "generated-code"
    generated_codes: list[dict] = []
    if codes_dir.exists():
        for path in sorted(codes_dir.glob("*.js")):
            generated_codes.append({
                "tc_id": path.stem,
                "code": path.read_text(encoding="utf-8"),
                "syntax_valid": True,
                "self_fix_count": 0,
            })

    # 필터 — run_options.scenario_ids (TS 단위)
    scenario_ids = state["run_options"].get("scenario_ids") or []
    if scenario_ids:
        scenarios = [s for s in scenarios if s.get("ts_id") in scenario_ids]
        valid_tc_ids = {
            tc.get("tc_id")
            for s in scenarios
            for tc in s.get("test_cases") or []
        }
        action_mappings = [a for a in action_mappings if a.get("tc_id") in valid_tc_ids]
        generated_codes = [c for c in generated_codes if c.get("tc_id") in valid_tc_ids]

    # 필터 — run_options.tags (TC 단위)
    tags = state["run_options"].get("tags") or []
    if tags:
        tc_tags_map = _collect_tc_tags(scenarios)
        valid_tc_ids = {
            tc_id for tc_id, tc_tags in tc_tags_map.items()
            if any(t in tc_tags for t in tags)
        }
        action_mappings = [a for a in action_mappings if a.get("tc_id") in valid_tc_ids]
        generated_codes = [c for c in generated_codes if c.get("tc_id") in valid_tc_ids]
        # 시나리오는 그대로 두되 test_cases 필터링은 후속 단계가 alignment 처리

    # depends_on 토폴로지 정렬 (PR #82)
    scenarios = _topo_sort_scenarios(scenarios)

    return {
        "trace_id": trace_id,
        "scenarios": scenarios,
        "action_mappings": action_mappings,
        "generated_codes": generated_codes,
        "current_layer": "L2",
    }


async def _test_execution(state: PipelineState) -> dict:
    """Layer 2 핵심 노드 — async_playwright + APITrace/UITest/DBTest 통합 호출.

    spec §3.3 FR-006 의 X-Trace-Id 헤더 주입 (browser.new_context).
    각 TC 별 결과를 .qapilot/results/{trace_id}/{ts_id}/{tc_id}/ 에 저장 (spec §6.1, L2 디스크 캐시).
    DBTestTool 은 QAPILOT_MODULE_URL 미설정 시 graceful skip.
    """
    from playwright.async_api import async_playwright

    from qapilot.shared.config import load_config
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.api_trace_tool import APITraceTool
    from qapilot.tools.db_test_tool import DBTestTool
    from qapilot.tools.ui_test_tool import UITestTool

    trace_id = state["trace_id"]
    scenarios = state.get("scenarios") or []
    action_mappings = state.get("action_mappings") or []
    cfg = load_config()
    headless = bool(getattr(cfg.test, "headless", True)) if hasattr(cfg, "test") else True
    target_url = getattr(cfg.project, "target_url", "") if hasattr(cfg, "project") else ""

    results_root = Path(".qapilot") / "results" / trace_id

    ui_results: list[dict] = []
    api_results: list[dict] = []
    db_results: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            extra_http_headers={"X-Trace-Id": trace_id}
        )
        page = await context.new_page()

        try:
            for am in action_mappings:
                tc_id = am.get("tc_id") or "unknown"
                ts_id = _ts_id_of_tc(tc_id, scenarios)
                tc_dir = results_root / ts_id / tc_id
                screenshots_dir = tc_dir / "screenshots"
                tc_dir.mkdir(parents=True, exist_ok=True)

                ui_res = await _run_ui_with_trace(
                    page=page,
                    tc_id=tc_id,
                    action_mapping=am,
                    target_url=target_url,
                    screenshots_dir=screenshots_dir,
                    trace_id=trace_id,
                    UITestTool=UITestTool,
                    APITraceTool=APITraceTool,
                    ToolInput=ToolInput,
                )
                ui_results.append(ui_res["ui_result"])
                api_results.append(ui_res["api_result"])

                db_res = await _run_db_test_safe(
                    tc_id=tc_id,
                    trace_id=trace_id,
                    DBTestTool=DBTestTool,
                    ToolInput=ToolInput,
                )
                db_results.append(db_res)

                # L2 디스크 저장 (spec §6.1)
                (tc_dir / "ui_result.json").write_text(
                    json.dumps(ui_res["ui_result"], ensure_ascii=False, indent=2), "utf-8"
                )
                (tc_dir / "api_result.json").write_text(
                    json.dumps(ui_res["api_result"], ensure_ascii=False, indent=2), "utf-8"
                )
                (tc_dir / "db_result.json").write_text(
                    json.dumps(db_res, ensure_ascii=False, indent=2), "utf-8"
                )
        finally:
            await context.close()
            await browser.close()

    return {
        "ui_results": ui_results,
        "api_results": api_results,
        "db_results": db_results,
    }


async def _run_ui_with_trace(
    *,
    page,
    tc_id: str,
    action_mapping: dict,
    target_url: str,
    screenshots_dir: Path,
    trace_id: str,
    UITestTool,
    APITraceTool,
    ToolInput,
) -> dict:
    """APITraceTool 의 listener 등록 (즉시 반환) + UITestTool 실행 후 api calls 재계산."""
    apt = APITraceTool(trace_id=trace_id)
    apt_out = await apt.run(ToolInput(trace_id=trace_id, params={"page": page, "tc_id": tc_id}))
    api_trace = apt_out.result["api_trace"]

    ui_tool = UITestTool(trace_id=trace_id)
    ui_out = await ui_tool.run(
        ToolInput(
            trace_id=trace_id,
            params={
                "page": page,
                "action_mapping": action_mapping,
                "tc_id": tc_id,
                "target_url": target_url,
                "screenshot_dir": str(screenshots_dir),
            },
        )
    )

    # listener 가 누적한 calls 를 dict 로 강제 변환 (TypedDict 인스턴스 dict-like)
    api_trace_dict = dict(api_trace)
    api_trace_dict["total_calls"] = len(api_trace_dict.get("calls") or [])
    api_trace_dict["error_calls"] = sum(
        1 for c in (api_trace_dict.get("calls") or [])
        if c.get("status_code", 0) >= 400
    )

    return {
        "ui_result": ui_out.result["ui_result"],
        "api_result": api_trace_dict,
    }


async def _run_db_test_safe(*, tc_id: str, trace_id: str, DBTestTool, ToolInput) -> dict:
    """DBTestTool graceful — QAPILOT_MODULE_URL 미설정 또는 호출 실패 시 빈 결과."""
    try:
        tool = DBTestTool(trace_id=trace_id)
        out = await tool.run(ToolInput(trace_id=trace_id, params={"tc_id": tc_id}))
        return dict(out.result.get("db_test") or {
            "tc_id": tc_id, "snapshots": [], "summary": "DB test 결과 비어있음",
        })
    except Exception as e:
        return {
            "tc_id": tc_id,
            "snapshots": [],
            "summary": f"DBTest skip: {type(e).__name__}: {e}",
        }


async def _cross_check(state: PipelineState) -> dict:
    """Layer 2 정합성 검증 노드 — TC 별 CrossCheckAgent 호출.

    UI/API/DB 결과를 TC id 기준 매칭하여 각 Agent 호출. has_mismatch 어느 하나라도 True 면
    state["has_mismatch"]=True (Layer 3 분기 결정).
    """
    from qapilot.agents.cross_check_agent import CrossCheckAgent
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    ui_results = state.get("ui_results") or []
    api_results = state.get("api_results") or []
    db_results = state.get("db_results") or []

    # tc_id 별 인덱싱
    ui_map = {r.get("tc_id"): r for r in ui_results if r.get("tc_id")}
    api_map = {r.get("tc_id"): r for r in api_results if r.get("tc_id")}
    db_map = {r.get("tc_id"): r for r in db_results if r.get("tc_id")}

    cross_check_results: list[dict] = []
    any_mismatch = False

    for tc_id in ui_map.keys():
        ui_result = ui_map.get(tc_id, {})
        api_trace = api_map.get(tc_id, {})
        db_result = db_map.get(tc_id, {})

        agent = CrossCheckAgent(trace_id=trace_id)
        try:
            output = await agent.run(
                AgentInput(
                    trace_id=trace_id,
                    context={
                        "ui_result": ui_result,
                        "api_trace": api_trace,
                        "db_result": db_result,
                    },
                    params={"tc_id": tc_id},
                )
            )
            cc = dict(output.result.get("cross_check") or {})
            if not cc:
                cc = {
                    "tc_id": tc_id, "match_score": 0.0, "matched_fields": 0,
                    "mismatched_fields": 0, "mismatches": [], "has_mismatch": False,
                }
            cross_check_results.append(cc)
            if cc.get("has_mismatch"):
                any_mismatch = True
        except Exception as e:
            cross_check_results.append({
                "tc_id": tc_id, "match_score": 0.0, "matched_fields": 0,
                "mismatched_fields": 0, "mismatches": [],
                "has_mismatch": False,
                "error": f"CrossCheck skip: {type(e).__name__}: {e}",
            })

    return {
        "cross_check_results": cross_check_results,
        "has_mismatch": any_mismatch,
    }


async def _defect_classify(state: PipelineState) -> dict:
    """Layer 3 첫 노드 — DefectClassifierAgent 호출 (FR-009).

    본체 stub 상태 (NotImplementedError) — graceful 흡수 후 fallback `unknown` 카테고리.
    cross_check_results 의 has_mismatch=True 인 TC 만 분류 대상.
    DefectClassifier 본체 머지 시 자동 정상 동작.
    """
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    cross_check_results = state.get("cross_check_results") or []
    ui_results = state.get("ui_results") or []
    ui_map = {r.get("tc_id"): r for r in ui_results if r.get("tc_id")}

    defect_results: list[dict] = []

    for cc in cross_check_results:
        if not cc.get("has_mismatch"):
            continue
        tc_id = cc.get("tc_id", "unknown")
        ui_result = ui_map.get(tc_id, {})

        defect_dict = await _classify_defect_safe(
            trace_id=trace_id,
            tc_id=tc_id,
            cross_check=cc,
            ui_result=ui_result,
            AgentInput=AgentInput,
        )
        defect_results.append(defect_dict)

    return {"defect_results": defect_results}


async def _classify_defect_safe(*, trace_id, tc_id, cross_check, ui_result, AgentInput) -> dict:
    """DefectClassifier graceful — stub raise 시 fallback unknown."""
    from qapilot.agents.defect_classifier_agent import DefectClassifierAgent

    try:
        agent = DefectClassifierAgent(trace_id=trace_id)
        output = await agent.run(
            AgentInput(
                trace_id=trace_id,
                context={
                    "cross_check": cross_check,
                    "ui_result": ui_result,
                },
                params={
                    "tc_id": tc_id,
                    "mismatches": cross_check.get("mismatches") or [],
                    "ui_steps": ui_result.get("steps") or [],
                },
            )
        )
        result = output.result
        # 단일 결과 또는 list 모두 수용
        if isinstance(result.get("defect_classification"), dict):
            return dict(result["defect_classification"])
        if isinstance(result.get("defect_results"), list) and result["defect_results"]:
            return dict(result["defect_results"][0])
    except Exception as e:
        # stub NotImplementedError 또는 LLM 실패 graceful
        return {
            "tc_id": tc_id,
            "defect_type": "unknown",
            "sub_type": "",
            "description": f"DefectClassifier skip: {type(e).__name__}: {e}",
            "rule_based": False,
        }

    return {
        "tc_id": tc_id,
        "defect_type": "unknown",
        "sub_type": "",
        "description": "DefectClassifier 결과 형식 불명",
        "rule_based": False,
    }


async def _root_cause(state: PipelineState) -> dict:
    """Layer 3 두 번째 노드 — RootCauseAgent 호출 (FR-010).

    cross_check_results 의 mismatch TC 별로 원인 후보 Top-N 추론.
    Cross-check 결과의 error_code / summary / mismatches 를 params 로 전달.
    """
    from qapilot.agents.root_cause_agent import RootCauseAgent
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    cross_check_results = state.get("cross_check_results") or []

    root_cause_results: list[dict] = []

    for cc in cross_check_results:
        if not cc.get("has_mismatch"):
            continue
        tc_id = cc.get("tc_id", "unknown")

        try:
            agent = RootCauseAgent(trace_id=trace_id)
            output = await agent.run(
                AgentInput(
                    trace_id=trace_id,
                    context={},
                    params={
                        "tc_id": tc_id,
                        "error_code": cc.get("error_code") or "",
                        "summary": cc.get("summary") or "",
                        "mismatches": cc.get("mismatches") or [],
                        "has_mismatch": True,
                    },
                )
            )
            root_causes = output.result.get("root_causes") or []
            # 단일 또는 list — list 첫 번째를 결과로
            if isinstance(root_causes, list) and root_causes:
                root_cause_results.append(dict(root_causes[0]))
            elif isinstance(root_causes, dict):
                root_cause_results.append(dict(root_causes))
            else:
                root_cause_results.append({
                    "tc_id": tc_id, "candidates": [],
                })
        except Exception as e:
            root_cause_results.append({
                "tc_id": tc_id,
                "candidates": [],
                "error": f"RootCause skip: {type(e).__name__}: {e}",
            })

    return {"root_cause_results": root_cause_results}


async def _fix_recommend(state: PipelineState) -> dict:
    """Layer 3 세 번째 노드 — FixRecommenderAgent 호출 (FR-011).

    RootCauseResult.candidates 를 받아 해결 가이드 생성.
    """
    from qapilot.agents.fix_recommender_agent import FixRecommenderAgent
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    root_cause_results = state.get("root_cause_results") or []

    fix_results: list[dict] = []

    for rc in root_cause_results:
        tc_id = rc.get("tc_id", "unknown")
        candidates = rc.get("candidates") or []

        try:
            agent = FixRecommenderAgent(trace_id=trace_id)
            output = await agent.run(
                AgentInput(
                    trace_id=trace_id,
                    context={},
                    params={
                        "tc_id": tc_id,
                        "candidates": candidates,
                    },
                )
            )
            fr_list = output.result.get("fix_results") or []
            if isinstance(fr_list, list) and fr_list:
                fix_results.append(dict(fr_list[0]))
            elif isinstance(fr_list, dict):
                fix_results.append(dict(fr_list))
            else:
                fix_results.append({"tc_id": tc_id, "suggestions": []})
        except Exception as e:
            fix_results.append({
                "tc_id": tc_id,
                "suggestions": [],
                "error": f"FixRecommender skip: {type(e).__name__}: {e}",
            })

    return {"fix_results": fix_results}


async def _report(state: PipelineState) -> dict:
    """Layer 2/3 마지막 노드 — 임시 markdown summary (Report Tool 본체 stub 상태).

    spec §6.1 의 .qapilot/reports/{trace_id}.md 저장. v1.5 의 단일 형식 spec FR-012:
    - 실행 요약
    - 실패 케이스 (step별 에러, TOOL_UI_* prefix)
    - Cross-check 요약
    - 장애 분류 (FR-009, Layer 3)
    - 원인 분석 (FR-010, Top-N candidates)
    - 해결 방안 (FR-011, suggestions)

    ReportTool 본체 머지 후 별도 PR 에서 호출로 교체.
    """
    trace_id = state["trace_id"]
    ui_results = state.get("ui_results") or []
    cross_check_results = state.get("cross_check_results") or []
    defect_results = state.get("defect_results") or []
    root_cause_results = state.get("root_cause_results") or []
    fix_results = state.get("fix_results") or []

    total = len(ui_results)
    passed = sum(1 for r in ui_results if r.get("status") == "pass")
    failed = total - passed

    lines: list[str] = [
        f"# QApilot 테스트 리포트",
        f"",
        f"- trace_id: `{trace_id}`",
        f"- 총 TC: {total}",
        f"- pass: {passed}",
        f"- fail: {failed}",
        f"",
        f"## 1. 실패 케이스 (UI 스텝 기준)",
    ]
    for ui in ui_results:
        if ui.get("status") != "fail":
            continue
        lines.append(f"### {ui.get('tc_id')}")
        for step in ui.get("steps") or []:
            if step.get("status") != "fail":
                continue
            lines.append(
                f"- step {step.get('step_no')} ({step.get('action')}): "
                f"`{step.get('error') or ''}`"
            )

    lines.extend(["", "## 2. Cross-check 정합성"])
    for cc in cross_check_results:
        marker = "⚠️" if cc.get("has_mismatch") else "✅"
        lines.append(
            f"- {marker} `{cc.get('tc_id')}` "
            f"match_score={cc.get('match_score', 0):.2f} "
            f"mismatches={cc.get('mismatched_fields', 0)}"
        )

    if defect_results:
        lines.extend(["", "## 3. 장애 분류 (FR-009)"])
        for d in defect_results:
            lines.append(
                f"- `{d.get('tc_id')}` → **{d.get('defect_type', 'unknown')}**"
                + (f" / {d.get('sub_type')}" if d.get('sub_type') else "")
                + (f" — {d.get('description', '')[:120]}" if d.get('description') else "")
            )

    if root_cause_results:
        lines.extend(["", "## 4. 원인 분석 (FR-010)"])
        for rc in root_cause_results:
            tc_id = rc.get("tc_id")
            candidates = rc.get("candidates") or []
            lines.append(f"### `{tc_id}` — Top {len(candidates)} 후보")
            for cand in candidates[:5]:
                lines.append(
                    f"- rank {cand.get('rank', '?')}: {cand.get('cause', '')[:160]} "
                    f"(confidence={cand.get('confidence', 0):.2f})"
                )
                file_path = cand.get('affected_file')
                if file_path:
                    line_no = cand.get('affected_line')
                    suffix = f":{line_no}" if line_no else ""
                    lines.append(f"  - 위치: `{file_path}{suffix}`")

    if fix_results:
        lines.extend(["", "## 5. 해결 방안 (FR-011)"])
        for fr in fix_results:
            tc_id = fr.get("tc_id")
            suggestions = fr.get("suggestions") or []
            lines.append(f"### `{tc_id}` — 제안 {len(suggestions)}개")
            for s in suggestions[:3]:
                lines.append(
                    f"- `{s.get('file_path', 'N/A')}:{s.get('line_number', '?')}` — "
                    f"{s.get('description', '')[:160]}"
                )

    reports_dir = Path(".qapilot") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{trace_id}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "status": "completed",
    }
