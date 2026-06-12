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
import os
import re
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from qapilot.db.code_reader import load_codebase_index, load_latest_action_mapping, load_latest_generated_code
from qapilot.db.code_writer import upsert_action_mapping, upsert_codebase_index, upsert_generated_code
from qapilot.db.rtm_writer import write_rtm_version
from qapilot.db.change_request_writer import upsert_change_request
from qapilot.db.scenario_writer import upsert_scenario_version
from qapilot.db.tc_result_writer import insert_tc_artifact, upsert_tc_result
from qapilot.orchestrator.state import PipelineState
from qapilot.shared import progress
from qapilot.shared.logger import get_logger
from qapilot.shared.trace_store import load_trace

logger = get_logger(source="orchestrator")
from qapilot.storage import s3_client
from qapilot.tools.frontend_dom_scanner import scan_frontend_directory


def _qapilot_path(state: PipelineState, *parts: str) -> Path:
    """state["qapilot_dir"] 기준의 절대경로를 반환한다.

    파이프라인 내부의 모든 파일 I/O 는 반드시 이 helper 를 거쳐야 한다.
    ``Path(".qapilot")/...`` 같은 CWD 의존 경로는 금지 — 다른 서비스의
    데이터를 덮어쓸 수 있다.
    """
    base = state.get("qapilot_dir")
    if not base:
        raise RuntimeError(
            "PipelineState에 qapilot_dir 가 비어 있다. runner.run_pipeline(...) 호출부가 "
            "qapilot_dir 인자를 누락한 것이다."
        )
    return Path(base).joinpath(*parts)


def _progress_node(name: str, fn):
    """노드 진입 시 progress 이벤트 1건 발행 후 원래 노드를 실행하는 래퍼.

    Layer 1A/1B 노드에만 적용 — 생성 오버레이 프로그레스바를 실제 노드 전환과 맞춘다.
    발행 실패/Redis 미설정은 progress 모듈이 알아서 흡수하므로 노드 실행에 영향 없다.
    """
    async def wrapped(state: PipelineState) -> dict:
        trigger = state["run_options"].get("trigger")
        progress.node(state.get("trace_id"), name, trigger)
        return await fn(state)

    return wrapped


def build_pipeline() -> StateGraph:
    """파이프라인 그래프를 구성하고 반환한다."""
    graph = StateGraph(PipelineState)

    # ═══════════════════════════════════════════════════
    # Layer 1A — generate_scenarios
    # ═══════════════════════════════════════════════════
    graph.add_node("doc_import", _progress_node("doc_import", _doc_import))
    graph.add_node("codebase_scan", _progress_node("codebase_scan", _codebase_scan))
    graph.add_node("domain_knowledge", _progress_node("domain_knowledge", _domain_knowledge))
    graph.add_node("requirement_extract", _progress_node("requirement_extract", _requirement_extract))
    graph.add_node("scenario_generate", _progress_node("scenario_generate", _scenario_generate))
    graph.add_node("save_scenarios", _progress_node("save_scenarios", _save_scenarios))

    graph.add_edge("doc_import", "codebase_scan")
    graph.add_edge("codebase_scan", "domain_knowledge")
    graph.add_edge("domain_knowledge", "requirement_extract")
    graph.add_edge("requirement_extract", "scenario_generate")
    graph.add_edge("scenario_generate", "save_scenarios")
    graph.add_edge("save_scenarios", END)

    # ═══════════════════════════════════════════════════
    # Layer 1B — generate_code
    # ═══════════════════════════════════════════════════
    graph.add_node("load_scenarios_for_codegen", _progress_node("load_scenarios_for_codegen", _load_scenarios_for_codegen))
    graph.add_node("action_mapping", _progress_node("action_mapping", _action_mapping))
    graph.add_node("code_generate", _progress_node("code_generate", _code_generate))
    graph.add_node("save_codes", _progress_node("save_codes", _save_codes))

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
    graph.add_node("root_cause", _root_cause)
    graph.add_node("fix_recommend", _fix_recommend)
    graph.add_node("report", _report)

    graph.add_edge("load_scenarios_for_test", "test_execution")
    graph.add_edge("test_execution", "cross_check")
    graph.add_conditional_edges(
        "cross_check",
        lambda state: "root_cause" if state["has_mismatch"] else "report",
    )
    graph.add_edge("root_cause", "fix_recommend")
    graph.add_edge("fix_recommend", "report")
    graph.add_edge("report", END)

    # ═══════════════════════════════════════════════════
    # prd_only_experiment — PRD-only TS + doc-search TC + codebase-aware TV
    # ═══════════════════════════════════════════════════
    graph.add_node("doc_import_exp",            _doc_import)
    graph.add_node("requirement_extract_exp",   _requirement_extract)
    graph.add_node("codebase_scan_exp",         _codebase_scan)
    graph.add_node("ts_generate_prd_only",      _ts_generate_prd_only)
    graph.add_node("tc_generate_doc_search",    _tc_generate_doc_search)
    graph.add_node("tv_generate_codebase_aware", _tv_generate_codebase_aware)
    graph.add_node("save_experiment_scenarios", _save_experiment_scenarios)

    graph.add_edge("doc_import_exp",            "requirement_extract_exp")
    graph.add_edge("requirement_extract_exp",   "codebase_scan_exp")
    graph.add_edge("codebase_scan_exp",         "ts_generate_prd_only")
    graph.add_edge("ts_generate_prd_only",      "tc_generate_doc_search")
    graph.add_edge("tc_generate_doc_search",    "tv_generate_codebase_aware")
    graph.add_edge("tv_generate_codebase_aware", "save_experiment_scenarios")
    graph.add_edge("save_experiment_scenarios", END)

    # ═══════════════════════════════════════════════════
    # 진입점 분기 (4개 명령)
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
        "prd_only_experiment": "doc_import_exp",
    }
    if command not in entry_map:
        raise ValueError(f"지원하지 않는 command: {command}")
    return entry_map[command]


# ═══════════════════════════════════════════════════
# 노드 함수 (스켈레톤)
# ═══════════════════════════════════════════════════


# ── Layer 1A 헬퍼: spec §6.1 디스크 캐시 ──────────────────────────────────────
def _save_codebase_index_to_disk(scan: dict, state: PipelineState) -> None:
    """spec §6.1 의 .qapilot/codebase-index/ 4파일 저장 (L2 디스크 캐시).

    Tool 본체의 `.qapilot/manifest.json` (증분 분석 추적용) 과 독립.
    본 manifest 는 spec §6.1 정합용으로 codebase-index/ 안에 둔다.
    """
    cache_dir = _qapilot_path(state, "codebase-index")
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
            if not fn_dict.get("body_excerpt"):
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

    indices: list[tuple[str, Any]] = [
        ("endpoints.json", endpoints),
        ("models.json", models),
        ("functions.json", functions),
        ("callgraph.json", callgraph),
        ("manifest.json", manifest),
    ]

    frontend_elements = list(scan.get("frontend_elements") or [])
    if not frontend_elements:
        project_root = _resolve_project_root(state)
        if project_root is not None:
            frontend_elements = scan_frontend_directory(project_root)
        else:
            get_logger("orchestrator").warning(
                "frontend_index_project_root_unresolved",
                qapilot_dir=state.get("qapilot_dir"),
            )
    indices.append(
        (
            "frontend.json",
            {
                "version": 1,
                "element_count": len(frontend_elements),
                "elements": frontend_elements,
            },
        )
    )

    for filename, payload in indices:
        (cache_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # PR-17 — DB+S3 mirror. service_id 는 trace.json 에서.
    trace = load_trace(state["trace_id"]) or {}
    service_id = trace.get("service_id")
    if service_id:
        commit_hash = manifest.get("commit_hash") or None
        file_count = manifest.get("file_count")
        for filename, payload in indices:
            kind = filename.replace(".json", "")
            upsert_codebase_index(service_id, commit_hash, kind, payload,
                                  file_count=file_count if kind == "manifest" else None)


def _resolve_project_root(state: PipelineState) -> Path | None:
    """frontend 스캔용 SUT 루트를 추론한다.

    우선순위:
    1) qapilot.config.yaml 의 project.root / repo_path (CLI 흐름)
    2) state.target_root (SaaS 흐름 — Spring 이 body 로 보낸 service.target_root)
    3) state.qapilot_dir 가 `<root>/.qapilot[/service]` 형태일 때 `<root>` (legacy fallback)
    """
    from qapilot.shared.config import load_config

    cfg = load_config()
    for raw in (cfg.project.root, cfg.project.repo_path):
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.is_dir():
                return path

    target_root = state.get("target_root")
    if target_root:
        path = Path(target_root).expanduser().resolve()
        if path.is_dir():
            return path

    qapilot_dir = state.get("qapilot_dir")
    if not qapilot_dir:
        return None

    path = Path(qapilot_dir).resolve()
    if path.name == ".qapilot" and path.parent.is_dir():
        return path.parent
    if path.parent.name == ".qapilot" and path.parent.parent.is_dir():
        return path.parent.parent
    return None


# ── Layer 1A 노드 (generate_scenarios) ────────────────────────────────────────

_SUPPORTED_DOC_SUFFIXES = frozenset({".md", ".pdf", ".docx", ".xlsx", ".xls"})
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
    """업로드된 도메인 문서 (DB+S3) 와 docs/ 디스크 mirror 를 Qdrant 에 임포트.

    우선순위:
      1) DB (domain_documents) → S3 GET → 임시 파일로 풀어 import — SaaS / UI 흐름.
      2) config.project.root/docs/ — CLI 흐름 호환.
    이미 임포트된 파일(index.json 존재 + 경로 일치)은 건너뛴다.
    """
    # natural_lang/code_change는 문서 재임포트 불필요 — 시나리오 조회/수정만 수행
    trigger = (state["run_options"].get("trigger") or "init")
    if trigger in ("natural_lang", "code_change"):
        return {}

    import tempfile

    from qapilot.db.domain_reader import list_latest_domain_documents, mark_reflected
    from qapilot.shared.config import load_config
    from qapilot.shared.schemas import ToolInput
    from qapilot.storage import s3_client
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    trace_id = state.get("trace_id") or str(_uuid.uuid4())
    tool = DomainKnowledgeTool(trace_id=trace_id)
    logger = tool.logger

    # Qdrant 컬렉션이 없으면 index.json skip 조건을 무시하고 전체 재적재
    from qapilot.tools.domain_knowledge._store import VectorStore
    collection_alive = await VectorStore(logger).collection_exists()
    if not collection_alive:
        logger.warning("qdrant_collection_missing_reimport", name="domain_knowledge")

    async def _import_path(doc_path: Path, _service_id: str | None = None, document_id: str | None = None) -> bool:
        """문서를 Qdrant 에 임포트. 반환값은 '문서가 임베딩에 반영되어 있는지' (스킵 포함).

        document_id 가 있으면(DB+S3 경로) 캐시의 document_id 와 비교해 "동일 버전"
        여부를 판단한다. document_id 가 없는 mirror/docs 경로는 기존 경로 문자열 비교로 폴백한다.
        _service_id 가 있으면 Qdrant 청크 payload 에 service_id 를 태깅해 멀티테넌트 검색 필터링에 사용한다.

        실패 시에만 False — 이미 임포트되어 스킵한 경우도 반영된 상태이므로 True.
        """
        index_path = _qapilot_path(state, "domain", f"{doc_path.stem}.index.json")
        if collection_alive and index_path.exists():
            try:
                saved = json.loads(index_path.read_text(encoding="utf-8"))
                if document_id is not None:
                    if saved.get("document_id") == document_id:
                        logger.info("doc_import_skip", file=str(doc_path), document_id=document_id)
                        return True
                elif saved.get("file") == str(doc_path):
                    logger.info("doc_import_skip", file=str(doc_path))
                    return True
            except Exception:
                pass
        try:
            params: dict = {"action": "import", "file_path": str(doc_path), "document_id": document_id}
            if _service_id:
                params["service_id"] = _service_id
            await tool.run(ToolInput(trace_id=trace_id, params=params))
            return True
        except Exception as e:
            logger.warning("doc_import_failed", file=str(doc_path), error=str(e))
            return False

    # (1) DB + S3 — UI 에서 업로드한 PRD/정책 문서가 진실의 원천.
    trace = load_trace(state["trace_id"]) or {}
    service_id = trace.get("service_id")
    imported_from_saas = 0
    if service_id:
        for doc in list_latest_domain_documents(service_id):
            filename = doc.get("filename") or ""
            s3_key = doc.get("s3_key") or ""
            if not (filename and s3_key):
                continue
            if Path(filename).suffix.lower() not in _SUPPORTED_DOC_SUFFIXES:
                continue
            data = s3_client.get_object(s3_key)
            if data is None:
                logger.warning("doc_import_s3_miss", service_id=service_id, s3_key=s3_key)
                continue
            with tempfile.NamedTemporaryFile(
                suffix=Path(filename).suffix, delete=False
            ) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name).with_name(filename)
            # 원본 파일명 유지를 위해 임시 디렉토리 안에 rename — index 도 stem 기준이라 일관성 확보.
            Path(tmp.name).rename(tmp_path)
            try:
                imported = await _import_path(tmp_path, _service_id=service_id, document_id=doc.get("id"))
            finally:
                tmp_path.unlink(missing_ok=True)
            # 임베딩에 반영된 문서는 UI 의 '미반영' 태그를 해소하도록 표시.
            if imported:
                imported_from_saas += 1
                if doc.get("id"):
                    mark_reflected(doc["id"])

    # SaaS 흐름 — (1) 에서 1건 이상 import 했으면 (2) CLI 호환 분기 skip (#229).
    # 미설정 cfg.project.root → Path(".") fallback → CWD = qapilot 레포 → qapilot/docs/
    # 의 19개 본인 docs 가 Qdrant domain_knowledge 오염시키던 격차 차단.
    # _read_latest_prd_text (line 819-) 의 `if texts: return` 동형 패턴.
    if imported_from_saas > 0:
        logger.info(
            "doc_import_saas_complete",
            service_id=service_id, imported_count=imported_from_saas,
        )
        return {}

    # (2) config.project.root/docs/ — CLI 흐름 호환.
    config = load_config()
    proj = config.project
    repo_root = Path(proj.root or proj.repo_path or ".")
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        doc_files = _filter_latest_doc_versions([
            p for p in docs_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _SUPPORTED_DOC_SUFFIXES
        ])
        for doc_path in sorted(doc_files):
            await _import_path(doc_path)

    return {}


def _load_scan_result_from_disk(state: PipelineState) -> dict:
    """codebase-index/ 디스크 캐시에서 scan_result를 복원한다.

    natural_lang 트리거처럼 코드베이스 재스캔이 불필요한 경우에 사용한다.
    manifest.json → framework/language/endpoint_count,
    endpoints.json → file별 endpoints 재구성.
    캐시 미존재 시 scan_result=None으로 안전하게 반환한다.
    """
    try:
        manifest_path = _qapilot_path(state, "codebase-index", "manifest.json")
        endpoints_path = _qapilot_path(state, "codebase-index", "endpoints.json")
        if not manifest_path.exists():
            return {"scan_result": None, "current_layer": "L1A"}

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        files: list[dict] = []
        if endpoints_path.exists():
            endpoints: list[dict] = json.loads(endpoints_path.read_text(encoding="utf-8"))
            by_file: dict[str, list] = {}
            for ep in endpoints:
                by_file.setdefault(ep.get("file", ""), []).append(ep)
            files = [
                {"path": fpath, "endpoints": eps, "functions": [], "models": [], "dependencies": []}
                for fpath, eps in by_file.items()
            ]

        scan_result = {
            "files": files,
            "git_diff": None,
            "framework": manifest.get("framework") or "unknown",
            "language": manifest.get("language") or "unknown",
            "endpoint_count": int(manifest.get("endpoint_count") or 0),
        }
        return {"scan_result": scan_result, "current_layer": "L1A"}
    except Exception:
        return {"scan_result": None, "current_layer": "L1A"}


async def _upsert_scenario_index(service_id: str, ts: Any) -> None:
    """시나리오 저장 후 Qdrant scenario_index를 비동기로 업데이트한다. 실패는 silent."""
    try:
        from qapilot.tools.scenario_index import ScenarioVectorStore
        await ScenarioVectorStore().upsert_scenario(service_id=service_id, ts=ts)
    except Exception as e:
        get_logger(source="orchestrator").warning("scenario_index_upsert_error", error=str(e))


def _load_existing_scenarios_full(state: PipelineState) -> list[dict]:
    """기존 시나리오 전체(JSON)를 디스크 우선, 없으면 DB 폴백으로 로드한다.

    test_cases[].req_id 까지 포함한 원본 그대로 반환 — doc_update 증분 재생성의
    requirement↔scenario 매핑(이슈 #261)과 _build_existing_scenarios_summary 의
    공통 로딩 경로로 쓰인다. SaaS 서비스처럼 디스크 미사용 환경에서는 DB 폴백.
    """
    try:
        scenarios = _load_json_files(_qapilot_path(state, "scenarios"))
        if scenarios:
            return scenarios
    except Exception:
        pass

    try:
        from qapilot.db.scenario_reader import load_latest_scenarios
        trace = load_trace(state["trace_id"]) or {}
        service_id = trace.get("service_id")
        if service_id:
            db_scenarios = load_latest_scenarios(service_id)
            if db_scenarios:
                get_logger(source="orchestrator").debug(
                    "existing_scenarios_from_db", count=len(db_scenarios)
                )
                return db_scenarios
    except Exception:
        pass

    return []


def _build_existing_scenarios_summary(state: PipelineState) -> list[dict]:
    """기존 시나리오를 TS+TC 요약으로 반환한다.

    NaturalLanguageAgent가 target_ts_id / target_tc_id를 정확히 특정할 수 있도록
    ts_id, title, test_cases(tc_id + title)만 추출해 전달한다.
    전체 시나리오 JSON을 넘기면 프롬프트가 비대해지므로 요약본만 사용한다.
    """
    def _to_summary(scenarios: list[dict]) -> list[dict]:
        result = []
        for ts in scenarios:
            tc_summaries = [
                {"tc_id": tc.get("tc_id"), "title": tc.get("title") or tc.get("name") or ""}
                for tc in (ts.get("test_cases") or [])
                if tc.get("tc_id")
            ]
            result.append({
                "ts_id": ts.get("ts_id"),
                "title": ts.get("title") or ts.get("name") or "",
                "test_cases": tc_summaries,
            })
        return result

    return _to_summary(_load_existing_scenarios_full(state))


def _coerce_explicit_scenario_tc_add(
    user_input: str,
    requirements: list[dict],
    existing_scenarios: list[dict],
) -> None:
    """명시적 "XXX 시나리오에 YYY 케이스 추가" 요청을 create+tc로 보정한다.

    LLM이 이런 문장을 create+ts로 오분류하면 ScenarioGeneratorAgent가 새 TS를 만든다.
    사용자가 기존 TS명을 직접 쓴 경우에는 임베딩 유사도보다 명시적 지시를 우선한다.
    """
    normalized_input = re.sub(r"\s+", "", user_input)
    has_add_intent = bool(re.search(r"추가|생성|만들", user_input, flags=re.IGNORECASE))
    has_case_intent = bool(re.search(r"테스트\s*케이스|케이스|TC", user_input, flags=re.IGNORECASE))
    if not (has_add_intent and has_case_intent):
        return

    for ts in existing_scenarios:
        ts_id = ts.get("ts_id")
        title = str(ts.get("title") or ts.get("name") or "").strip()
        if not ts_id or not title:
            continue
        normalized_title = re.sub(r"\s+", "", title)
        if normalized_title and normalized_title in normalized_input:
            for req in requirements:
                req["action_type"] = "create"
                req["target_level"] = "tc"
                req["target_ts_id"] = ts_id
                req["target_tc_id"] = None
            return


def _coerce_explicit_tc_delete(
    user_input: str,
    requirements: list[dict],
    existing_scenarios: list[dict],
) -> None:
    """명시적 TC 삭제 요청을 delete+tc로 보정하고 가능한 좌표를 직접 주입한다."""
    if not re.search(r"삭제|제거|지워|없애|delete", user_input, flags=re.IGNORECASE):
        return

    direct_tc = re.search(r"\b(TS-\d{3}-TC-\d{2})\b", user_input, flags=re.IGNORECASE)
    if direct_tc:
        tc_id = direct_tc.group(1).upper()
        ts_id = tc_id.split("-TC-")[0]
        for req in requirements:
            req["action_type"] = "delete"
            req["target_level"] = "tc"
            req["target_ts_id"] = ts_id
            req["target_tc_id"] = tc_id
        return

    is_tc_delete = bool(re.search(r"테스트\s*케이스|TC|tc|케이스", user_input))
    if not is_tc_delete:
        return

    normalized_input = re.sub(r"\s+", "", user_input)
    matched_ts_id: str | None = None
    for ts in existing_scenarios:
        ts_id = ts.get("ts_id")
        title = str(ts.get("title") or ts.get("name") or "").strip()
        normalized_title = re.sub(r"\s+", "", title)
        if ts_id and normalized_title and normalized_title in normalized_input:
            matched_ts_id = str(ts_id)
            break

    for req in requirements:
        req["action_type"] = "delete"
        req["target_level"] = "tc"
        if matched_ts_id:
            req["target_ts_id"] = matched_ts_id
        req["target_tc_id"] = req.get("target_tc_id") or None


async def _rough_match_scenarios(
    user_input: str,
    existing_scenarios: list[dict],
    top_n: int = 5,
    threshold: float = 0.40,
    service_id: str | None = None,
) -> list[dict]:
    """user_input을 임베딩하여 기존 시나리오 중 top-N 후보를 반환한다.

    service_id가 제공되면 Qdrant scenario_index에서 검색(사전 임베딩 활용).
    Qdrant 미가동·장애 시 인메모리 코사인 유사도로 폴백한다.

    NaturalLanguageAgent 호출 전에 실행되며, LLM에게 전체 목록 대신
    유사도 높은 후보만 전달하여 프롬프트 크기를 제한한다.
    """
    import asyncio as _asyncio

    import numpy as np

    from qapilot.tools.domain_knowledge._embedder import get_embedder

    if not existing_scenarios or not user_input.strip():
        return []

    embedder = get_embedder()
    query_vecs = await _asyncio.to_thread(embedder.encode, [user_input], normalize_embeddings=True)
    query_vec = query_vecs[0]

    # ── Qdrant 검색 (service_id 있을 때) ─────────────────────────────────
    if service_id:
        try:
            from qapilot.tools.scenario_index import ScenarioVectorStore

            store = ScenarioVectorStore()
            hits = await store.search_ts(
                service_id=str(service_id),
                query_vec=query_vec.tolist(),
                top_n=top_n,
                threshold=threshold,
            )
            if hits:
                # Qdrant 결과를 existing_scenarios 원본 dict과 merge
                ts_by_id = {ts.get("ts_id"): ts for ts in existing_scenarios}
                return [
                    {**ts_by_id.get(h["ts_id"], {"ts_id": h["ts_id"], "title": h["title"]}),
                     "_similarity": h["_similarity"]}
                    for h in hits
                    if h["ts_id"] in ts_by_id or True
                ]
        except Exception as e:
            get_logger(source="orchestrator").debug("scenario_index_fallback", error=str(e))

    # ── 인메모리 폴백 ──────────────────────────────────────────────────────
    ts_texts = [ts.get("title") or ts.get("ts_id", "") for ts in existing_scenarios]
    ts_vecs = await _asyncio.to_thread(embedder.encode, ts_texts, normalize_embeddings=True)
    if ts_vecs is None or len(ts_vecs) == 0:
        return []

    sims = [float(np.dot(query_vec, ts_vec)) for ts_vec in ts_vecs]
    candidates = [
        {**ts, "_similarity": round(sims[i], 4)}
        for i, ts in enumerate(existing_scenarios)
        if sims[i] >= threshold
    ]
    candidates.sort(key=lambda x: x["_similarity"], reverse=True)
    return candidates[:top_n]


async def _resolve_scenario_targets(
    requirements: list[dict],
    existing_scenarios: list[dict],
    threshold: float = 0.50,
    service_id: str | None = None,
) -> list[dict]:
    """action_type: update인 요구사항에 임베딩 매칭으로 target_ts_id / target_tc_id를 주입한다.

    LLM이 아닌 코드 레벨에서 대상을 탐색하므로 존재하지 않는 ID를 반환하지 않는다.

    service_id가 제공되면 Qdrant scenario_index 사용 (사전 임베딩).
    Qdrant 미가동·장애 시 인메모리 코사인 유사도로 폴백한다.

    흐름:
    1. update 요구사항의 domain_area + content를 쿼리 텍스트로 임베딩
    2. Qdrant(또는 인메모리)로 TS 매칭 → target_ts_id 주입
    3. target_level이 tc/tv이면 TS 내 TC 매칭 → target_tc_id 주입
    4. threshold 미달 시 null 유지 (ScenarioGeneratorAgent가 create처럼 처리)
    """
    import asyncio as _asyncio

    import numpy as np

    from qapilot.tools.domain_knowledge._embedder import get_embedder

    # update/delete + create(tc/tv) 모두 처리: create+ts는 target_ts_id 불필요
    update_reqs = [
        r for r in requirements
        if r.get("action_type") in ("update", "delete")
        or (r.get("action_type") == "create" and r.get("target_level") in ("tc", "tv"))
    ]
    if not update_reqs or not existing_scenarios:
        return requirements

    embedder = get_embedder()

    # 쿼리 텍스트 임베딩 (요청당 1회 — TS/TC는 Qdrant에 사전 임베딩)
    query_texts = [
        f"{r.get('domain_area', '')} {r.get('content', '')}"
        for r in update_reqs
    ]
    query_vecs = await _asyncio.to_thread(
        embedder.encode, query_texts, normalize_embeddings=True
    )

    # TS 인메모리 폴백용 벡터 (Qdrant 실패 시)
    ts_vecs_cache: list | None = None
    ts_texts = [ts.get("title") or ts.get("ts_id", "") for ts in existing_scenarios]

    async def _get_ts_vecs():
        nonlocal ts_vecs_cache
        if ts_vecs_cache is None:
            res = await _asyncio.to_thread(
                embedder.encode, ts_texts, normalize_embeddings=True
            )
            # Ensure we never return None to callers that iterate over ts_vecs
            ts_vecs_cache = res if res is not None else []
        return ts_vecs_cache

    for i, req in enumerate(update_reqs):
        q_vec = query_vecs[i]

        # ── TS 매칭: Qdrant 우선 ──────────────────────────────────────────
        matched_ts_id: str | None = None
        matched_ts: dict | None = None

        action_type = req.get("action_type", "create")
        target_level = req.get("target_level", "ts")
        preset_ts_id = req.get("target_ts_id")

        if preset_ts_id:
            ts_by_id = {ts.get("ts_id"): ts for ts in existing_scenarios}
            matched_ts_id = str(preset_ts_id)
            matched_ts = ts_by_id.get(matched_ts_id)

        # create + ts: 완전 새 TS 생성이므로 target_ts_id 스킵 (기존 TS 교체 방지)
        if action_type == "create" and target_level == "ts":
            continue

        if matched_ts is None and service_id:
            try:
                from qapilot.tools.scenario_index import ScenarioVectorStore

                hits = await ScenarioVectorStore().search_ts(
                    service_id=str(service_id),
                    query_vec=q_vec.tolist(),
                    top_n=1,
                    threshold=threshold,
                )
                if hits:
                    matched_ts_id = hits[0]["ts_id"]
                    ts_by_id = {ts.get("ts_id"): ts for ts in existing_scenarios}
                    matched_ts = ts_by_id.get(matched_ts_id)
            except Exception as e:
                get_logger(source="orchestrator").debug(
                    "resolve_targets_qdrant_ts_fallback", error=str(e)
                )

        if matched_ts is None:
            # 인메모리 폴백
            ts_vecs = await _get_ts_vecs()
            # _get_ts_vecs may return None or an empty value; guard before iteration.
            if ts_vecs is None or len(ts_vecs) == 0:
                continue
            ts_sims = [float(np.dot(q_vec, tv)) for tv in ts_vecs]
            best_ts_idx = int(np.argmax(ts_sims))
            if ts_sims[best_ts_idx] < threshold:
                continue
            matched_ts = existing_scenarios[best_ts_idx]
            matched_ts_id = matched_ts.get("ts_id")

        if not matched_ts_id or not matched_ts:
            continue

        req["target_ts_id"] = matched_ts_id

        if target_level in ("tc", "tv"):
            tc_matched: dict | None = None

            if target_level == "tv" and service_id:
                try:
                    from qapilot.tools.scenario_index import ScenarioVectorStore

                    tc_hits = await ScenarioVectorStore().search_tc(
                        service_id=str(service_id),
                        ts_id=matched_ts_id,
                        query_vec=q_vec.tolist(),
                        threshold=threshold,
                    )
                    if tc_hits:
                        tc_matched = tc_hits[0]
                except Exception as e:
                    get_logger(source="orchestrator").debug(
                        "resolve_targets_qdrant_tc_fallback", error=str(e)
                    )

            if target_level == "tv" and tc_matched:
                req["target_tc_id"] = tc_matched.get("tc_id")
                continue

            # TC 레벨은 target_tc_id 설정 없이 유사 TC만 감지한다.
            # TV 레벨은 어느 TC에 추가할지 알아야 하므로 인메모리 fallback 매칭을 유지한다.
            tcs = matched_ts.get("test_cases") or []
            if not tcs:
                continue
            tc_texts = [
                tc.get("name") or tc.get("title") or tc.get("tc_id", "")
                for tc in tcs
            ]
            tc_vecs = await _asyncio.to_thread(
                embedder.encode, tc_texts, normalize_embeddings=True
            )
            tc_sims = [float(np.dot(q_vec, tc_vec)) for tc_vec in tc_vecs]
            best_tc_idx = int(np.argmax(tc_sims))
            get_logger(source="orchestrator").debug(
                "tc_similarity_check",
                query=query_texts[i][:60],
                best_tc=tc_texts[best_tc_idx][:60],
                similarity=round(tc_sims[best_tc_idx], 3),
                threshold=0.75,
                target_level=target_level,
            )

            if target_level == "tv" and tc_sims[best_tc_idx] >= threshold:
                req["target_tc_id"] = tcs[best_tc_idx].get("tc_id")
            elif target_level == "tc" and tc_sims[best_tc_idx] >= 0.75:
                if action_type in ("update", "delete"):
                    # update/delete 경로: 대상 TC를 확정해 ScenarioGeneratorAgent에 전달
                    req["target_tc_id"] = tcs[best_tc_idx].get("tc_id")
                else:
                    # create 경로: 유사 TC 발견 → 사용자 확인 요청 (새로 추가 / 수정 / 삭제 선택)
                    similar_tc = tcs[best_tc_idx]
                    req["_similar_tc"] = {
                        "tc_id": similar_tc.get("tc_id"),
                        "name": similar_tc.get("name") or similar_tc.get("title", ""),
                        "ts_id": matched_ts.get("ts_id"),
                        "similarity": round(tc_sims[best_tc_idx], 3),
                    }

    return requirements


async def _codebase_scan(state: PipelineState) -> dict:
    """FR-000 코드베이스 스캔 + spec §6.1 디스크 캐시.

    기본 정책:
    - Git 입력 (`repo_url` 또는 `repos`) 이 있으면 **항상 GitCodebaseScannerTool 우선**
    - Git 스캔이 실패하면 warning 로그를 남기고 CodebaseScannerTool 로컬 fallback
    - Git 입력이 아예 없을 때만 로컬 스캔을 직접 사용
    """
    from qapilot.shared.schemas import ToolInput

    trace_id = state.get("trace_id") or str(_uuid.uuid4())
    run_options = state["run_options"]
    logger = get_logger("orchestrator")

    is_git_mode = bool(run_options.get("repo_url") or run_options.get("repos"))

    trigger: str = run_options.get("trigger") or "init"

    # natural_lang: 코드 변경 없음 → 디스크 캐시 복원, 스캔 skip (이슈 #180)
    if trigger == "natural_lang":
        return _load_scan_result_from_disk(state)

    # doc_update: 코드 스캔 불필요 → 안전하게 None 반환 (이슈 #180, NoneType 크래시 수정)
    if trigger == "doc_update":
        return {"scan_result": None, "trace_id": trace_id, "current_layer": "L1A"}

    params: dict[str, Any] = {"trigger": trigger}
    for key in ("repo_url", "token", "branch", "local_path", "repos"):
        val = run_options.get(key)
        if val is not None:
            params[key] = val

    # code_change: 직전 스캔의 commit_hash를 읽어 증분 스캔 활성화 (이슈 #180)
    if trigger == "code_change":
        manifest_path = _qapilot_path(state, "codebase-index", "manifest.json")
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                last_hash = manifest.get("commit_hash", "")
                if last_hash:
                    params["last_commit_hash"] = last_hash
            except Exception:
                pass

    result = None
    if is_git_mode:
        from qapilot.tools.git_codebase_scanner_tool import GitCodebaseScannerTool

        git_tool = GitCodebaseScannerTool(trace_id=trace_id)
        try:
            logger.info(
                "codebase_scan_git_attempt",
                trace_id=trace_id,
                repo_url=run_options.get("repo_url"),
                repo_count=len(run_options.get("repos") or []),
            )
            result = await git_tool.run(
                ToolInput(trace_id=trace_id, params=params)
            )
            logger.info("codebase_scan_git_succeeded", trace_id=trace_id)
        except Exception as e:
            logger.warning(
                "codebase_scan_git_failed_fallback_local",
                trace_id=trace_id,
                error=f"{type(e).__name__}: {e}",
                repo_url=run_options.get("repo_url"),
                repo_count=len(run_options.get("repos") or []),
            )

    if result is None:
        # 이슈 #156: 로컬 모드 임시 fallback — Git 입력 부재 또는 Git 스캔 실패 시 사용
        from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool

        local_tool = CodebaseScannerTool(trace_id=trace_id)
        local_params = {"trigger": trigger}
        if params.get("last_commit_hash"):
            local_params["last_commit_hash"] = params["last_commit_hash"]
        if params.get("local_path") is not None:
            local_params["local_path"] = params["local_path"]

        logger.info(
            "codebase_scan_local_attempt",
            trace_id=trace_id,
            reason="git_failed" if is_git_mode else "git_input_missing",
        )
        result = await local_tool.run(
            ToolInput(trace_id=trace_id, params=local_params)
        )
        logger.info("codebase_scan_local_succeeded", trace_id=trace_id)

    scan: dict[str, Any] = result.result["scan_result"]

    # L2: spec §6.1 정합 디스크 캐시 (Tool 본체 무수정)
    _save_codebase_index_to_disk(scan, state)

    # SaaS 흐름 — Git mode 의 임시 dir dump path 자동 주입.
    # GitCodebaseScannerTool 이 REST API 로 받은 file content 를 임시 dir 에 reconstruct
    # 한 결과. 사용자가 등록 시 GitHub repo URL 만 입력하면 8001 내부에서 본인 데이터
    # layer 의 scan_all_metadata 가 동작할 수 있는 본질적 path. state.target_root 우선순위
    # 는 cfg → state.target_root → state.qapilot_dir derive 순 (CLI 우선, SaaS 자동 주입).
    git_temp_repo_root = (result.result.get("_metadata") or {}).get("temp_repo_root")

    # metadata-index (PoC 10): 4영역 AST 추출 → S3/DB
    if scan:
        # Git mode 의 임시 dir 이 있으면 그것 우선 사용 (SaaS 본질),
        # 없으면 기존 _resolve_project_root (CLI cfg / state.target_root / qapilot_dir derive)
        if git_temp_repo_root:
            _project_root = Path(git_temp_repo_root)
            logger.info(
                "metadata_index_using_git_temp_repo_root",
                path=str(_project_root),
            )
        else:
            _project_root = _resolve_project_root(state)

        if _project_root is not None:
            _trace = load_trace(state["trace_id"]) or {}
            _service_id = _trace.get("service_id")
            _commit_sha = (scan.get("git_diff") or {}).get("commit_hash") or ""
            if _service_id and _commit_sha:
                from qapilot.scan.orchestrator import scan_all_metadata
                try:
                    await scan_all_metadata(
                        _service_id, _project_root, _commit_sha,
                        skip_llm_classification=True,
                    )
                    logger.info(
                        "metadata_index_done",
                        service_id=_service_id,
                        commit_sha=_commit_sha[:12],
                    )
                except Exception as _e:
                    logger.warning("metadata_index_failed", error=str(_e))

    return_dict: dict[str, Any] = {
        "trace_id": trace_id,
        "scan_result": scan,
        "current_layer": "L1A",
    }
    # 후속 노드도 git temp dir 활용 가능하도록 state.target_root 갱신.
    # CLI cfg / 사용자 명시 target_root 우선순위는 _resolve_project_root 가 관리.
    if git_temp_repo_root:
        return_dict["target_root"] = git_temp_repo_root
    return return_dict


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


def _read_latest_prd_text(state: PipelineState | None = None) -> str:
    """최신 PRD 텍스트를 반환한다. 3단 우선순위:

      1) UI 업로드 (domain_documents + S3) — service_id 기반.
      2) config.project.root/docs/ — CLI 흐름 호환.

    어느 단계든 텍스트가 잡히면 그 단계만 반환 (낮은 단계로 fallback 안 함) —
    UI 가 업로드한 PRD 와 무관한 CLI docs 가 섞이는 leak 방지.
    """
    from qapilot.db.domain_reader import list_latest_domain_documents
    from qapilot.shared.config import load_config
    from qapilot.storage import s3_client

    # (1) UI 업로드 (DB+S3) — service_id 가 있을 때만.
    if state is not None:
        trace = load_trace(state["trace_id"]) or {}
        service_id = trace.get("service_id")
        if service_id:
            texts: list[str] = []
            for doc in list_latest_domain_documents(service_id):
                filename = doc.get("filename") or ""
                s3_key = doc.get("s3_key") or ""
                if not (filename and s3_key):
                    continue
                if Path(filename).suffix.lower() not in _SUPPORTED_DOC_SUFFIXES:
                    continue
                if "prd" not in filename.lower():
                    continue
                data = s3_client.get_object(s3_key)
                if data is None:
                    continue
                try:
                    texts.append(data.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
            if texts:
                return "\n\n".join(texts)

    # (2) config.project.root/docs/ — CLI 흐름.
    config = load_config()
    proj = config.project
    repo_root = Path(proj.root or proj.repo_path or ".")
    docs_dir = repo_root / "docs"
    if not docs_dir.exists():
        return ""
    all_docs = [
        p for p in docs_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _SUPPORTED_DOC_SUFFIXES
    ]
    prd_docs = [
        p for p in _filter_latest_doc_versions(all_docs)
        if "prd" in p.name.lower()
    ]
    texts = []
    for p in prd_docs:
        try:
            texts.append(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return "\n\n".join(texts)


async def _classify_requirements_by_embedding(
    new_requirements: list[dict],
    existing_scenarios: list[dict],
    service_id: str | None = None,
    update_threshold: float = 0.50,
    unchanged_threshold: float = 0.85,
) -> tuple[list[dict], list[str]]:
    """임베딩 유사도로 요구사항을 unchanged/updated/created로 분류한다.

    req_id 기반 분류(`_classify_doc_update_requirements`)를 대체하며 PRD 외
    문서 업로드에도 동작한다.

    분류 기준:
      - 유사도 >= unchanged_threshold : unchanged  → ScenarioGeneratorAgent 전달 안 함
      - update_threshold <= 유사도 < unchanged_threshold : updated  → delta 수정
      - 유사도 < update_threshold : created  → 신규 생성

    Returns:
        (분류 태그가 주입된 requirements, 고아 ts_id 목록)
    """
    import asyncio as _asyncio

    import numpy as np

    from qapilot.tools.domain_knowledge._embedder import get_embedder

    if not existing_scenarios:
        for req in new_requirements:
            req.setdefault("_diff_status", "created")
            req.setdefault("action_type", "create")
        return new_requirements, []

    embedder = get_embedder()

    query_texts = [
        f"{r.get('domain_area', '')} {r.get('content', '')}"
        for r in new_requirements
    ]
    query_vecs = await _asyncio.to_thread(
        embedder.encode, query_texts, normalize_embeddings=True
    )

    ts_texts = [ts.get("title") or ts.get("name") or ts.get("ts_id", "") for ts in existing_scenarios]

    # Qdrant 우선, 실패 시 인메모리 폴백
    use_qdrant = bool(service_id)
    ts_vecs_cache: list | None = None

    async def _get_ts_vecs():
        nonlocal ts_vecs_cache
        if ts_vecs_cache is None:
            ts_vecs_cache = await _asyncio.to_thread(
                embedder.encode, ts_texts, normalize_embeddings=True
            )
        return ts_vecs_cache

    covered_ts_ids: set[str] = set()

    for i, req in enumerate(new_requirements):
        q_vec = query_vecs[i]
        best_sim: float = 0.0
        best_ts: dict | None = None

        if use_qdrant:
            try:
                from qapilot.tools.scenario_index import ScenarioVectorStore

                hits = await ScenarioVectorStore().search_ts(
                    service_id=str(service_id),
                    query_vec=q_vec.tolist(),
                    top_n=1,
                    threshold=update_threshold,
                )
                if hits:
                    ts_by_id = {ts.get("ts_id"): ts for ts in existing_scenarios}
                    best_ts = ts_by_id.get(hits[0]["ts_id"])
                    best_sim = hits[0].get("_similarity", 0.0)
            except Exception:
                use_qdrant = False  # 이후 요건은 인메모리로 처리

        if best_ts is None:
            ts_vecs = await _get_ts_vecs()
            if not ts_vecs:
                # No TS vectors available -> treat as no match (created)
                req["_diff_status"] = "created"
                req.setdefault("action_type", "create")
                continue
            ts_sims = [float(np.dot(q_vec, tv)) for tv in ts_vecs]
            best_idx = int(np.argmax(ts_sims))
            best_sim = ts_sims[best_idx]
            if best_sim >= update_threshold:
                best_ts = existing_scenarios[best_idx]

        if best_ts is None or best_sim < update_threshold:
            req["_diff_status"] = "created"
            req.setdefault("action_type", "create")
        elif best_sim >= unchanged_threshold:
            req["_diff_status"] = "unchanged"
            covered_ts_ids.add(best_ts.get("ts_id", ""))
        else:
            req["_diff_status"] = "updated"
            req["action_type"] = "update"
            req["target_level"] = "ts"
            req["target_ts_id"] = best_ts.get("ts_id")
            covered_ts_ids.add(best_ts.get("ts_id", ""))

    # 어떤 요구사항과도 매칭되지 않은 기존 TS → 고아
    all_ts_ids = {ts.get("ts_id", "") for ts in existing_scenarios}
    orphaned_ts_ids = sorted(all_ts_ids - covered_ts_ids - {""})

    return new_requirements, orphaned_ts_ids


def _classify_doc_update_requirements(
    new_requirements: list[dict],
    prev_requirements: list[dict],
    existing_scenarios: list[dict],
) -> tuple[list[dict], list[str]]:
    """이전 PRD 요구사항과 비교해 각 요구사항을 unchanged/update/create로 분류한다 (이슈 #261).

    PRD의 `#### FR-XXX-NN` heading에서 부여되는 req_id는 문서가 바뀌어도 안정적으로
    유지되므로 (RequirementExtractorAgent._ensure_all_document_frs), 임베딩 매칭 없이
    req_id 동일성 + content 비교만으로 정확한 diff가 가능하다.

    분류 결과는 각 requirement dict에 in-place로 태깅한다:
      - unchanged : req_id/content 모두 동일 → _diff_status="unchanged" (재생성 skip)
      - update    : req_id 동일, content만 변경 → action_type="update" + target_ts_id
                    (TC.req_id 매핑으로 대상 TS 특정 — natural_lang의 임베딩 매칭과 달리
                    req_id가 정확한 키이므로 더 신뢰도 높은 매칭이 가능하다)
      - create    : 새 req_id, 또는 매핑 대상 TS를 못 찾은 update → action_type="create"

    Returns:
        (분류 태그가 주입된 requirements, 고아가 된 기존 시나리오의 ts_id 목록)
        고아 = 이전 PRD에는 있었으나 새 PRD에서 사라진 req_id가 매핑되어 있던 TS.
    """
    prev_by_id = {r.get("req_id"): r for r in prev_requirements if r.get("req_id")}

    # req_id → ts_id 매핑 — TC.req_id 기준 (RTM 매핑(_write_initial_rtm_version)과 동일 키)
    req_to_ts: dict[str, str] = {}
    for ts in existing_scenarios:
        ts_id = ts.get("ts_id")
        if not ts_id:
            continue
        for tc in ts.get("test_cases") or []:
            req_id = tc.get("req_id")
            if req_id and req_id not in req_to_ts:
                req_to_ts[req_id] = ts_id

    for req in new_requirements:
        req_id = req.get("req_id")
        prev = prev_by_id.get(req_id) if req_id else None
        if prev is None:
            req["_diff_status"] = "created"
            req["action_type"] = "create"
        elif (prev.get("content") or "") == (req.get("content") or ""):
            req["_diff_status"] = "unchanged"
        else:
            target_ts_id = req_to_ts.get(req_id) if req_id is not None else None
            if target_ts_id:
                req["_diff_status"] = "updated"
                req["action_type"] = "update"
                req["target_level"] = "ts"
                req["target_ts_id"] = target_ts_id
            else:
                # 내용은 바뀌었지만 매핑된 기존 시나리오가 없음 → 새로 생성 (안전한 폴백)
                req["_diff_status"] = "created"
                req["action_type"] = "create"

    new_ids = {r.get("req_id") for r in new_requirements if r.get("req_id")}
    removed_ids = set(prev_by_id) - new_ids
    orphaned_ts_ids = sorted({req_to_ts[rid] for rid in removed_ids if rid in req_to_ts})

    return new_requirements, orphaned_ts_ids


async def _requirement_extract(state: PipelineState) -> dict:
    """FR-024 trigger별 요구사항 추출.

    trigger에 따라 호출 Agent가 다르다 (이슈 #180):
    - natural_lang : NaturalLanguageAgent — 챗봇 자연어 쿼리 해석
    - init / doc_update : RequirementExtractorAgent — PRD 문서에서 요구사항 추출
    - code_change : 재추출 불필요 → 빈 requirements 반환
    """
    trigger = (state["run_options"].get("trigger") or "init")

    # ── natural_lang: 챗봇 쿼리 → NaturalLanguageAgent ──────────────────────────
    if trigger == "natural_lang":
        from qapilot.agents.natural_language_agent import NaturalLanguageAgent
        from qapilot.shared.schemas import AgentInput
        from qapilot.shared.session_store import (
            get_last_exchange,
            pop_pending_requirements,
            pop_pending_similar_tc,
            save_exchange,
            save_pending_requirements,
            save_pending_similar_tc,
        )

        user_input = (state["run_options"].get("user_input") or "").strip()
        session_id = state["run_options"].get("session_id") or ""
        qapilot_dir = state.get("qapilot_dir") or ""

        # ── 유사 TC 질문에 대한 "새로 추가" / "수정" / "삭제" 응답 처리 ──────────────────────
        # 직전 교환이 _similar_tc 감지로 인한 insufficient이고
        # 현재 입력이 "새로 추가" 계열이면 pending_requirements를 그대로 재사용한다.
        # "수정" 계열이면 pending_similar_tc에 저장된 tc 좌표를 꺼내 update 방향으로 주입한다.
        # "삭제" 계열이면 pending_similar_tc의 tc 좌표를 꺼내 delete 방향으로 주입한다.
        _NEW_ADD_KEYWORDS = {"새로 추가", "새로추가", "추가", "add", "yes", "네", "응", "그냥 추가"}
        _MODIFY_KEYWORDS = {"수정", "변경", "고쳐", "수정할게", "수정해줘", "modify", "update"}
        _DELETE_KEYWORDS = {"삭제", "삭제할래", "삭제해", "삭제해줘", "삭제할게", "지워", "지울래", "delete"}
        if session_id and qapilot_dir and user_input in _NEW_ADD_KEYWORDS:
            pending_reqs = pop_pending_requirements(qapilot_dir, session_id)
            pop_pending_similar_tc(qapilot_dir, session_id)  # 잔여 좌표 정리
            if pending_reqs:
                save_exchange(qapilot_dir, session_id, user_input, "sufficient", None)
                return {"requirements": pending_reqs}

        if session_id and qapilot_dir and user_input in _MODIFY_KEYWORDS:
            pending_reqs = pop_pending_requirements(qapilot_dir, session_id)
            pending_tc = pop_pending_similar_tc(qapilot_dir, session_id)
            if pending_reqs and pending_tc:
                for req in pending_reqs:
                    req["action_type"] = "update"
                    req["target_level"] = "tc"
                    req["target_ts_id"] = pending_tc.get("ts_id")
                    req["target_tc_id"] = pending_tc.get("tc_id")
                save_exchange(qapilot_dir, session_id, user_input, "sufficient", None)
                return {"requirements": pending_reqs}

        if session_id and qapilot_dir and user_input in _DELETE_KEYWORDS:
            pending_reqs = pop_pending_requirements(qapilot_dir, session_id)
            pending_tc = pop_pending_similar_tc(qapilot_dir, session_id)
            if pending_reqs and pending_tc:
                for req in pending_reqs:
                    req["action_type"] = "delete"
                    req["target_level"] = "tc"
                    req["target_ts_id"] = pending_tc.get("ts_id")
                    req["target_tc_id"] = pending_tc.get("tc_id")
                save_exchange(qapilot_dir, session_id, user_input, "sufficient", None)
                return {"requirements": pending_reqs}

        # 직전 교환을 context에 포함 (sufficient도 포함 — "도" 같은 연결 표현의 맥락 유지)
        # rejected 교환은 get_last_exchange에서 None 반환하므로 맥락 오염 없음
        last_exchange = (
            get_last_exchange(qapilot_dir, session_id)
            if session_id and qapilot_dir else None
        )

        # 전체 시나리오 목록 로드
        existing_scenarios_summary = _build_existing_scenarios_summary(state)

        # service_id: Qdrant 검색에 필요 (trace.json에서 추출)
        # `load_trace(trace_id: str) -> dict | None` — 1 arg signature.
        # 이전: load_trace(qapilot_dir, state["trace_id"]) — 2 args (signature mismatch)
        # → `defects_persist_failed error='load_trace() takes 1 positional argument but
        # 2 were given'` 직접 원인 (kshyun PR `6e37349` defect INSERT 와 동작 충돌).
        _trace_for_svc = load_trace(state["trace_id"]) or {}
        _service_id = _trace_for_svc.get("service_id") or None

        # 1단계: user_input 직접 임베딩 → top-N 후보 탐색 (이슈 #186, #221)
        # Qdrant scenario_index 우선 검색, 장애 시 인메모리 폴백
        try:
            top_candidates = await _rough_match_scenarios(
                user_input, existing_scenarios_summary, service_id=_service_id
            )
        except Exception as e:
            get_logger("orchestrator").warning("rough_match_failed", error=str(e))
            top_candidates = []

        # 2단계: NaturalLanguageAgent — top_candidates 기반으로 create/update 판단
        agent = NaturalLanguageAgent(trace_id=state["trace_id"])
        output = await agent.run(
            AgentInput(
                trace_id=state["trace_id"],
                context={
                    "scan_result": state.get("scan_result"),
                    "domain_rules": state.get("domain_rules") or [],
                    "conversation_history": [last_exchange] if last_exchange else [],
                    "top_candidates": top_candidates,
                },
                params={"trigger": "natural_lang", "user_input": user_input},
            )
        )

        query_status = output.result.get("query_status", "sufficient")
        query_feedback = output.result.get("query_feedback")

        # 세션에 이번 교환 저장
        if session_id and qapilot_dir:
            save_exchange(qapilot_dir, session_id, user_input, query_status, query_feedback)

        if query_status != "sufficient":
            return {
                "requirements": [],
                "query_status": query_status,
                "query_feedback": query_feedback,
            }

        requirements = output.result.get("requirements", []) or []

        # LLM 오분류 교정: "XXX 시나리오 삭제"처럼 TS/TC 관리 동작인데 create/update로 분류된 경우를 보정.
        import re as _re
        _input_is_tc_delete = bool(_re.search(
            r"테스트\s*케이스\s+삭제|TC\s+삭제|tc\s+삭제", user_input.strip()
        ))
        _input_is_ts_delete = bool(_re.search(
            r"시나리오\s+삭제\s*$|삭제\s*해\s*줘\s*$|삭제\s*해\s*주세요\s*$", user_input.strip()
        ))
        for req in requirements:
            if req.get("action_type") in ("create", "update"):
                domain = req.get("domain_area", "")
                domain_is_delete = bool(_re.search(r"(시나리오\s*)?삭제\s*$", domain))
                if _input_is_tc_delete:
                    req["action_type"] = "delete"
                    req["target_level"] = "tc"
                    req["domain_area"] = _re.sub(r"\s*(테스트\s*케이스\s*)?삭제\s*$", "", domain).strip() or domain
                elif domain_is_delete or _input_is_ts_delete:
                    req["action_type"] = "delete"
                    req["target_level"] = "ts"
                    req["domain_area"] = _re.sub(r"\s*(시나리오\s*)?삭제\s*$", "", domain).strip() or domain

        _coerce_explicit_scenario_tc_add(user_input, requirements, existing_scenarios_summary)
        _coerce_explicit_tc_delete(user_input, requirements, existing_scenarios_summary)

        resolve_candidates = list(top_candidates)
        known_candidate_ids = {c.get("ts_id") for c in resolve_candidates}
        explicit_ts_ids = {
            r.get("target_ts_id")
            for r in requirements
            if r.get("target_ts_id")
        }
        for ts in existing_scenarios_summary:
            if ts.get("ts_id") in explicit_ts_ids and ts.get("ts_id") not in known_candidate_ids:
                resolve_candidates.append(ts)
                known_candidate_ids.add(ts.get("ts_id"))

        # 3단계: top_candidates 안에서 target_ts_id / target_tc_id 확정 주입 (이슈 #186, #221)
        try:
            requirements = await _resolve_scenario_targets(
                requirements, resolve_candidates, service_id=_service_id
            )
        except Exception as e:
            get_logger("orchestrator").warning("scenario_target_resolve_failed", error=str(e))

        # 3.5단계: delete 요청인데 target_ts_id 미확정(TS 삭제) 또는 target_tc_id 미확정(TC 삭제) → insufficient
        unresolved_deletes = [
            r for r in requirements
            if r.get("action_type") == "delete" and (
                not r.get("target_ts_id")
                or (r.get("target_level") == "tc" and not r.get("target_tc_id"))
            )
        ]
        if unresolved_deletes:
            domain_areas = ", ".join(
                f"'{r.get('domain_area', '')}'" for r in unresolved_deletes if r.get("domain_area")
            ) or "해당 대상"
            has_tc_delete = any(r.get("target_level") == "tc" for r in unresolved_deletes)
            if has_tc_delete:
                feedback = f"삭제할 테스트 케이스를 찾지 못했습니다: {domain_areas}. TC 이름이나 내용을 더 구체적으로 입력해 주세요."
            else:
                feedback = f"삭제할 시나리오를 찾지 못했습니다: {domain_areas}. 시나리오 이름을 더 구체적으로 입력해 주세요."
            if session_id and qapilot_dir:
                save_exchange(qapilot_dir, session_id, user_input, "insufficient", feedback)
            return {
                "requirements": [],
                "query_status": "insufficient",
                "query_feedback": feedback,
            }

        # 4단계: 유사 TC 감지 → insufficient로 사용자 확인 요청
        for req in requirements:
            similar_tc = req.pop("_similar_tc", None)
            if similar_tc:
                tc_id = similar_tc.get("tc_id", "")
                tc_name = similar_tc.get("name", "")
                # similar_tc에 ts_id가 있으면 그쪽 사용, 없으면 req fallback
                ts_id = similar_tc.get("ts_id") or req.get("target_ts_id", "")
                tc_label = f"{tc_id} {tc_name}".strip() if tc_id else tc_name
                feedback = (
                    f"유사한 TC '{tc_label}'가 {ts_id}에 이미 있습니다. "
                    f"이 TC를 수정할까요, 새로 추가할까요, 아니면 삭제할까요?"
                )
                if session_id and qapilot_dir:
                    # 원본 요건을 저장 — 다음 턴 "새로 추가"/"수정" 선택 시 재사용
                    save_pending_requirements(qapilot_dir, session_id, requirements)
                    # tc 좌표 별도 보존 — "수정" 응답 시 target_tc_id 확정에 사용
                    save_pending_similar_tc(qapilot_dir, session_id, {
                        "tc_id": similar_tc.get("tc_id"),
                        "ts_id": similar_tc.get("ts_id") or req.get("target_ts_id", ""),
                    })
                    save_exchange(qapilot_dir, session_id, user_input, "insufficient", feedback)
                return {
                    "requirements": [],
                    "query_status": "insufficient",
                    "query_feedback": feedback,
                }

        return {"requirements": requirements}

    # ── code_change: 요구사항 재추출 불필요 ──────────────────────────────────────
    if trigger == "code_change":
        return {"requirements": []}

    # ── init / doc_update: PRD 문서 → RequirementExtractorAgent ─────────────────
    from qapilot.agents.requirement_extractor import RequirementExtractorAgent
    from qapilot.shared.schemas import AgentInput

    user_input = (state["run_options"].get("user_input") or "").strip()
    document_text = user_input or _read_latest_prd_text(state)

    if not document_text:
        return {"requirements": []}

    # PRD 텍스트 자체가 안 바뀌었으면 재추출 스킵 — 같은 입력으로 LLM 을 또 호출해
    # (사실상) 같은 결과를 만들고 시나리오까지 불필요하게 재생성하는 낭비를 막는다.
    # user_input(수동 텍스트)이 있는 경우는 PRD 캐시 대상이 아니므로 제외.
    cache_path = _qapilot_path(state, "domain", "_prd_requirements_cache.json")
    text_hash: str | None = None
    prev_cached: dict | None = None
    if not user_input:
        import hashlib

        text_hash = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
        if trigger == "doc_update":
            try:
                raw = cache_path.read_text(encoding="utf-8")
                prev_cached = json.loads(raw) if raw else None
                if isinstance(prev_cached, dict) and prev_cached.get("hash") == text_hash and prev_cached.get("requirements"):
                    get_logger("orchestrator").info("requirement_extract_skip_unchanged_prd")
                    return {"requirements": prev_cached.get("requirements", [])}
            except Exception:
                prev_cached = None

    agent = RequirementExtractorAgent(trace_id=state["trace_id"])
    output = await agent.run(
        AgentInput(
            trace_id=state["trace_id"],
            context={"domain_rules": state.get("domain_rules") or []},
            params={"document_text": document_text, "existing_count": 0},
        )
    )
    # agent_logs append — runner.run_pipeline 의 total_cost 집계가 누락되던 격차 (#227).
    # action_mapper / code_generator 와 동형 패턴 (line 1292 / 1337).
    agent_logs = state.get("agent_logs", []) + [output.metadata.model_dump()]
    requirements = output.result.get("requirements", []) or []

    # 캐시는 분류 태깅(_diff_status/action_type/...) 이전의 순수 추출 결과로 저장한다 —
    # 다음 실행에서 diff 기준선(prev_requirements)으로 그대로 재사용하기 위함.
    if text_hash is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"hash": text_hash, "requirements": requirements}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    result: dict = {"requirements": requirements, "agent_logs": agent_logs}

    # ── doc_update 증분 재생성: 임베딩 유사도 기반 분류 ──────────────────────────
    # req_id 기반 분류(_classify_doc_update_requirements)를 대체한다.
    # PRD 문서뿐 아니라 API 명세·도메인 규칙 문서 등 비PRD 업로드에도 동작하며,
    # 기존 시나리오가 없는 최초 생성(existing_scenarios 빈 목록) 시에도 안전하게 처리된다.
    if trigger == "doc_update":
        try:
            existing_scenarios = _load_existing_scenarios_full(state)
            _trace_doc = load_trace(state["trace_id"]) or {}
            _service_id_doc = _trace_doc.get("service_id") or None
            requirements, orphaned_ts_ids = await _classify_requirements_by_embedding(
                requirements, existing_scenarios, service_id=_service_id_doc
            )
            result["requirements"] = requirements
            if orphaned_ts_ids:
                result["orphaned_ts_ids"] = orphaned_ts_ids
            get_logger("orchestrator").info(
                "doc_update_requirements_classified",
                created=sum(1 for r in requirements if r.get("_diff_status") == "created"),
                updated=sum(1 for r in requirements if r.get("_diff_status") == "updated"),
                unchanged=sum(1 for r in requirements if r.get("_diff_status") == "unchanged"),
                orphaned_ts_count=len(orphaned_ts_ids),
            )
        except Exception as e:
            get_logger("orchestrator").warning("doc_update_requirement_diff_failed", error=str(e))

    return result


async def _scenario_generate(state: PipelineState) -> dict:
    """FR-002 ScenarioGeneratorAgent 호출 → TS/TC/TV 시나리오 목록."""
    from qapilot.agents.scenario_generator.agent import ScenarioGeneratorAgent
    from qapilot.shared.schemas import AgentInput

    # insufficient/rejected 상태: 사용자 확인 대기 중 → 생성 불필요
    query_status = state.get("query_status")
    if query_status and query_status != "sufficient":
        return {"scenarios": []}

    trigger = state["run_options"].get("trigger") or "code_change"
    affected_only = trigger == "code_change"

    # natural_lang에서 requirements가 비어있으면 ScenarioGeneratorAgent 호출 불필요
    # (router-based fallback을 막기 위해 명시적으로 early return)
    if trigger == "natural_lang" and not (state.get("requirements") or []):
        return {"scenarios": []}

    requirements_for_agent = state.get("requirements") or []
    if trigger == "doc_update":
        # 증분 재생성(이슈 #261): _requirement_extract가 _diff_status="unchanged"로
        # 분류한 요구사항은 ScenarioGeneratorAgent에 전달하지 않는다 — 매핑된 기존
        # 시나리오를 그대로 보존하기 위함. (state.requirements 자체는 RTM 전체 매핑
        # 생성을 위해 원본 그대로 유지 — _write_initial_rtm_version에서 사용)
        filtered = [r for r in requirements_for_agent if r.get("_diff_status") != "unchanged"]
        if len(filtered) != len(requirements_for_agent):
            get_logger("orchestrator").info(
                "doc_update_unchanged_requirements_skipped",
                skipped=len(requirements_for_agent) - len(filtered),
                remaining=len(filtered),
            )
        requirements_for_agent = filtered
        # 변경/신규 요구사항이 하나도 없으면 ScenarioGeneratorAgent를 호출하지 않는다 —
        # requirements가 빈 리스트로 전달되면 _execute가 router 기반 fallback(전체 재생성)
        # 으로 빠지므로, 명시적으로 early return해 기존 시나리오를 그대로 보존한다.
        if not requirements_for_agent:
            get_logger("orchestrator").info("doc_update_no_changed_requirements_skip_generation")
            return {"scenarios": []}

    context: dict = {
        "scan_result": state.get("scan_result"),
        "domain_rules": state.get("domain_rules") or [],
        "requirements": requirements_for_agent,
        "service_id": (load_trace(state["trace_id"]) or {}).get("service_id"),
        # codebase-index 디렉토리를 state.qapilot_dir 기준으로 read 하도록 전달.
        # 미주입 시 agent 가 config.project.root → CWD fallback → qapilot 자체 dir 을 읽음 (회귀 원인).
        "qapilot_dir": state.get("qapilot_dir"),
    }

    # natural_lang: existing_scenarios 불필요 — requirements의 target_ts_id/target_tc_id로
    # ScenarioGeneratorAgent가 qapilot_dir/scenarios/{ts_id}.json을 직접 로드하면 됨

    agent = ScenarioGeneratorAgent(trace_id=state["trace_id"])
    output = await agent.run(
        AgentInput(
            trace_id=state["trace_id"],
            context=context,
            params={"trigger": trigger, "affected_only": affected_only},
        )
    )
    scenarios = output.result.get("scenarios", []) or []
    # agent_logs append — runner.run_pipeline 의 total_cost 집계 (#227).
    agent_logs = state.get("agent_logs", []) + [output.metadata.model_dump()]
    result: dict = {"scenarios": scenarios, "agent_logs": agent_logs}
    # code_change router-based 에서 탐지된 orphaned TS id 목록 — _save_scenarios 로 전달
    orphaned_ts_ids = output.result.get("orphaned_ts_ids")
    if orphaned_ts_ids:
        result["orphaned_ts_ids"] = orphaned_ts_ids
    return result


async def _save_scenarios(state: PipelineState) -> dict:
    """생성된 시나리오를 .qapilot/scenarios/{ts_id}.json 에 저장 (spec §6.1, L2).

    동시에 RTM (Requirements Traceability Matrix) 버전 1개를 .qapilot/rtm-versions/ 에
    자동 생성한다. 요구사항(state.requirements) ↔ TC(state.scenarios[].test_cases[].req_id)
    매핑만 저장하고 status / passCount 는 Spring 이 응답 시점에 동적 계산한다.
    """
    scenarios = state.get("scenarios") or []
    requirements = state.get("requirements") or []
    logger = get_logger(source="orchestrator", trace_id=state.get("trace_id"))

    # natural_lang에서 insufficient/rejected 반환 시 시나리오 저장 없이 통과 (이슈 #182)
    query_status = state.get("query_status")
    if query_status and query_status != "sufficient":
        logger.info("save_scenarios_skipped", query_status=query_status)
        return {
            "saved_scenario_paths": [],
            "status": "completed",
            "query_status": query_status,
            "query_feedback": state.get("query_feedback"),
        }

    logger.info(
        "save_scenarios_invoked",
        scenarios_count=len(scenarios),
        requirements_count=len(requirements),
    )
    scenarios_dir = _qapilot_path(state, "scenarios")
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    trigger = state["run_options"].get("trigger") or "init"
    saved_paths: list[str] = []
    # service_id 는 trace.json 에서 — Spring 이 create_trace 시점에 넣어둔 값.
    trace = load_trace(state["trace_id"]) or {}
    service_id = trace.get("service_id")

    # requirements 가 비어있으면 RTM fallback 과 동일하게 TC.req_id 를 FR-{ts_id} 로 미리 채움.
    # 디스크/DB 저장이 fallback 보다 먼저 일어나는 순서 문제 회피 — _write_initial_rtm_version 의
    # 같은 블록은 멱등이라 그대로 둠.
    def _backfill_req_id(ts_dict: Any, ts_id: str) -> None:
        if requirements:
            return
        # Support both dict-like scenarios and object-based (e.g. dataclass/Pydantic) TestScenario
        if isinstance(ts_dict, dict):
            tcs = ts_dict.get("test_cases") or []
        else:
            tcs = getattr(ts_dict, "test_cases", []) or []

        for tc in tcs:
            if isinstance(tc, dict):
                if not tc.get("req_id"):
                    tc["req_id"] = f"FR-{ts_id}"
            else:
                # try attribute access / assignment for object-like TC
                if not getattr(tc, "req_id", None):
                    try:
                        setattr(tc, "req_id", f"FR-{ts_id}")
                    except Exception:
                        # last-resort: mutate __dict__ if available
                        if hasattr(tc, "__dict__"):
                            tc.__dict__["req_id"] = f"FR-{ts_id}"

    if trigger == "natural_lang":
        # natural_lang: delta(신규/수정 시나리오)만 저장, 기존 시나리오 파일 유지 (이슈 #180)
        # ScenarioGeneratorAgent가 반환한 시나리오만 쓰고, 나머지는 건드리지 않음.
        change_summary: list[str] = []
        for idx, ts in enumerate(scenarios, start=1):
            ts_id = ts.get("ts_id") or f"TS-{idx:03d}"
            _backfill_req_id(ts, ts_id)
            ts_delete_requested = ts.pop("_ts_delete_requested", False)
            if ts_delete_requested:
                if service_id:
                    upsert_change_request(
                        service_id=service_id,
                        scenario_id=ts_id,
                        trigger="chatbot",
                        reason=f"시나리오 삭제 요청: {ts.get('name') or ts_id}",
                        content={"delete_ts": True},
                    )
                continue
            raw_change_type = ts.pop("_change_type", "ts_created")
            change_type = raw_change_type if isinstance(raw_change_type, str) else "ts_created"
            _tmp_change_target = ts.pop("_change_target", None)
            change_target = str(_tmp_change_target) if _tmp_change_target is not None else None
            change_tc_ids = ts.pop("_change_tc_ids", None)
            deleted_tc_ids = ts.pop("_deleted_tc_ids", None)
            # None이 아닌 경우(빈 리스트 포함)는 명시적으로 content에 기록한다.
            # `if change_tc_ids`는 []를 None과 동일하게 취급해 content가 NULL로 저장되고,
            # 프론트엔드가 "데이터 없음"으로 간주해 TS 전체 TC를 강조하는 문제가 생긴다.
            if change_tc_ids is not None or deleted_tc_ids is not None:
                change_content: dict | None = {}
                if change_tc_ids is not None:
                    change_content["changed_tc_ids"] = change_tc_ids
                if deleted_tc_ids is not None:
                    change_content["deleted_tc_ids"] = deleted_tc_ids
            else:
                change_content = None
            path = scenarios_dir / f"{ts_id}.json"
            path.write_text(
                json.dumps(ts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_paths.append(str(path))
            is_unchanged = ts.pop("_unchanged", False)
            tc_count = len(ts.get("test_cases") or [])
            logger.info("scenario_save_debug", ts_id=ts_id, tc_count=tc_count, unchanged=is_unchanged)
            if service_id:
                payload = _ensure_payload_dict(ts)
                upsert_scenario_version(service_id, ts_id, payload)
                await _upsert_scenario_index(service_id, payload)
                if not is_unchanged:
                    upsert_change_request(
                        service_id=service_id,
                        scenario_id=ts_id,
                        trigger="chatbot",
                        reason=payload.get("name") or ts_id,
                        target_id=change_target,
                        content=change_content,
                    )
            if not is_unchanged:
                _build_change_summary(change_summary, change_type, _ensure_payload_dict(ts), ts_id, change_target)
                logger.info(
                    "scenario_merged",
                    ts_id=ts_id,
                    action_type=ts.get("action_type", "create"),
                    unchanged=is_unchanged,
                )
    else:
        for idx, ts in enumerate(scenarios, start=1):
            ts_id = ts.get("ts_id") or f"TS-{idx:03d}"
            _backfill_req_id(ts, ts_id)
            # update 분기에서 마킹한 내부 마커 — 저장 파일에는 남기지 않고
            # change_request 등록 시 강조 범위(target_id/content)로만 활용한다.
            is_unchanged = ts.pop("_unchanged", False)
            ts.pop("_change_type", None)
            _tmp_change_target = ts.pop("_change_target", None)
            change_target = str(_tmp_change_target) if _tmp_change_target is not None else None
            change_tc_ids = ts.pop("_change_tc_ids", None)
            deleted_tc_ids = ts.pop("_deleted_tc_ids", None)
            # changed_tc_ids와 deleted_tc_ids 모두 content에 포함 — UI가 각각 노란/빨간 스타일로 구분
            if change_tc_ids is not None or deleted_tc_ids is not None:
                change_content: dict | None = {}
                if change_tc_ids is not None:
                    change_content["changed_tc_ids"] = change_tc_ids
                if deleted_tc_ids is not None:
                    change_content["deleted_tc_ids"] = deleted_tc_ids
            else:
                change_content = None
            path = scenarios_dir / f"{ts_id}.json"
            path.write_text(
                json.dumps(ts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_paths.append(str(path))
            if service_id:
                payload = _ensure_payload_dict(ts)
                upsert_scenario_version(service_id, ts_id, payload)
                await _upsert_scenario_index(service_id, payload)
                # code_change/doc_update 로 (재)생성된 시나리오는 AI 변경 요청으로 등록해
                # 목록에 "AI 생성" 표시(검토 대기) 가 뜨도록 한다 — natural_lang(chatbot) 과
                # 동일한 검토 흐름을 code/file 트리거에도 적용 (이슈 #180 후속).
                # 단, 재생성 결과가 기존과 실질적으로 동일하면(_unchanged) 표시하지 않는다
                # — _diff_status="updated" 오분류로 인한 불필요한 검토 표시 방지 (이슈 #261 후속).
                if is_unchanged:
                    pass
                elif trigger == "code_change":
                    upsert_change_request(
                        service_id=service_id,
                        scenario_id=ts_id,
                        trigger="code",
                        reason=payload.get("name") or ts_id,
                        target_id=change_target,
                        content=change_content,
                    )
                elif trigger == "doc_update":
                    upsert_change_request(
                        service_id=service_id,
                        scenario_id=ts_id,
                        trigger="file",
                        reason=payload.get("name") or ts_id,
                        target_id=change_target,
                        content=change_content,
                    )

    # RTM 버전 자동 생성 — natural_lang delta 저장 시에는 skip (전체 시나리오 기준이 아니므로)
    if trigger != "natural_lang":
        try:
            rtm_scenarios: list[dict] | None = None
            if trigger == "doc_update":
                # 증분 재생성(이슈 #261): state.scenarios 는 변경분(delta)만 담고 있으므로
                # 보존된 기존 시나리오와 병합한 전체 목록을 RTM 매핑 기준으로 사용한다 —
                # 그래야 미변경 시나리오의 req_id↔TC 매핑도 RTM 에 계속 반영된다.
                merged_by_id: dict[str, Any] = {}
                for existing_ts in _load_existing_scenarios_full(state):
                    ts_id = existing_ts.get("ts_id")
                    if ts_id:
                        merged_by_id[ts_id] = existing_ts
                for ts in scenarios:
                    ts_id = ts.get("ts_id")
                    if ts_id:
                        merged_by_id[ts_id] = ts
                rtm_scenarios = list(merged_by_id.values())
            _write_initial_rtm_version(state, scenarios=rtm_scenarios)
        except Exception as e:
            # RTM 생성 실패는 본 시나리오 생성 흐름을 막지 않도록 silent.
            logger = get_logger(source="orchestrator", trace_id=state.get("trace_id"))
            logger.warning("rtm_version_write_failed", error=str(e))

    # 고아 시나리오(이슈 #261) — 새 PRD 에서 매핑 요구사항이 사라진 기존 시나리오를
    # "검토 대상"으로 표시한다. trigger="orphaned" 로 등록해 "AI 생성"(검토 대기)과
    # 구분되는 별도 스타일(빨간색 + AlertTriangle)로 렌더링되도록 한다.
    orphaned_ts_ids = state.get("orphaned_ts_ids") or []
    if trigger == "doc_update" and service_id and orphaned_ts_ids:
        for ts_id in orphaned_ts_ids:
            upsert_change_request(
                service_id=service_id,
                scenario_id=ts_id,
                trigger="orphaned",
                reason="PRD에서 관련 요구사항이 제거되어 검토가 필요합니다",
            )
        logger.info("doc_update_orphaned_scenarios_flagged", ts_ids=orphaned_ts_ids)

    if trigger == "code_change" and service_id and orphaned_ts_ids:
        for ts_id in orphaned_ts_ids:
            upsert_change_request(
                service_id=service_id,
                scenario_id=ts_id,
                trigger="orphaned",
                reason="연결된 라우터 파일이 삭제되어 검토가 필요합니다",
            )
        logger.info("code_change_orphaned_scenarios_flagged", ts_ids=orphaned_ts_ids)

    # 첫 마일스톤 v1.0 자동 박제 — init 트리거 + service 의 첫 시나리오 생성일 때.
    # UNIQUE(service_id, label) 제약 덕에 재실행해도 멱등 (이미 v1.0 있으면 skip).
    if trigger == "init" and service_id and scenarios:
        try:
            from qapilot.db.scenario_version_writer import insert_initial_milestone

            # ensure each TestScenario-like object is normalized to a plain dict
            created = insert_initial_milestone(
                service_id,
                [ _ensure_payload_dict(ts) for ts in scenarios ],
                label="v1.0",
                description="초기 자동 생성",
            )
            logger.info("initial_milestone", created=created, label="v1.0")
        except Exception as e:
            logger.warning("initial_milestone_failed", error=str(e))

    result: dict = {
        "saved_scenario_paths": saved_paths,
        "status": "completed",
    }
    if trigger == "natural_lang" and change_summary:
        result["change_summary"] = change_summary
    return result


def _build_change_summary(
    summary: list[str],
    change_type: str,
    ts: dict,
    ts_id: str,
    change_target: str | None,
) -> None:
    """변경 내용을 사람이 읽기 좋은 문장으로 요약해 summary에 추가한다."""
    ts_name = ts.get("name", ts_id)
    tcs = ts.get("test_cases") or []
    if change_type == "ts_created":
        tc_names = ", ".join(tc.get("name", "") for tc in tcs[:3])
        suffix = f" 등 {len(tcs)}개 TC" if len(tcs) > 3 else f" {len(tcs)}개 TC"
        summary.append(f"{ts_id} '{ts_name}' 시나리오 생성 ({tc_names}{suffix})")
    elif change_type == "tc_added":
        last_tc = tcs[-1] if tcs else {}
        summary.append(f"{change_target or ts_id} 시나리오에 TC '{last_tc.get('name', '')}' 추가")
    elif change_type == "tc_updated":
        summary.append(f"{change_target or ts_id} TC 수정 완료")
    elif change_type == "ts_updated":
        summary.append(f"{ts_id} '{ts_name}' 시나리오 수정 ({len(tcs)}개 TC)")
    else:
        summary.append(f"{ts_id} 시나리오 업데이트")


def _ensure_payload_dict(obj: Any) -> dict:
    """Normalize a TestScenario-like object to a plain dict for DB/API writers."""
    if isinstance(obj, dict):
        return obj
    # pydantic v2 model_dump
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return obj.model_dump()
        except Exception:
            pass
    # pydantic v1 dict()
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        try:
            return obj.dict()
        except Exception:
            pass
    try:
        return dict(obj)
    except Exception:
        return {}


def _write_initial_rtm_version(state: PipelineState, scenarios: list[Any] | None = None) -> None:
    """state.requirements 와 scenarios 의 TC 들을 매핑해 RTM 버전 JSON 을 디스크에 저장.

    label 은 기존 RTM 버전 수 +1 기준 vN.0 (예: v1.0, v2.0). status/카운트는 빈 채로
    두고 Spring 응답 시점 동적 계산.

    state.requirements 가 비어있으면, scenarios 의 ts_id 별로 placeholder FR 을 만들어
    최소한 RTM 구조는 항상 생성한다 (UI 에 빈 RTM 페이지 보이지 않도록).

    Args:
        scenarios: RTM 매핑에 사용할 전체 시나리오 목록. 미지정 시 state.scenarios 사용.
            doc_update 증분 재생성(이슈 #261)에서는 state.scenarios 가 변경분(delta)만
            담고 있으므로, 호출부가 기존 시나리오와 병합한 전체 목록을 명시적으로 넘긴다 —
            그래야 보존된(미변경) 시나리오의 req_id↔TC 매핑도 RTM 에 반영된다.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    logger = get_logger(source="orchestrator", trace_id=state.get("trace_id"))
    requirements = state.get("requirements") or []
    scenarios = scenarios if scenarios is not None else (state.get("scenarios") or [])
    logger.info(
        "rtm_write_invoked",
        requirements_count=len(requirements),
        scenarios_count=len(scenarios),
    )
    if not scenarios:
        logger.info("rtm_write_skipped", reason="no scenarios")
        return

    # requirements 가 비어있는 경우 fallback — 각 TS 별로 placeholder FR 생성.
    # 이렇게 하면 LLM 이 req_id 안 채워도 최소한 RTM 페이지가 보임.
    if not requirements:
        logger.warning("rtm_using_ts_fallback", reason="empty requirements list")
        requirements = []
        for ts in scenarios:
            ts_id = ts.get("ts_id")
            if not ts_id:
                continue
            requirements.append({
                "req_id": f"FR-{ts_id}",
                "content": ts.get("name") or ts.get("description") or ts_id,
            })
            # TC 들에도 req_id 강제 주입 (이 후 매핑 작업용)
            for tc in ts.get("test_cases") or []:
                if not tc.get("req_id"):
                    tc["req_id"] = f"FR-{ts_id}"

    # req_id → [tc_id] 매핑 구성
    req_to_tcs: dict[str, list[str]] = {}
    for ts in scenarios:
        for tc in ts.get("test_cases") or []:
            req_id = tc.get("req_id")
            tc_id = tc.get("tc_id")
            if not req_id or not tc_id:
                continue
            req_to_tcs.setdefault(req_id, []).append(tc_id)

    rtm_requirements: list[dict] = []
    for req in requirements:
        req_id = req.get("req_id")
        if not req_id:
            continue
        rtm_requirements.append({
            "frId": req_id,
            "content": req.get("content") or "",
            "status": "미측정",
            "passCount": 0,
            "totalCount": len(req_to_tcs.get(req_id, [])),
            "history": [],
            "linkedTcIds": req_to_tcs.get(req_id, []),
        })

    rtm_dir = _qapilot_path(state, "rtm-versions")
    rtm_dir.mkdir(parents=True, exist_ok=True)
    existing_count = len(list(rtm_dir.glob("*.json")))
    label = f"v{existing_count + 1}.0"

    rtm_version_id = str(_uuid.uuid4())
    rtm_version = {
        "rtmVersionId": rtm_version_id,
        "serviceId": "",  # FastAPI 는 service_id 모름 — Spring 이 listAll 시 디렉토리 위치 기반으로 알아냄
        "label": label,
        "traceId": state.get("trace_id"),
        "requirements": rtm_requirements,
        "summary": {
            "total": len(rtm_requirements),
            "satisfied": 0,
            "unsatisfied": 0,
            "unmeasured": len(rtm_requirements),
        },
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (rtm_dir / f"{rtm_version_id}.json").write_text(
        json.dumps(rtm_version, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "rtm_version_written",
        rtm_version_id=rtm_version_id,
        label=label,
        fr_count=len(rtm_requirements),
    )

    # DB mirror — service_id 가 trace.json 에 있어야 함. 없으면 graceful skip.
    trace_loaded = load_trace(state["trace_id"]) or {}
    service_id_for_db = trace_loaded.get("service_id")
    if service_id_for_db:
        write_rtm_version(
            service_id=service_id_for_db,
            trace_id=state.get("trace_id"),
            label=label,
            requirements=rtm_requirements,
            summary=rtm_version["summary"],
        )


async def _load_scenarios_for_codegen(state: PipelineState) -> dict:
    """.qapilot/scenarios/ 에서 시나리오를 로드한다 (코드 생성용)."""
    import json
    from pathlib import Path

    scenarios_dir = _qapilot_path(state, "scenarios")
    raw_scenario_ids = state["run_options"].get("scenario_ids")
    explicit_scenario_filter = raw_scenario_ids is not None or bool(state["run_options"].get("incremental"))
    scenario_ids = raw_scenario_ids or []
    scenarios = []
    service_id = state.get("service_id") or (load_trace(state.get("trace_id", "")) or {}).get("service_id")

    if service_id and not (explicit_scenario_filter and not scenario_ids):
        from qapilot.db.scenario_reader import load_latest_scenarios
        scenarios = load_latest_scenarios(str(service_id), scenario_ids or None)

    if not scenarios and scenarios_dir.exists() and not (explicit_scenario_filter and not scenario_ids):
        for path in scenarios_dir.glob("*.json"):
            if path.name == "raw":
                continue
            if path.name == "regression":
                continue
            ts_id = path.stem
            if scenario_ids and ts_id not in scenario_ids:
                continue
            try:
                ts = json.loads(path.read_text(encoding="utf-8"))
                scenarios.append(ts)
            except Exception:
                pass

    scenarios.sort(key=lambda x: x.get("ts_id", ""))

    # scan_result: DB/S3 → 디스크 fallback
    scan_result = None
    frontend_dom: list[dict] = []
    if service_id:
        endpoints_db = load_codebase_index(str(service_id), "endpoints")
        if endpoints_db:
            scan_result = {"files": [{"path": "mock", "endpoints": endpoints_db}]}
        frontend_db = load_codebase_index(str(service_id), "frontend")
        if isinstance(frontend_db, dict):
            frontend_dom = list(frontend_db.get("elements") or [])
    if scan_result is None:
        try:
            endpoints_path = _qapilot_path(state, "codebase-index", "endpoints.json")
            if endpoints_path.exists():
                endpoints = json.loads(endpoints_path.read_text(encoding="utf-8"))
                scan_result = {"files": [{"path": "mock", "endpoints": endpoints}]}
        except Exception:
            pass
    if not frontend_dom:
        try:
            frontend_path = _qapilot_path(state, "codebase-index", "frontend.json")
            if frontend_path.exists():
                frontend_payload = json.loads(frontend_path.read_text(encoding="utf-8"))
                if isinstance(frontend_payload, dict):
                    frontend_dom = list(frontend_payload.get("elements") or [])
        except Exception:
            pass

    return {
        "scenarios": scenarios,
        "scan_result": scan_result,
        "frontend_dom": frontend_dom,
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
                "scan_result": state.get("scan_result"),
                "frontend_dom": state.get("frontend_dom") or [],
                "qapilot_dir": state.get("qapilot_dir"),
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
    """CodeGenerator 호출. Agent 실패 시 generated_codes=[] 로 graceful — pipeline 계속.

    이슈 #105: Agent 가 단일 LLM 호출로 78 TC 를 처리하다 JSON parse 실패 시 retry 4회
    후 AgentExecutionError 가 pipeline 전체를 중단시켰음. 그 결과 직전 노드의
    ActionMapping 78건이 `_save_codes` 미도달로 모두 휘발.

    CodeGen 실패 시에도 Layer 2 는 ActionMapping fallback 으로 계속 진행 가능해야 한다.
    현재 Layer 2 는 generated code 실행을 우선하되, 코드가 없으면 ActionMapping fallback
    을 사용한다. `_save_codes` 가 ActionMapping/generated_codes 둘 다 처리하므로 여기서는
    fail 흡수만 한다.
    """
    from qapilot.agents.code_generator_agent import CodeGeneratorAgent
    from qapilot.shared.schemas import AgentInput

    import structlog
    logger = structlog.get_logger("orchestrator")

    action_mappings = state.get("action_mappings") or []
    agent_logs = state.get("agent_logs", [])

    try:
        agent = CodeGeneratorAgent(trace_id=state.get("trace_id"))
        result = await agent.run(
            AgentInput(
                trace_id=state.get("trace_id") or "",
                context={
                    "action_mappings": action_mappings,
                    "scenarios": state.get("scenarios", []),
                    "frontend_dom": state.get("frontend_dom") or [],
                    "qapilot_dir": state.get("qapilot_dir"),
                },
                params={},
            )
        )
        generated_codes = result.result.get("generated_codes", [])
        failed_tcs = result.result.get("failed_tcs", [])
        agent_logs = agent_logs + [result.metadata.model_dump()]
        logger.info(
            "code_generate_complete",
            trace_id=state.get("trace_id"),
            tc_count=len(action_mappings),
            generated_count=len(generated_codes),
            failed_count=len(failed_tcs),
        )
        if failed_tcs:
            # 이슈 #107: TC-별 분할 후 부분 실패 TC 의 가시성. spec §4.5.1 LENIENT 부분 성공.
            logger.warning(
                "code_generate_partial_failure",
                trace_id=state.get("trace_id"),
                failed_tc_ids=[ft.get("tc_id") for ft in failed_tcs],
                note=f"{len(failed_tcs)}/{len(action_mappings)} TC 코드 생성 실패 — ActionMapping 으로 UITestTool 실행 가능 (spec §4.5)",
            )
        # failed_tcs 는 logging 만 활용 — PipelineState 에 별도 키 필요 없음 (다음 노드는
        # generated_codes 의 유무로 처리. ActionMapping 은 _save_codes 가 따로 디스크 저장).
        return {"generated_codes": generated_codes, "agent_logs": agent_logs}
    except Exception as e:
        logger.warning(
            "code_generate_failed_graceful",
            trace_id=state.get("trace_id"),
            error=f"{type(e).__name__}: {e}",
            action_mapping_count=len(action_mappings),
            note="ActionMapping 만 저장됩니다. Layer 2 는 generated code 부재 시 ActionMapping fallback 으로 진행합니다.",
        )
        return {"generated_codes": [], "agent_logs": agent_logs}


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
    deleted_tc_ids = (state.get("run_options") or {}).get("deleted_tc_ids") or []

    code_dir = _qapilot_path(state, "generated-code")
    am_dir = _qapilot_path(state, "action-mappings")
    code_dir.mkdir(parents=True, exist_ok=True)
    am_dir.mkdir(parents=True, exist_ok=True)

    saved_code_paths: list[str] = []
    # PR-17 — DB+S3 dual-write. trace.json 에서 service_id lookup. 없으면 graceful skip.
    trace = load_trace(state["trace_id"]) or {}
    service_id = trace.get("service_id")

    for tc_id in deleted_tc_ids:
        if not tc_id:
            continue
        for path in (code_dir / f"{tc_id}.js", am_dir / f"{tc_id}.json"):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    for code_obj in generated_codes:
        tc_id = code_obj.get("tc_id")
        if not tc_id:
            continue
        code_text = code_obj.get("code", "")
        path = code_dir / f"{tc_id}.js"
        path.write_text(code_text, encoding="utf-8")
        saved_code_paths.append(str(path))
        if service_id:
            upsert_generated_code(service_id, tc_id, code_text)

    # ActionMapping 디스크 영속화 (Layer 2 의 _load_scenarios_for_test 가 읽음)
    for am in action_mappings:
        tc_id = am.get("tc_id")
        if not tc_id:
            continue
        path = am_dir / f"{tc_id}.json"
        # normalize to plain dict (supports dict-like or pydantic/model objects)
        payload = _ensure_payload_dict(am)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if service_id:
            upsert_action_mapping(service_id, tc_id, payload)

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


def _load_remote_tc_artifacts(service_id: str, tc_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """S3/DB mirror 에서 최신 action mapping / generated code 를 로드한다."""
    action_mappings: list[dict] = []
    generated_codes: list[dict] = []
    for tc_id in tc_ids:
        am = load_latest_action_mapping(service_id, tc_id)
        if isinstance(am, dict):
            action_mappings.append(am)
        gc = load_latest_generated_code(service_id, tc_id)
        if isinstance(gc, dict):
            generated_codes.append(gc)
    return action_mappings, generated_codes


def _topo_sort_scenarios(scenarios: list[Any]) -> list[dict]:
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


def _collect_tc_tags(scenarios: list[Any]) -> dict[str, list[str]]:
    """TC id → tag 목록 dict."""
    result: dict[str, list[str]] = {}
    for ts in scenarios:
        for tc in ts.get("test_cases") or []:
            tc_id = tc.get("tc_id")
            if tc_id:
                result[tc_id] = tc.get("tags") or []
    return result


def _ts_id_of_tc(tc_id: str, scenarios: list[Any]) -> str:
    """TC id → 소속 TS id. 매칭 실패 시 'unknown'."""
    for ts in scenarios:
        for tc in ts.get("test_cases") or []:
            if tc.get("tc_id") == tc_id:
                return str(ts.get("ts_id") or "unknown")
    return "unknown"


async def _load_scenarios_for_test(state: PipelineState) -> dict:
    """Layer 2 진입 노드 — 디스크에서 scenarios + action_mappings + generated_codes 로드.

    spec §6.1 정합. PipelineState 가 휘발성이므로 `qapilot test` 단독 실행 시 디스크에서 채움.
    필터 적용: run_options.scenario_ids / tags. depends_on 기반 토폴로지 정렬.
    """
    import uuid as _uuid

    trace_id = state.get("trace_id") or str(_uuid.uuid4())
    trace = load_trace(trace_id) or {}
    service_id = trace.get("service_id") or state.get("service_id")

    scenarios = []
    if service_id:
        from qapilot.db.scenario_reader import load_latest_scenarios
        scenario_ids = (state.get("run_options") or {}).get("scenario_ids") or None
        scenarios = load_latest_scenarios(str(service_id), scenario_ids)
    if not scenarios:
        scenarios = _load_json_files(_qapilot_path(state, "scenarios"))
    tc_ids = [
        tc.get("tc_id")
        for s in scenarios
        for tc in (s.get("test_cases") or [])
        if tc.get("tc_id")
    ]

    remote_action_mappings: list[dict] = []
    remote_generated_codes: list[dict] = []
    if service_id and tc_ids:
        remote_action_mappings, remote_generated_codes = _load_remote_tc_artifacts(
            str(service_id), [str(tc_id) for tc_id in tc_ids]
        )

    action_mappings = remote_action_mappings or _load_json_files(_qapilot_path(state, "action-mappings"))

    # generated_codes 는 S3 mirror 우선, 없으면 디스크 fallback.
    generated_codes: list[dict] = remote_generated_codes
    if not generated_codes:
        codes_dir = _qapilot_path(state, "generated-code")
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

    # "선택한 시나리오들의 전체 TC 수" 기록 — P/F/N 의 분모. resume 시 새로 실행한 TC 만이
    # 아니라, 사용자가 선택한 시나리오의 전체 TC 수가 total 이 되어야 함.
    selected_total_tc_count = sum(
        len(s.get("test_cases") or []) for s in scenarios
    )
    try:
        from qapilot.shared.trace_store import annotate_trace as _annotate
        _annotate(trace_id, selected_total_tc_count=selected_total_tc_count)
    except Exception:
        pass

    # 필터 — run_options.resume_from_trace (이어서 실행)
    # 이전 trace 의 results 디렉토리에 ui_result.json 이 있는 TC 는 이미 실행 완료된 것으로
    # 간주하고 스킵. 끊긴 시점부터 이어가기 위함.
    resume_from = state["run_options"].get("resume_from_trace")
    if resume_from:
        prev_results = _qapilot_path(state, "results", resume_from)
        completed_tc_ids: set[str] = set()
        if prev_results.exists():
            for ui_path in prev_results.rglob("ui_result.json"):
                completed_tc_ids.add(ui_path.parent.name)
        if not completed_tc_ids:
            from qapilot.db.tc_result_reader import load_completed_tc_ids as _load_done
            completed_tc_ids = _load_done(resume_from)
        if completed_tc_ids:
            action_mappings = [a for a in action_mappings if a.get("tc_id") not in completed_tc_ids]
            generated_codes = [c for c in generated_codes if c.get("tc_id") not in completed_tc_ids]

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
    Playwright Chromium 부재 시 자동 다운로드 (FR-006, 이슈 #97).
    """
    from playwright.async_api import async_playwright

    from qapilot.cli._ensure_browser import ensure_chromium_for_test_async
    from qapilot.shared.config import load_config
    from qapilot.shared.errors import ErrorCode, ToolExecutionError
    from qapilot.shared.schemas import ToolInput
    from qapilot.tools.api_trace_tool import APITraceTool
    from qapilot.tools.db_test_tool import DBTestTool
    from qapilot.tools.ui_test_tool import UITestTool

    # Playwright Chromium 자동 셋업 (lazy, async 안전) — RuntimeWarning 차단
    if not await ensure_chromium_for_test_async():
        raise ToolExecutionError(
            ErrorCode.TOOL_UI_NAVIGATION_FAIL,
            "Playwright Chromium 설치 실패. 수동 명령: `python -m playwright install chromium`",
        )

    trace_id = state["trace_id"]
    scenarios = state.get("scenarios") or []
    action_mappings = state.get("action_mappings") or []
    generated_codes = state.get("generated_codes") or []
    cfg = load_config()
    headless = bool(getattr(cfg.test, "headless", True)) if hasattr(cfg, "test") else True
    # SaaS 호출 경로(Spring) 에서는 state.staging_url 이 service.stagingUrl 로 채워져 있다.
    # CLI 단독 실행에서는 비어있으므로 cfg.project.target_url 로 fallback.
    target_url = state.get("staging_url") or (
        getattr(cfg.project, "target_url", "") if hasattr(cfg, "project") else ""
    )

    # 이슈 #174 (격차 12 D 영역): UITestTool 의 _ensure_authenticated fail-safe 용 test_account.
    # 격차 12 SaaS 후속 (2026-06-02): state.test_account 우선 (Spring 이 body 로 채움) →
    # 없으면 cfg.project.test_account fallback (CLI 흐름 / dev 임시). staging_url 동형.
    test_account_state = state.get("test_account")
    test_account_dict: dict | None = (
        test_account_state if isinstance(test_account_state, dict) and test_account_state.get("email") and test_account_state.get("password") else None
    )
    if test_account_dict is None:
        test_account_cfg = getattr(cfg.project, "test_account", None) if hasattr(cfg, "project") else None
        if test_account_cfg and getattr(test_account_cfg, "email", None) and getattr(test_account_cfg, "password", None):
            test_account_dict = {
                "email": test_account_cfg.email,
                "password": test_account_cfg.password,
                "login_path": getattr(test_account_cfg, "login_path", None),
            }

    results_root = _qapilot_path(state, "results", trace_id)

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
            execution_items = generated_codes or action_mappings
            action_mapping_by_tc = {
                str(am.get("tc_id")): am for am in action_mappings if am.get("tc_id")
            }
            # tc_id → depends_on (TC간 상태 격리에서 의존 체인 예외 판정용)
            tc_depends_map = {
                str(tc.get("tc_id")): {str(d) for d in (tc.get("depends_on") or [])}
                for ts in (scenarios or [])
                for tc in (ts.get("test_cases") or [])
                if tc.get("tc_id")
            }
            prev_tc_id: str | None = None

            for item in execution_items:
                tc_id = item.get("tc_id") or "unknown"
                ts_id = _ts_id_of_tc(tc_id, scenarios)
                tc_dir = results_root / ts_id / tc_id
                screenshots_dir = tc_dir / "screenshots"
                tc_dir.mkdir(parents=True, exist_ok=True)

                # TC간 상태 격리 — 시작 전 브라우저 상태(localStorage 토큰·쿠키) 리셋.
                # 누수된 로그인 토큰이 /login → /dashboard 리다이렉트를 유발해 로그인 TC 가
                # 실패하고, 인증 필요 TC 는 _ensure_authenticated fixture 가 TC 마다 재로그인한다.
                # 단 직전 TC 에 depends_on 한 경우(연속 의존 체인)는 선행 상태를 이어받아야 하므로 제외.
                depends_on_prev = prev_tc_id is not None and prev_tc_id in tc_depends_map.get(str(tc_id), set())
                if not depends_on_prev:
                    try:
                        await page.evaluate(
                            "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                        )
                    except Exception:
                        pass  # 첫 TC 등 about:blank 상태 — 지울 것 없음
                    try:
                        await context.clear_cookies()
                        await page.goto("about:blank")
                    except Exception as e:
                        logger.warning("tc_state_reset_failed", tc_id=tc_id, error=str(e))
                prev_tc_id = str(tc_id)

                # item may be either a GeneratedCode dict or an ActionMapping dict.
                # Only call the parser when this item looks like generated code (has "code").
                if generated_codes and isinstance(item, dict) and item.get("code") is not None:
                    exec_mapping = _action_mapping_from_generated_code(item)
                    # api_endpoint 힌트는 기존 ActionMapping 의 값을 최대한 유지.
                    original = action_mapping_by_tc.get(str(tc_id)) or {}
                    original_steps = list(original.get("steps") or [])
                    for idx, step in enumerate(exec_mapping.get("steps") or []):
                        if idx < len(original_steps):
                            step["api_endpoint"] = original_steps[idx].get("api_endpoint")
                            # #256 본질 fix — generated_code 가 password 등을 process.env.*
                            # 로 마스킹. _resolve_js_value 가 환경변수 미설정 시 "" 반환 →
                            # fill('') → form 빈 채 → POST 0건. e2e trace `87041b5e` 진단
                            # (ui_fill_cached password value_len=0). 본인 누적 10 PR (D 영역
                            # race fix) 모두 본질 아니었음 — 진짜 본질은 generated_code 변환.
                            # ActionMapping 원본 value 가 있고 generated_code 의 value 가
                            # 빈 채면 원본으로 fallback (password masking 회피).
                            orig_value = original_steps[idx].get("value")
                            if orig_value and not step.get("value"):
                                step["value"] = orig_value
                else:
                    exec_mapping = item

                # per-TC 안전 실행 — 한 TC 의 UI 실행 실패(빈 steps·셀렉터 미해결·타임아웃 등)가
                # run 전체를 중단시키지 않도록 격리. 실패 TC 는 failed 로 기록 후 계속.
                try:
                    ui_res = await _run_ui_with_trace(
                        page=page,
                        tc_id=tc_id,
                        action_mapping=exec_mapping,
                        target_url=target_url,
                        screenshots_dir=screenshots_dir,
                        trace_id=trace_id,
                        UITestTool=UITestTool,
                        APITraceTool=APITraceTool,
                        ToolInput=ToolInput,
                        test_account=test_account_dict,
                    )
                    ui_results.append(ui_res["ui_result"])
                    api_results.append(ui_res["api_result"])
                except Exception as e:
                    logger.warning("ui_execution_failed", trace_id=trace_id, tc_id=tc_id, error=str(e))
                    ui_results.append({
                        "tc_id": tc_id,
                        "status": "fail",
                        "steps": [],
                        "total_duration_ms": 0,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    api_results.append({
                        "tc_id": tc_id, "calls": [], "total_calls": 0, "error_calls": 0,
                    })

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

                # L3 DB / S3 mirror — 실패해도 디스크 진실은 보존됨
                _mirror_tc_results_and_artifacts(
                    trace_id=trace_id,
                    ts_id=ts_id,
                    tc_id=tc_id,
                    ui_result=ui_res["ui_result"],
                    api_result=ui_res["api_result"],
                    db_result=db_res,
                    screenshots_dir=screenshots_dir,
                )
        finally:
            await context.close()
            await browser.close()

    # tc_results / scenario_results 집계는 디스크 기반 — 같은 trace_id 로 resume 한 경우
    # 이전 run 의 결과도 results 디렉토리에 누적되어 있으므로 디스크 스캔이 진실의 source.
    disk_ui, disk_api, disk_db = _load_all_tc_results_from_disk(results_root)
    tc_results = _aggregate_tc_results(disk_ui, disk_api, disk_db)
    scenario_results = _aggregate_scenario_results(tc_results, scenarios)

    return {
        "ui_results": ui_results,
        "api_results": api_results,
        "db_results": db_results,
        "tc_results": tc_results,
        "scenario_results": scenario_results,
    }


def _derive_api_status(payload: dict | None) -> str | None:
    """api kind 의 tc_results.status 도출 (#227).

    APITraceResult schema 에 status 키가 없어 자동 추출 None — error_calls 기반 명시 분류.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return "pass" if int(payload.get("error_calls", 0)) == 0 else "fail"
    except (TypeError, ValueError):
        return None


def _derive_db_status(payload: dict | None) -> str | None:
    """db kind 의 tc_results.status 도출 (#227).

    DBTestResult schema 에 status 키가 없어 자동 추출 None — summary "skip" 시작이면 skip,
    snapshots 비면 pass (기본 — DB 변화 없음 정상), else pass. 의미적 mismatch 검증은 cross_check kind 책임.
    """
    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("summary", "")).strip()
    if summary.lower().startswith("dbtest skip") or summary.lower().startswith("skip"):
        return "skip"
    return "pass"


def _mirror_tc_results_and_artifacts(
    *,
    trace_id: str,
    ts_id: str,
    tc_id: str,
    ui_result: dict,
    api_result: dict,
    db_result: dict,
    screenshots_dir: Path,
) -> None:
    """ui/api/db 결과 → tc_results UPSERT, 스크린샷 PNG → S3 + tc_artifacts.

    DB / S3 미설정/실패 시 모두 graceful — file 기록이 source of truth.
    """
    ui_result_id = upsert_tc_result(
        run_id=trace_id, ts_id=ts_id, tc_id=tc_id, kind="ui", payload=ui_result,
    )
    # api/db kind 의 payload 에는 status 키가 없어 (APITraceResult / DBTestResult schema)
    # upsert_tc_result 의 자동 추출이 None → DB status 컬럼 null 저장됨. 명시적 도출 (#227).
    upsert_tc_result(
        run_id=trace_id, ts_id=ts_id, tc_id=tc_id, kind="api", payload=api_result,
        status=_derive_api_status(api_result),
    )
    upsert_tc_result(
        run_id=trace_id, ts_id=ts_id, tc_id=tc_id, kind="db", payload=db_result,
        status=_derive_db_status(db_result),
    )

    # 스크린샷은 UI result 에 묶음. tc_result_id 없으면 (DB 비활성) S3 도 skip.
    if not ui_result_id or not screenshots_dir.exists():
        return

    for png in sorted(screenshots_dir.glob("step_*.png")):
        step_index = _parse_step_index(png.name)
        if step_index is None:
            continue
        s3_key = f"runs/{trace_id}/tc/{ts_id}/{tc_id}/screenshots/{png.name}"
        meta = s3_client.put_file(s3_key, str(png), content_type="image/png")
        if meta is None:
            continue
        insert_tc_artifact(
            tc_result_id=ui_result_id,
            step_index=step_index,
            kind="png",
            s3_key=s3_key,
            sha256=meta.get("sha256"),
            size_bytes=meta.get("bytes"),
        )


_STEP_INDEX_RE = re.compile(r"step_(\d+)\.")


def _parse_step_index(name: str) -> int | None:
    m = _STEP_INDEX_RE.search(name)
    return int(m.group(1)) if m else None


def _load_all_tc_results_from_disk(
    results_root: Path,
) -> tuple[list[dict], list[dict], list[dict]]:
    """results/{trace_id}/<ts>/<tc>/{ui,api,db}_result.json 전체를 로드한다.

    같은 trace 가 여러 차례 resume 되어 결과가 누적된 경우 전부 합쳐 반환.
    """
    ui_all: list[dict] = []
    api_all: list[dict] = []
    db_all: list[dict] = []
    if not results_root.exists():
        return ui_all, api_all, db_all
    for tc_dir in results_root.glob("*/*"):
        if not tc_dir.is_dir():
            continue
        for kind, bucket in (("ui", ui_all), ("api", api_all), ("db", db_all)):
            f = tc_dir / f"{kind}_result.json"
            if not f.exists():
                continue
            try:
                bucket.append(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return ui_all, api_all, db_all


def _aggregate_tc_results(
    ui_results: list[dict], api_results: list[dict], db_results: list[dict]
) -> dict[str, str]:
    """TC 별 종합 status 도출 — `passed` / `failed`.

    판정 기준: UI 가 pass/skip/fallback_used 이고, API error_calls 0, DB error 없음 → passed.
    하나라도 위반 → failed. spec §6 의 results/{trace}/{ts}/{tc}/*.json 과 정합.
    """
    ui_map: dict[str, Any] = {str(r["tc_id"]): r for r in ui_results if r.get("tc_id") is not None}
    api_map: dict[str, Any] = {str(r["tc_id"]): r for r in api_results if r.get("tc_id") is not None}
    db_map: dict[str, Any] = {str(r["tc_id"]): r for r in db_results if r.get("tc_id") is not None}

    tc_results: dict[str, str] = {}
    for tc_id in ui_map.keys():
        ui = ui_map.get(tc_id) or {}
        api = api_map.get(tc_id) or {}
        db = db_map.get(tc_id) or {}

        ui_status = ui.get("status", "")
        ui_ok = ui_status in ("pass", "skip", "fallback_used")
        api_ok = int(api.get("error_calls") or 0) == 0
        db_ok = not db.get("error")

        tc_results[tc_id] = "passed" if (ui_ok and api_ok and db_ok) else "failed"
    return tc_results


def _aggregate_scenario_results(
    tc_results: dict[str, str], scenarios: list[Any]
) -> dict[str, str]:
    """TS 별 status 도출 — 모든 TC passed → passed, 하나라도 failed → failed, TC 없으면 미수록.

    Scenario 에 속한 TC 중 실행된 것만 봄. 실행 안 된 TC 는 무시
    (selective 실행 시나리오 보존).
    """
    scenario_results: dict[str, str] = {}
    for ts in scenarios or []:
        ts_id = ts.get("ts_id")
        if not ts_id:
            continue
        ts_tc_ids = [tc.get("tc_id") for tc in (ts.get("test_cases") or []) if tc.get("tc_id")]
        ran = [tc_id for tc_id in ts_tc_ids if tc_id in tc_results]
        if not ran:
            continue
        scenario_results[ts_id] = (
            "failed" if any(tc_results[tc_id] == "failed" for tc_id in ran) else "passed"
        )
    return scenario_results


_LOCATOR_ASSIGN_RE = re.compile(
    r"""const\s+(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<expr>page\.(?:getByLabel|getByPlaceholder|getByText|getByTestId|getByAltText|getByTitle|locator)\(.+?\))\s*;"""
)
_PAGE_CALL_RE = re.compile(
    r"""await\s+page\.(?P<method>goto|reload|goBack|goForward|waitForTimeout|waitForURL|waitForLoadState|waitForResponse)\((?P<args>.*)\)\s*;"""
)
_LOCATOR_CALL_RE = re.compile(
    r"""await\s+(?P<expr>page\.(?:getByLabel|getByPlaceholder|getByText|getByTestId|getByAltText|getByTitle|locator)\(.+?\)|[A-Za-z_]\w*)\.(?P<method>fill|clear|click|dblclick|hover|selectOption|check|uncheck|press|setInputFiles)\((?P<args>.*)\)\s*;"""
)
_EXPECT_RE = re.compile(
    r"""await\s+expect\((?P<expr>page(?:\.(?:getByLabel|getByPlaceholder|getByText|getByTestId|getByAltText|getByTitle|locator)\(.+?\))?|[A-Za-z_]\w*)\)\.(?P<method>toBeVisible|toBeHidden|toHaveText|toHaveValue|toBeEnabled|toBeDisabled|toHaveCount|toHaveURL)\((?P<args>.*)\)\s*;"""
)
_LOCATOR_EXPR_RE = re.compile(
    r"""page\.(?P<kind>getByLabel|getByPlaceholder|getByText|getByTestId|getByAltText|getByTitle|locator)\((?P<args>.*)\)"""
)


def _action_mapping_from_generated_code(code_obj: Any) -> dict[str, Any]:
    """생성된 Playwright JS 코드의 표준 패턴을 ActionMapping 으로 복원한다."""
    payload = _ensure_payload_dict(code_obj)
    tc_id = str(payload.get("tc_id") or "unknown")
    code = str(payload.get("code") or "")
    locator_vars: dict[str, tuple[str, str]] = {}
    steps: list[dict[str, Any]] = []

    for raw_line in code.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        assign = _LOCATOR_ASSIGN_RE.match(line)
        if assign:
            parsed = _parse_locator_expr(assign.group("expr"))
            if parsed:
                locator_vars[assign.group("var")] = parsed
            continue

        page_call = _PAGE_CALL_RE.match(line)
        if page_call:
            method = page_call.group("method")
            arg = _first_arg(page_call.group("args"))
            if method == "goto":
                steps.append(_step("navigate", None, None, _resolve_js_value(arg), None, len(steps) + 1))
            elif method == "reload":
                steps.append(_step("reload", None, None, None, None, len(steps) + 1))
            elif method == "goBack":
                steps.append(_step("go_back", None, None, None, None, len(steps) + 1))
            elif method == "goForward":
                steps.append(_step("go_forward", None, None, None, None, len(steps) + 1))
            elif method == "waitForTimeout":
                steps.append(_step("wait", None, None, _resolve_js_value(arg), None, len(steps) + 1))
            elif method == "waitForURL":
                steps.append(_step("wait_for_url", None, None, _resolve_js_value(arg), None, len(steps) + 1))
            elif method == "waitForLoadState":
                steps.append(_step("wait_for_load_state", None, None, _resolve_js_value(arg), None, len(steps) + 1))
            elif method == "waitForResponse":
                steps.append(_step("wait_for_response", None, None, _resolve_js_value(arg), None, len(steps) + 1))
            continue

        loc_call = _LOCATOR_CALL_RE.match(line)
        if loc_call:
            locator = _resolve_locator_ref(loc_call.group("expr"), locator_vars)
            if locator:
                selector_type, selector = locator
                method = loc_call.group("method")
                action = {
                    "selectOption": "select",
                    "setInputFiles": "upload",
                }.get(method, method)
                value = _resolve_js_value(_first_arg(loc_call.group("args")))
                if action in {"clear", "click", "dblclick", "hover", "check", "uncheck"}:
                    value = None
                steps.append(_step(action, selector, selector_type, value, None, len(steps) + 1))
            continue

        exp = _EXPECT_RE.match(line)
        if exp:
            expr = exp.group("expr")
            method = exp.group("method")
            arg = _resolve_js_value(_first_arg(exp.group("args")))
            if expr == "page" and method == "toHaveURL":
                steps.append(_step("assert_url", None, None, None, arg, len(steps) + 1))
                continue
            locator = _resolve_locator_ref(expr, locator_vars)
            if not locator:
                continue
            selector_type, selector = locator
            action = {
                "toBeVisible": "assert_visible",
                "toBeHidden": "assert_hidden",
                "toHaveText": "assert_text",
                "toHaveValue": "assert_value",
                "toBeEnabled": "assert_enabled",
                "toBeDisabled": "assert_disabled",
                "toHaveCount": "assert_count",
            }[method]
            expected = arg if action in {"assert_text", "assert_value", "assert_count"} else None
            steps.append(_step(action, selector, selector_type, None, expected, len(steps) + 1))
            continue

    return {
        "tc_id": tc_id,
        "steps": steps,
        "selector_confidence": 1.0 if steps else 0.0,
        "source": "generated_code",
    }


def _resolve_locator_ref(expr: str, locator_vars: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    expr = expr.strip()
    if expr in locator_vars:
        return locator_vars[expr]
    return _parse_locator_expr(expr)


def _parse_locator_expr(expr: str) -> tuple[str, str] | None:
    match = _LOCATOR_EXPR_RE.match(expr.strip())
    if not match:
        return None
    kind = match.group("kind")
    arg = _resolve_js_value(_first_arg(match.group("args")))
    if not arg:
        return None
    selector_type = {
        "getByLabel": "label",
        "getByPlaceholder": "placeholder",
        "getByText": "text",
        "getByTestId": "testid",
        "getByAltText": "alttext",
        "getByTitle": "title",
        "locator": "css",
    }.get(kind)
    if not selector_type:
        return None
    return selector_type, arg


def _first_arg(args: str) -> str:
    return args.split(",", 1)[0].strip()


def _resolve_js_value(token: str | None) -> str | None:
    if token is None:
        return None
    token = token.strip().rstrip(";")
    if not token:
        return None
    if token in {"null", "undefined"}:
        return None
    if token.startswith(("'", '"')) and token.endswith(("'", '"')) and len(token) >= 2:
        return token[1:-1]
    env_match = re.fullmatch(r"process\.env\.([A-Z0-9_]+)", token)
    if env_match:
        return os.getenv(env_match.group(1)) or ""
    number_match = re.fullmatch(r"-?\d+(?:\.\d+)?", token)
    if number_match:
        return token
    return token


def _step(
    action: str,
    selector: str | None,
    selector_type: str | None,
    value: str | None,
    expected: str | None,
    step_no: int,
) -> dict[str, Any]:
    return {
        "step_no": step_no,
        "action": action,
        "selector": selector,
        "selector_type": selector_type,
        "value": value,
        "expected": expected,
        "api_endpoint": None,
    }


async def _run_ui_with_trace(
    *,
    page,
    tc_id: str,
    action_mapping: Any,
    target_url: str,
    screenshots_dir: Path,
    trace_id: str,
    UITestTool,
    APITraceTool,
    ToolInput,
    test_account: dict | None = None,
) -> dict:
    """APITraceTool 의 listener 등록 (즉시 반환) + UITestTool 실행 후 api calls 재계산."""
    apt = APITraceTool(trace_id=trace_id)
    apt_out = await apt.run(ToolInput(trace_id=trace_id, params={"page": page, "tc_id": tc_id}))
    api_trace = apt_out.result["api_trace"]

    ui_tool = UITestTool(trace_id=trace_id)
    ui_params = {
        "page": page,
        "action_mapping": action_mapping,
        "tc_id": tc_id,
        "target_url": target_url,
        "screenshot_dir": str(screenshots_dir),
    }
    if test_account:
        ui_params["test_account"] = test_account
    ui_out = await ui_tool.run(
        ToolInput(
            trace_id=trace_id,
            params=ui_params,
        )
    )

    # 이슈 #131 후속: APITraceTool listener cleanup.
    # 현재 TC 의 UI 조작이 끝났으므로, 다음 TC 에 현재 listener 가 반응하지 않도록 해제.
    try:
        page.remove_listener("request", apt._on_request)
        page.remove_listener("response", apt._on_response)
    except Exception:
        pass

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
    """DBTestTool graceful — env 부재 시 Tool 호출 자체 차단 (로그 노이즈 0).

    DBTestTool 본체가 `QAPILOT_SUT_DB_URL` 미설정 시 ValueError raise + BaseTool 가
    error 로그 출력. TC 별 노이즈 누적 방지를 위해 env 사전 점검으로 호출 자체를 skip.
    변수명은 PR #184 (이슈 #80) 와 정합 — sut-db-agent 의 클러스터 endpoint URL.
    """
    # db_test_tool._module_url() 사용 — _ensure_dotenv 자동 호출로 .env 강제 로드 보장
    # (PR #235 override revert 후 본질 fix #242). main.py 의 load_dotenv 타이밍 무관.
    from qapilot.tools.db_test_tool import _module_url

    if not _module_url():
        return {
            "tc_id": tc_id,
            "snapshots": [],
            "summary": "DBTest skip: QAPILOT_SUT_DB_URL 미설정 (env 사전 점검)",
        }

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
    ui_map: dict[str, Any] = {str(r["tc_id"]): r for r in ui_results if r.get("tc_id") is not None}
    api_map: dict[str, Any] = {str(r["tc_id"]): r for r in api_results if r.get("tc_id") is not None}
    db_map: dict[str, Any] = {str(r["tc_id"]): r for r in db_results if r.get("tc_id") is not None}

    cross_check_results: list[dict] = []
    any_mismatch = False
    # agent_logs 누적 append — runner.run_pipeline 의 total_cost 합산 (#232).
    # Layer 3 노드는 for loop 안 multi-TC 호출이라 매 TC 마다 누적 (PR #228 단일 호출 패턴 확장).
    agent_logs = state.get("agent_logs", [])

    # UI 단계 fail 도 mismatch 신호로 — CrossCheck 의 정합성 정의 (UI↔API↔DB) 만으론
    # UI 전체 실패 (locator timeout 등) 케이스가 Layer 3 진입 못 함. 본인 노드에서 보강.
    ui_failed_tc_ids = {
        tc_id for tc_id, r in ui_map.items()
        if r.get("status") == "fail"
    }
    if ui_failed_tc_ids:
        any_mismatch = True

    # DB / API 검증 부재 (env 부재 / k8s trace 캡쳐 실패 등) 도 식별 — 묵시 PASS 차단.
    # has_mismatch 는 변경 X (cost 폭증 방지) — cross_check tc_results.status 를
    # "unverified" 로 분리하여 false PASS 차단 + 리포트에 명시. 사용자가 환경 fix 후
    # 재실행하면 정상 검증. e2e trace `40fce3fa` 의 DB env 부재로 5/6 TC false PASS 격차.
    db_unverified_tc_ids = {
        tc_id for tc_id, r in db_map.items()
        if not r or (r.get("summary") or "").lower().startswith("dbtest skip")
    }
    api_unverified_tc_ids = {
        tc_id for tc_id, r in api_map.items()
        if not r or (
            (r.get("total_calls") or 0) == 0
            and not (r.get("calls") or [])
        )
    }

    # #259 본질 fix — 시나리오 본문 (then/tags/name) 을 tc_id 별 인덱싱.
    # cross_check 가 시나리오 의도 (positive/negative) 와 outcome 도달 여부를
    # 정확히 판정하려면 시나리오 본문 필수. e2e trace `2cf12739` 진단: TC-02
    # then="409 Conflict + Email already registered" actual 409 → 의도 도달
    # = pass 가 정답인데 has_mismatch=True 로 잘못 fail. cross_check 가 본문 안
    # 받음. 본 fix 로 scenarios state 에서 추출 + tc_intent 로 context 주입.
    scenarios = state.get("scenarios") or []
    tc_intent_map: dict[str, dict] = {}
    for sc in scenarios:
        for tc in (sc.get("test_cases") or []):
            tid = tc.get("tc_id")
            if not tid:
                continue
            tc_intent_map[str(tid)] = {
                "name": tc.get("name") or "",
                "tags": tc.get("tags") or [],
                "given": tc.get("given") or "",
                "when": tc.get("when") or "",
                "then": tc.get("then") or "",
            }

    for tc_id in ui_map.keys():
        ui_result = ui_map.get(tc_id, {})
        api_trace = api_map.get(tc_id, {})
        db_result = db_map.get(tc_id, {})
        scenario_intent = tc_intent_map.get(str(tc_id)) or {}

        agent = CrossCheckAgent(trace_id=trace_id)
        try:
            output = await agent.run(
                AgentInput(
                    trace_id=trace_id,
                    context={
                        "ui_result": ui_result,
                        "api_trace": api_trace,
                        "db_result": db_result,
                        # #259: 시나리오 의도 — cross_check agent 가 positive/negative
                        # 구분하여 의도 도달 시 has_mismatch=false 판정에 활용.
                        "scenario_intent": scenario_intent,
                    },
                    params={"tc_id": tc_id},
                )
            )
            agent_logs = agent_logs + [output.metadata.model_dump()]
            cc = dict(output.result.get("cross_check") or {})
            if not cc:
                cc = {
                    "tc_id": tc_id, "match_score": 0.0, "matched_fields": 0,
                    "mismatched_fields": 0, "mismatches": [], "has_mismatch": False,
                }
            # UI 단계 fail 인 TC 는 Layer 3 진입 위해 has_mismatch 강제 True.
            # #259 본질 보강: 시나리오 의도 negative + outcome 도달 시 ui_failed 라도
            # 강제 fail 처리하지 않음 (cross_check agent 의 판정 우선). 단순 ui_failed
            # 강제 True 가 negative test 의 의도된 form prevent / 4xx 도 fail 로 만드는
            # 격차. agent 가 시나리오 의도 도달 판정했으면 그 결과 보존.
            agent_says_intended = (
                output.result.get("intent_satisfied") is True
            )
            if tc_id in ui_failed_tc_ids and not agent_says_intended:
                cc["has_mismatch"] = True
                cc.setdefault("ui_failed", True)
            elif tc_id in ui_failed_tc_ids:
                # 의도 도달 — ui_failed 기록만 유지하고 has_mismatch 는 agent 판정 따름
                cc.setdefault("ui_failed", True)
                cc.setdefault("intent_satisfied", True)
            # DB / API 검증 부재 표시 — has_mismatch 변경 X (root_cause 호출 안 함)
            if tc_id in db_unverified_tc_ids:
                cc["db_unverified"] = True
            if tc_id in api_unverified_tc_ids:
                cc["api_unverified"] = True
            # RootCauseAgent에 직결되도록 error_code/summary를 cc에 보존
            cc["error_code"] = output.result.get("error_code") or ""
            cc["summary"] = output.result.get("summary") or ""
            cross_check_results.append(cc)
            if cc.get("has_mismatch"):
                any_mismatch = True
        except Exception as e:
            # CrossCheck 실패 — UI fail TC 는 mismatch 신호 보존, 그 외는 False
            cross_check_results.append({
                "tc_id": tc_id, "match_score": 0.0, "matched_fields": 0,
                "mismatched_fields": 0, "mismatches": [],
                "has_mismatch": tc_id in ui_failed_tc_ids,
                "ui_failed": tc_id in ui_failed_tc_ids,
                "db_unverified": tc_id in db_unverified_tc_ids,
                "api_unverified": tc_id in api_unverified_tc_ids,
                "error_code": "",
                "summary": "",
                "error": f"CrossCheck skip: {type(e).__name__}: {e}",
            })

    # status 도출: has_mismatch → "fail", 검증 부재 → "unverified", 정상 → "pass".
    # "unverified" 분리는 e2e false PASS 차단의 본질 fix — DB env 부재 또는 API trace
    # capture 실패 시 묵시 PASS 처리 차단. 사용자가 리포트에서 명시 인식 → 환경 fix.
    def _derive_cc_status(cc: dict) -> str:
        if cc.get("has_mismatch"):
            return "fail"
        if cc.get("db_unverified") or cc.get("api_unverified"):
            return "unverified"
        return "pass"

    # tc_id ("TS-001-TC-05") → ts_id ("TS-001") 도출. upsert_tc_result 는
    # ts_id 빈 값 시 skip — 격차: cross_check kind tc_results 가 DB 에 0건 저장됐던 원인.
    # e2e trace `40fce3fa` 확인 (cross_check row 0건). ui/api/db kind 는 _mirror 에서
    # ts_id 명시 전달이라 정상이지만, cross_check 는 별도 노드라 본 도출 필요.
    def _derive_ts_id(tc_id_str: str) -> str:
        parts = (tc_id_str or "").split("-TC-")
        return parts[0] if len(parts) >= 2 else ""

    for cc in cross_check_results:
        tc_id_str = cc.get("tc_id", "")
        upsert_tc_result(
            run_id=trace_id,
            ts_id=_derive_ts_id(tc_id_str),
            tc_id=tc_id_str,
            kind="cross_check",
            payload=cc,
            status=_derive_cc_status(cc),
        )
        

    return {
        "cross_check_results": cross_check_results,
        "has_mismatch": any_mismatch,
        "agent_logs": agent_logs,
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
    # agent_logs 누적 append — Layer 3 cost 집계 (#232).
    agent_logs = state.get("agent_logs", [])

    for cc in cross_check_results:
        if not cc.get("has_mismatch"):
            continue
        tc_id = cc.get("tc_id", "unknown")

        try:
            agent = RootCauseAgent(trace_id=trace_id)
            output = await agent.run(
                AgentInput(
                    trace_id=trace_id,
                    context={
                        # SaaS 흐름에서 RootCauseAgent 가 codebase-index 로드하려면
                        # state.qapilot_dir 가 필요. cfg.project.repo_path 는 None →
                        # fallback Path(".") = qapilot 디렉토리에서 .qapilot/codebase-index
                        # 찾기 실패 → `codebase_index_empty` warning. 본 fix.
                        # service_id (#245, #248): state.service_id 가 LangGraph state
                        # propagation 에서 누락되는 격차 (PipelineState schema 추가 +
                        # load_trace fallback). 다른 노드 (line 1254/1548) 와 동일 패턴.
                        "qapilot_dir": state.get("qapilot_dir"),
                        "service_id": state.get("service_id")
                            or (load_trace(state["trace_id"]) or {}).get("service_id"),
                    },
                    params={
                        "tc_id": tc_id,
                        "error_code": cc.get("error_code") or "",
                        "summary": cc.get("summary") or "",
                        "mismatches": cc.get("mismatches") or [],
                        "has_mismatch": True,
                    },
                )
            )
            agent_logs = agent_logs + [output.metadata.model_dump()]
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

    return {"root_cause_results": root_cause_results, "agent_logs": agent_logs}


async def _fix_recommend(state: PipelineState) -> dict:
    """Layer 3 세 번째 노드 — FixRecommenderAgent 호출 (FR-011).

    RootCauseResult.candidates 를 받아 해결 가이드 생성.
    """
    from qapilot.agents.fix_recommender_agent import FixRecommenderAgent
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    root_cause_results = state.get("root_cause_results") or []

    fix_results: list[dict] = []
    # agent_logs 누적 append — Layer 3 cost 집계 (#232).
    agent_logs = state.get("agent_logs", [])

    for rc in root_cause_results:
        tc_id = rc.get("tc_id", "unknown")
        candidates = rc.get("candidates") or []

        try:
            agent = FixRecommenderAgent(trace_id=trace_id)
            output = await agent.run(
                AgentInput(
                    trace_id=trace_id,
                    context={
                        # FixRecommender 가 codebase-index fallback 사용 시 필요 (#244, #248)
                        "qapilot_dir": state.get("qapilot_dir"),
                        "service_id": state.get("service_id")
                            or (load_trace(state["trace_id"]) or {}).get("service_id"),
                    },
                    params={
                        "tc_id": tc_id,
                        "candidates": candidates,
                    },
                )
            )
            agent_logs = agent_logs + [output.metadata.model_dump()]
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

    return {"fix_results": fix_results, "agent_logs": agent_logs}


async def _report(state: PipelineState) -> dict:
    """Layer 2/3 마지막 노드 — 임시 markdown summary (Report Tool 본체 stub 상태).

    spec §6.1 의 .qapilot/reports/{trace_id}.md 저장. FR-012:
    - 실행 요약
    - 실패 케이스 (step별 에러, TOOL_UI_* prefix)
    - Cross-check 요약
    - 원인 분석 (FR-010, Top-N candidates)
    - 해결 방안 (FR-011, suggestions)

    ReportTool 본체 머지 후 별도 PR 에서 호출로 교체.
    """
    trace_id = state["trace_id"]
    ui_results = state.get("ui_results") or []
    cross_check_results = state.get("cross_check_results") or []
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

    if root_cause_results:
        lines.extend(["", "## 3. 원인 분석 (FR-010)"])
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
        lines.extend(["", "## 4. 해결 방안 (FR-011)"])
        for fr in fix_results:
            tc_id = fr.get("tc_id")
            suggestions = fr.get("suggestions") or []
            lines.append(f"### `{tc_id}` — 제안 {len(suggestions)}개")
            for s in suggestions[:3]:
                lines.append(
                    f"- `{s.get('file_path', 'N/A')}:{s.get('line_number', '?')}` — "
                    f"{s.get('description', '')[:160]}"
                )

    reports_dir = _qapilot_path(state, "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{trace_id}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # 결함 영속화 — root_cause + fix 결과를 defects 테이블에 INSERT.
    # 실패는 silent (리포트 흐름 보존).
    try:
        from qapilot.db.defect_writer import insert_defects

        # PR #237 #243 후속 (#247): load_trace signature 정합 — 1 arg. 본인 PR #237 가
        # line 908 만 fix 했고 본 위치 누락 → `defects_persist_failed` 잔존 (trace `531ce56a`).
        trace = load_trace(trace_id) or {}
        service_id = trace.get("service_id") or state.get("service_id")
        if service_id:
            # Ensure we pass plain dicts (not model instances) to insert_defects to satisfy type expectations.
            cc_plain: list[dict] = []
            for c in (cross_check_results or []):
                cc_plain.append(_ensure_payload_dict(c))
            rc_plain: list[dict] = []
            for rc in (root_cause_results or []):
                rc_plain.append(_ensure_payload_dict(rc))
            fr_plain: list[dict] = []
            for fr in (fix_results or []):
                fr_plain.append(_ensure_payload_dict(fr))
            inserted = insert_defects(
                service_id=service_id,
                run_id=trace_id,
                cross_check_results=cc_plain,
                root_cause_results=rc_plain,
                fix_results=fr_plain,
            )
            get_logger(source="orchestrator", trace_id=trace_id).info(
                "defects_persisted", count=inserted
            )
    except Exception as e:
        get_logger(source="orchestrator", trace_id=trace_id).warning(
            "defects_persist_failed", error=str(e)
        )

    return {
        "report_path": str(report_path),
        "status": "completed",
    }


# ═══════════════════════════════════════════════════════════════════
# prd_only_experiment 노드 구현
# ═══════════════════════════════════════════════════════════════════


async def _ts_generate_prd_only(state: PipelineState) -> dict:
    """PRD 요구사항만으로 TS 구조 생성 + S3 저장."""
    import time
    from datetime import datetime, timezone

    from qapilot.agents.ts_prd_only_agent import TSFromPRDAgent
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    _all_reqs = state.get("requirements") or []
    # E2E TS는 기능 요구사항만 대상 — 비기능(성능/보안/운영/호환성)은 TS 생성 제외
    requirements = [r for r in _all_reqs if r.get("req_type") == "functional"]
    if len(requirements) < len(_all_reqs):
        logger.info("ts_filter_non_functional", trace_id=trace_id,
                    total=len(_all_reqs), functional=len(requirements),
                    dropped=len(_all_reqs) - len(requirements))

    start = time.monotonic()
    agent = TSFromPRDAgent(trace_id=trace_id)
    output = await agent.run(AgentInput(
        trace_id=trace_id,
        context={"requirements": requirements},
        params={},
    ))
    duration = round(time.monotonic() - start, 2)
    ts_list = output.result.get("ts_list") or []

    trace = load_trace(trace_id) or {}
    service_id = trace.get("service_id")
    if service_id:
        payload = {
            "trace_id": trace_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "prd_only",
            "ts_list": ts_list,
            "metrics": {
                "model": output.metadata.model,
                "cost_usd": output.metadata.cost_usd,
                "duration_sec": duration,
            },
        }
        key = f"services/{service_id}/scenario-runs/{trace_id}/ts_generation.json"
        s3_client.put_bytes(
            key,
            json.dumps(payload, ensure_ascii=False, indent=2).encode(),
            "application/json",
        )

    agent_logs = list(state.get("agent_logs") or []) + [output.metadata.model_dump()]
    return {"ts_list": ts_list, "agent_logs": agent_logs}


async def _tc_generate_doc_search(state: PipelineState) -> dict:
    """ts_list 중 대상 TS만 문서 검색 → TC 생성 + S3 저장."""
    import time
    from datetime import datetime, timezone

    from qapilot.agents.tc_doc_search_agent import TCFromDocsAgent
    from qapilot.shared.schemas import AgentInput, ToolInput
    from qapilot.tools.domain_knowledge import DomainKnowledgeTool

    ts_list = state.get("ts_list") or []
    run_opts = state["run_options"]
    trace_id = state["trace_id"]
    trace = load_trace(trace_id) or {}
    service_id = trace.get("service_id")

    # tc_target_ts_ids 의미 구분 (PR #278 부터):
    # - None / 미주입 → 기존 default (처음 2 TS만 — CLI / dev 디버깅 용)
    # - [] 빈 리스트 → 전체 TS 처리 (SaaS UI 흐름 — PR #277 자동 진입에서 채움)
    # - [TS-x, ...] 명시 리스트 → 지정 TS만 (디버깅)
    raw_targets = run_opts.get("tc_target_ts_ids")
    if raw_targets is None:
        target_indices = list(range(min(2, len(ts_list))))
    elif len(raw_targets) == 0:
        target_indices = list(range(len(ts_list)))  # 전체 TS
    else:
        target_indices = []
        for t in raw_targets:
            try:
                idx = int(str(t).replace("TS-", "")) - 1
                if 0 <= idx < len(ts_list):
                    target_indices.append(idx)
            except ValueError:
                pass

    # req_id → content 매핑 (검색 쿼리 품질 향상)
    req_content_map: dict[str, str] = {}
    for r in (state.get("requirements") or []):
        if isinstance(r, dict):
            req_content_map[r.get("req_id", "")] = r.get("content", "")

    agent_logs = list(state.get("agent_logs") or [])
    result_test_cases: dict[int, list] = {}
    result_docs: dict[int, list] = {}  # 통합 TC 단계에서 재사용할 TS별 검색 문서

    for idx in target_indices:
        ts_item = ts_list[idx]
        ts_name = ts_item.get("name", "")
        domain_area = ts_item.get("domain_area", "")
        req_ids = ts_item.get("requirements") or []
        req_contents = " ".join(req_content_map.get(rid, rid) for rid in req_ids)
        query = f"{ts_name} {domain_area} {req_contents}".strip()

        tool = DomainKnowledgeTool(trace_id=trace_id)
        try:
            search_result = await tool.run(ToolInput(
                trace_id=trace_id,
                params={
                    "action": "search",
                    "query": query,
                    "top_k": 5,
                    "service_id": service_id or "",
                    "score_threshold": 0.55,
                },
            ))
            raw_rules = search_result.result.get("rules") or []
        except Exception:
            raw_rules = []

        retrieved_docs = [
            {
                "content": r.get("content", "") if isinstance(r, dict) else "",
                "source": r.get("source", "") if isinstance(r, dict) else "",
                "score": r.get("similarity_score", r.get("score", 0.0)) if isinstance(r, dict) else 0.0,
            }
            for r in raw_rules
        ]
        result_docs[idx] = retrieved_docs

        start = time.monotonic()
        agent = TCFromDocsAgent(trace_id=trace_id)
        output = await agent.run(AgentInput(
            trace_id=trace_id,
            context={"ts_item": ts_item, "retrieved_docs": retrieved_docs},
            params={},
        ))
        duration = round(time.monotonic() - start, 2)
        test_cases = output.result.get("test_cases") or []
        analysis = output.result.get("analysis") or []
        result_test_cases[idx] = test_cases

        if service_id:
            provisional_ts_id = f"TS-{idx + 1:03d}"
            payload = {
                "trace_id": trace_id,
                "ts_id": provisional_ts_id,
                "ts_name": ts_name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "doc_search_enumeration",
                "analysis": analysis,
                "test_cases": test_cases,
                "retrieved_docs": retrieved_docs,
                "metrics": {
                    "model": output.metadata.model,
                    "cost_usd": output.metadata.cost_usd,
                    "duration_sec": duration,
                    "doc_search_query": query,
                    "doc_search_top_k": 5,
                    "doc_search_results_count": len(retrieved_docs),
                },
            }
            key = f"services/{service_id}/scenario-runs/{trace_id}/tc_generation/{provisional_ts_id}.json"
            s3_client.put_bytes(
                key,
                json.dumps(payload, ensure_ascii=False, indent=2).encode(),
                "application/json",
            )

        agent_logs.append(output.metadata.model_dump())

    return {
        "tc_by_ts_index": result_test_cases,
        "docs_by_ts_index": result_docs,
        "agent_logs": agent_logs,
    }


async def _tv_generate_codebase_aware(state: PipelineState) -> dict:
    """TC 의 values 칸을 코드베이스/메타데이터/DB 기반으로 채운다.

    회의 4단계 워크플로우 Step 4. TC 자체는 변경 안 하고 values 만 채움.

    각 TC 마다:
    1. TC.api / req_id 로 4영역 메타데이터 필터링 (`metadata_filters`)
    2. sensitive 필드 LLM 컨텍스트 제외 (`sensitive_mask` — PR #256 본질 재발 방지)
    3. (선택) DB snapshot 일부 LLM 에 주입
    4. TVFromCodebaseAgent 호출 → TVValidator 검증 → 재시도
    5. sensitive 필드는 placeholder (process.env.TEST_PASSWORD) 머지

    조회 실패 / 메타데이터 미존재 시 graceful — TC.values 그대로 보존.
    """
    import time
    from datetime import datetime, timezone

    from qapilot.agents.tv_codebase_aware_agent import TVFromCodebaseAgent
    from qapilot.shared.db_state import get_db_snapshot_cached
    from qapilot.shared.metadata_filters import (
        find_endpoint_function_range,
        pick_source_files_for_tc,
        pick_table_for_tc,
    )
    from qapilot.shared.scan_storage import load_metadata_index, load_source
    from qapilot.shared.schemas import AgentInput

    trace_id = state["trace_id"]
    trace = load_trace(trace_id) or {}
    service_id = trace.get("service_id")
    tc_by_index: dict = state.get("tc_by_ts_index") or {}
    docs_by_index: dict = state.get("docs_by_ts_index") or {}
    if not tc_by_index:
        return {}

    # ── 4영역 메타데이터 로드 (service_id 있을 때만, LRU cache) ──
    selectors = routes = schemas = patterns = None
    if service_id:
        try:
            selectors = load_metadata_index(service_id, "frontend", "selectors")
            routes = load_metadata_index(service_id, "frontend", "routes")
            schemas = load_metadata_index(service_id, "backend", "schemas")
            patterns = load_metadata_index(service_id, "sut_tests", "patterns")
        except Exception as e:
            logger.warning("tv_metadata_load_failed", trace_id=trace_id, error=str(e))
            selectors = routes = schemas = patterns = None

    if not any([selectors, routes, schemas, patterns]):
        # 정합성 설계: 메타데이터가 없어도 중단하지 않는다 — given/when/then 은
        # 검색 문서 기반으로 생성해야 한다 (value 만 약해짐).
        logger.info(
            "tv_metadata_index_empty",
            trace_id=trace_id, service_id=service_id,
            reason="codebase_scan 미수행/빈 상태 — 문서 기반 gwt 만 생성, value 약화 가능",
        )

    agent_logs = list(state.get("agent_logs") or [])
    updated_tc_by_index: dict = {}

    # ── 현재 SHA — load_source 호출용 (codebase_scan_exp 에서 적재된 source/{sha}) ──
    scan_meta = state.get("scan_result") or {}
    commit_sha = (scan_meta.get("git_diff") or {}).get("commit_hash") or ""
    if not commit_sha:
        # codebase_scan_exp 가 안 돈 경우 — load_source 건너뜀, 다른 단계는 계속
        logger.info("tv_no_commit_sha", trace_id=trace_id,
                    reason="codebase_scan 결과에 git_diff.commit_hash 없음")

    for idx, tcs in tc_by_index.items():
        updated_tcs: list[dict] = []
        # 이 TS 의 검색 문서 — 통합 단계에서 given/when/then 의 근거로 재사용
        retrieved_docs = docs_by_index.get(idx) or docs_by_index.get(str(idx)) or []
        for tc in tcs:
            # schemas.db_models 기반으로 TC 와 가장 관련 깊은 테이블 1개 선택 + DB snapshot 조회
            # (sensitive 컬럼은 TV agent 안에서 strip_sensitive_from_db_snapshot 으로 제거)
            db_snapshot = None
            table_name = pick_table_for_tc(tc, schemas)
            if table_name:
                try:
                    db_snapshot = await get_db_snapshot_cached(service_id, table_name)
                    if db_snapshot:
                        rows = (db_snapshot.get("rows") or [])
                        logger.info(
                            "tv_db_snapshot_loaded",
                            trace_id=trace_id, tc_name=tc.get("name"),
                            table=table_name, row_count=len(rows),
                        )
                except Exception as e:
                    logger.warning(
                        "tv_db_snapshot_failed",
                        trace_id=trace_id, tc_name=tc.get("name"),
                        table=table_name, error=str(e),
                    )

            # ── 코드베이스 본문 (production 코드) — TC 관련 파일 load_source ──
            # router 파일은 endpoint 함수만 추출 (전체 60% 가 무관한 endpoint 노이즈 회피)
            source_snippets: list[dict] = []
            if commit_sha:
                source_targets = pick_source_files_for_tc(tc, schemas=schemas)
                for file_path, line_start, line_end in source_targets:
                    try:
                        content = load_source(
                            service_id, commit_sha, file_path,
                            line_start=line_start, line_end=line_end,
                        )
                    except Exception as e:
                        logger.warning(
                            "tv_load_source_failed",
                            trace_id=trace_id, tc_name=tc.get("name"),
                            file=file_path, error=str(e),
                        )
                        continue
                    if not content:
                        continue

                    # router 파일은 endpoint 함수만 추출 (정보 희석 회피)
                    is_router_file = (
                        "/routers/" in file_path
                        and line_start is None and line_end is None
                    )
                    if is_router_file:
                        endpoint_range = find_endpoint_function_range(content, tc.get("api"))
                        if endpoint_range:
                            ep_ls, ep_le = endpoint_range
                            # line range 만 잘라내기 (cache hit — S3 GET 안 함)
                            sliced = load_source(
                                service_id, commit_sha, file_path,
                                line_start=ep_ls, line_end=ep_le,
                            )
                            if sliced:
                                line_start, line_end = ep_ls, ep_le
                                content = sliced
                                logger.info(
                                    "tv_endpoint_extracted",
                                    trace_id=trace_id, tc_name=tc.get("name"),
                                    file=file_path, line_range=f"{ep_ls}-{ep_le}",
                                    original_chars=len(content), endpoint_chars=len(sliced),
                                )

                    source_snippets.append({
                        "file": file_path,
                        "line_start": line_start,
                        "line_end": line_end,
                        "content": content,
                    })
                if source_snippets:
                    logger.info(
                        "tv_source_loaded",
                        trace_id=trace_id, tc_name=tc.get("name"),
                        files=[s["file"] for s in source_snippets],
                    )

            start = time.monotonic()
            try:
                agent = TVFromCodebaseAgent(trace_id=trace_id)
                output = await agent.run(AgentInput(
                    trace_id=trace_id,
                    context={
                        "tc": tc,
                        "retrieved_docs": retrieved_docs,
                        "selectors": selectors,
                        "routes": routes,
                        "schemas": schemas,
                        "patterns": patterns,
                        "db_snapshot": db_snapshot,
                        "source_snippets": source_snippets,
                    },
                    params={},
                ))
                duration = round(time.monotonic() - start, 2)
                new_values = output.result.get("values") or []
                validation_passed = output.result.get("validation_passed", False)

                # 정합성: given/when/then 과 value 를 함께 생성 → TC 골격에 병합
                new_tc = dict(tc)
                for fld in ("given", "when", "then"):
                    val = output.result.get(fld)
                    if val:
                        new_tc[fld] = val
                if new_values:
                    new_tc["values"] = new_values
                new_tc["tv_validation_passed"] = validation_passed
                new_tc["tv_validation_reasons"] = (
                    output.result.get("validation_reasons") or []
                )
                updated_tcs.append(new_tc)

                agent_logs.append(output.metadata.model_dump())
                logger.info(
                    "tv_codebase_aware_done",
                    trace_id=trace_id,
                    tc_name=tc.get("name"),
                    gwt_generated=bool(output.result.get("when")),
                    values_count=len(new_values),
                    validation_passed=validation_passed,
                    duration_sec=duration,
                )
            except Exception as e:
                logger.warning(
                    "tv_codebase_aware_failed",
                    trace_id=trace_id,
                    tc_name=tc.get("name"),
                    error=str(e),
                )
                updated_tcs.append(tc)  # graceful — 원본 보존

        updated_tc_by_index[idx] = updated_tcs

    return {"tc_by_ts_index": updated_tc_by_index, "agent_logs": agent_logs}


async def _save_experiment_scenarios(state: PipelineState) -> dict:
    """ts_list + tc_by_ts_index를 병합해 최종 시나리오 파일로 디스크·DB 저장."""
    ts_list = state.get("ts_list") or []
    tc_by_index: dict = state.get("tc_by_ts_index") or {}
    trace = load_trace(state["trace_id"]) or {}
    service_id = trace.get("service_id")
    trigger = state["run_options"].get("trigger") or "init"

    scenarios_dir = _qapilot_path(state, "scenarios")
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    merged_scenarios: list[dict] = []

    for i, ts_item in enumerate(ts_list):
        ts_id = f"TS-{i + 1:03d}"
        test_cases = []
        for j, tc in enumerate(tc_by_index.get(i, []), start=1):
            tc["tc_id"] = f"{ts_id}-TC-{j:02d}"
            test_cases.append(tc)

        ts = {
            "ts_id": ts_id,
            "name": ts_item.get("name", ""),
            "description": ts_item.get("description", ""),
            "trigger": trigger,
            "affected_files": [],
            "domain_rules_used": [],
            "requirements": ts_item.get("requirements") or [],
            "depends_on": [],
            "test_cases": test_cases,
        }
        path = scenarios_dir / f"{ts_id}.json"
        path.write_text(json.dumps(ts, ensure_ascii=False, indent=2), encoding="utf-8")
        saved_paths.append(str(path))
        merged_scenarios.append(ts)
        if service_id:
            upsert_scenario_version(service_id, ts_id, ts)

    return {
        "scenarios": merged_scenarios,
        "saved_scenario_paths": saved_paths,
        "status": "completed",
    }
