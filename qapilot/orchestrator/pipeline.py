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
from qapilot.db.scenario_writer import upsert_scenario_version
from qapilot.db.tc_result_writer import insert_tc_artifact, upsert_tc_result
from qapilot.orchestrator.state import PipelineState
from qapilot.shared import progress
from qapilot.shared.logger import get_logger
from qapilot.shared.trace_store import load_trace
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
        progress.node(state.get("trace_id"), name)
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
    1) qapilot.config.yaml 의 project.root / repo_path
    2) state.qapilot_dir 가 `<root>/.qapilot[/service]` 형태일 때 `<root>`
    """
    from qapilot.shared.config import load_config

    cfg = load_config()
    for raw in (cfg.project.root, cfg.project.repo_path):
        if raw:
            path = Path(raw).expanduser().resolve()
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
    import tempfile

    from qapilot.db.domain_reader import list_latest_domain_documents
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

    async def _import_path(doc_path: Path) -> None:
        index_path = _qapilot_path(state, "domain", f"{doc_path.stem}.index.json")
        if collection_alive and index_path.exists():
            try:
                saved = json.loads(index_path.read_text(encoding="utf-8"))
                if saved.get("file") == str(doc_path):
                    logger.info("doc_import_skip", file=str(doc_path))
                    return
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
                await _import_path(tmp_path)
                imported_from_saas += 1
            finally:
                tmp_path.unlink(missing_ok=True)

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


async def _upsert_scenario_index(service_id: str, ts: dict) -> None:
    """시나리오 저장 후 Qdrant scenario_index를 비동기로 업데이트한다. 실패는 silent."""
    try:
        from qapilot.tools.scenario_index import ScenarioVectorStore
        await ScenarioVectorStore().upsert_scenario(service_id=service_id, ts=ts)
    except Exception as e:
        get_logger(source="orchestrator").warning("scenario_index_upsert_error", error=str(e))


def _build_existing_scenarios_summary(state: PipelineState) -> list[dict]:
    """기존 시나리오를 TS+TC 요약으로 반환한다.

    NaturalLanguageAgent가 target_ts_id / target_tc_id를 정확히 특정할 수 있도록
    ts_id, title, test_cases(tc_id + title)만 추출해 전달한다.
    전체 시나리오 JSON을 넘기면 프롬프트가 비대해지므로 요약본만 사용한다.
    """
    try:
        scenarios = _load_json_files(_qapilot_path(state, "scenarios"))
        summary = []
        for ts in scenarios:
            tc_summaries = [
                {"tc_id": tc.get("tc_id"), "title": tc.get("title") or tc.get("name") or ""}
                for tc in (ts.get("test_cases") or [])
                if tc.get("tc_id")
            ]
            summary.append({
                "ts_id": ts.get("ts_id"),
                "title": ts.get("title") or ts.get("name") or "",
                "test_cases": tc_summaries,
            })
        return summary
    except Exception:
        return []


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
                service_id=service_id,
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

    update_reqs = [r for r in requirements if r.get("action_type") == "update"]
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
            ts_vecs_cache = await _asyncio.to_thread(
                embedder.encode, ts_texts, normalize_embeddings=True
            )
        return ts_vecs_cache

    for i, req in enumerate(update_reqs):
        q_vec = query_vecs[i]

        # ── TS 매칭: Qdrant 우선 ──────────────────────────────────────────
        matched_ts_id: str | None = None
        matched_ts: dict | None = None

        if service_id:
            try:
                from qapilot.tools.scenario_index import ScenarioVectorStore

                hits = await ScenarioVectorStore().search_ts(
                    service_id=service_id,
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

        if matched_ts_id is None:
            # 인메모리 폴백
            ts_vecs = await _get_ts_vecs()
            ts_sims = [float(np.dot(q_vec, ts_vec)) for ts_vec in ts_vecs]
            best_ts_idx = int(np.argmax(ts_sims))
            if ts_sims[best_ts_idx] < threshold:
                continue
            matched_ts = existing_scenarios[best_ts_idx]
            matched_ts_id = matched_ts.get("ts_id")

        if not matched_ts_id or not matched_ts:
            continue

        req["target_ts_id"] = matched_ts_id

        # ── TC/TV 매칭: Qdrant 우선 ──────────────────────────────────────
        if req.get("target_level") in ("tc", "tv"):
            tc_matched: dict | None = None

            if service_id:
                try:
                    from qapilot.tools.scenario_index import ScenarioVectorStore

                    tc_hits = await ScenarioVectorStore().search_tc(
                        service_id=service_id,
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

            if tc_matched is None:
                # 인메모리 폴백
                tcs = matched_ts.get("test_cases") or []
                if tcs:
                    tc_texts_local = [
                        tc.get("title") or tc.get("tc_id", "") for tc in tcs
                    ]
                    tc_vecs = await _asyncio.to_thread(
                        embedder.encode, tc_texts_local, normalize_embeddings=True
                    )
                    tc_sims = [float(np.dot(q_vec, tc_vec)) for tc_vec in tc_vecs]
                    best_tc_idx = int(np.argmax(tc_sims))
                    if tc_sims[best_tc_idx] >= threshold:
                        tc_matched = {
                            "tc_id": tcs[best_tc_idx].get("tc_id"),
                            "_similarity": tc_sims[best_tc_idx],
                        }

            if tc_matched:
                req["target_tc_id"] = tc_matched.get("tc_id")

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
            save_exchange,
        )

        user_input = (state["run_options"].get("user_input") or "").strip()
        session_id = state["run_options"].get("session_id") or ""
        qapilot_dir = state.get("qapilot_dir") or ""

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

        # 3단계: top_candidates 안에서 target_ts_id / target_tc_id 확정 주입 (이슈 #186, #221)
        try:
            requirements = await _resolve_scenario_targets(
                requirements, top_candidates, service_id=_service_id
            )
        except Exception as e:
            get_logger("orchestrator").warning("scenario_target_resolve_failed", error=str(e))

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
    return {
        "requirements": output.result.get("requirements", []) or [],
        "agent_logs": agent_logs,
    }


async def _scenario_generate(state: PipelineState) -> dict:
    """FR-002 ScenarioGeneratorAgent 호출 → TS/TC/TV 시나리오 목록."""
    from qapilot.agents.scenario_generator.agent import ScenarioGeneratorAgent
    from qapilot.shared.schemas import AgentInput

    trigger = state["run_options"].get("trigger") or "code_change"
    affected_only = trigger == "code_change"

    context: dict = {
        "scan_result": state.get("scan_result"),
        "domain_rules": state.get("domain_rules") or [],
        "requirements": state.get("requirements") or [],
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
    return {"scenarios": scenarios, "agent_logs": agent_logs}


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
            "status": "skipped",
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
    def _backfill_req_id(ts_dict: dict, ts_id: str) -> None:
        if requirements:
            return
        for tc in ts_dict.get("test_cases") or []:
            if not tc.get("req_id"):
                tc["req_id"] = f"FR-{ts_id}"

    if trigger == "natural_lang":
        # natural_lang: delta(신규/수정 시나리오)만 저장, 기존 시나리오 파일 유지 (이슈 #180)
        # ScenarioGeneratorAgent가 반환한 시나리오만 쓰고, 나머지는 건드리지 않음.
        for idx, ts in enumerate(scenarios, start=1):
            ts_id = ts.get("ts_id") or f"TS-{idx:03d}"
            _backfill_req_id(ts, ts_id)
            path = scenarios_dir / f"{ts_id}.json"
            path.write_text(
                json.dumps(ts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_paths.append(str(path))
            if service_id:
                upsert_scenario_version(service_id, ts_id, ts)
                await _upsert_scenario_index(service_id, ts)
            logger.info(
                "scenario_merged",
                ts_id=ts_id,
                action_type=ts.get("action_type", "create"),
            )
    else:
        for idx, ts in enumerate(scenarios, start=1):
            ts_id = ts.get("ts_id") or f"TS-{idx:03d}"
            _backfill_req_id(ts, ts_id)
            path = scenarios_dir / f"{ts_id}.json"
            path.write_text(
                json.dumps(ts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_paths.append(str(path))
            if service_id:
                upsert_scenario_version(service_id, ts_id, ts)
                await _upsert_scenario_index(service_id, ts)

    # RTM 버전 자동 생성 — natural_lang delta 저장 시에는 skip (전체 시나리오 기준이 아니므로)
    if trigger != "natural_lang":
        try:
            _write_initial_rtm_version(state)
        except Exception as e:
            # RTM 생성 실패는 본 시나리오 생성 흐름을 막지 않도록 silent.
            logger = get_logger(source="orchestrator", trace_id=state.get("trace_id"))
            logger.warning("rtm_version_write_failed", error=str(e))

    # 첫 마일스톤 v1.0 자동 박제 — init 트리거 + service 의 첫 시나리오 생성일 때.
    # UNIQUE(service_id, label) 제약 덕에 재실행해도 멱등 (이미 v1.0 있으면 skip).
    if trigger == "init" and service_id and scenarios:
        try:
            from qapilot.db.scenario_version_writer import insert_initial_milestone

            created = insert_initial_milestone(service_id, scenarios, label="v1.0",
                                                description="초기 자동 생성")
            logger.info("initial_milestone", created=created, label="v1.0")
        except Exception as e:
            logger.warning("initial_milestone_failed", error=str(e))

    return {
        "saved_scenario_paths": saved_paths,
        "status": "completed",
    }


def _write_initial_rtm_version(state: PipelineState) -> None:
    """state.requirements 와 scenarios 의 TC 들을 매핑해 RTM 버전 JSON 을 디스크에 저장.

    label 은 기존 RTM 버전 수 +1 기준 vN.0 (예: v1.0, v2.0). status/카운트는 빈 채로
    두고 Spring 응답 시점 동적 계산.

    state.requirements 가 비어있으면, scenarios 의 ts_id 별로 placeholder FR 을 만들어
    최소한 RTM 구조는 항상 생성한다 (UI 에 빈 RTM 페이지 보이지 않도록).
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    logger = get_logger(source="orchestrator", trace_id=state.get("trace_id"))
    requirements = state.get("requirements") or []
    scenarios = state.get("scenarios") or []
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
    
    scenario_ids = state["run_options"].get("scenario_ids") or []
    scenarios = []
    service_id = state.get("service_id") or (load_trace(state.get("trace_id", "")) or {}).get("service_id")
    if service_id:
        from qapilot.db.scenario_reader import load_latest_scenarios
        scenarios = load_latest_scenarios(str(service_id), scenario_ids or None)
    if not scenarios:
        scenarios_dir = _qapilot_path(state, "scenarios")
        if scenarios_dir.exists():
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

    code_dir = _qapilot_path(state, "generated-code")
    am_dir = _qapilot_path(state, "action-mappings")
    code_dir.mkdir(parents=True, exist_ok=True)
    am_dir.mkdir(parents=True, exist_ok=True)

    saved_code_paths: list[str] = []
    # PR-17 — DB+S3 dual-write. trace.json 에서 service_id lookup. 없으면 graceful skip.
    trace = load_trace(state["trace_id"]) or {}
    service_id = trace.get("service_id")

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
        path.write_text(json.dumps(am, ensure_ascii=False, indent=2), encoding="utf-8")
        if service_id:
            upsert_action_mapping(service_id, tc_id, am)

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

            for item in execution_items:
                tc_id = item.get("tc_id") or "unknown"
                ts_id = _ts_id_of_tc(tc_id, scenarios)
                tc_dir = results_root / ts_id / tc_id
                screenshots_dir = tc_dir / "screenshots"
                tc_dir.mkdir(parents=True, exist_ok=True)

                if generated_codes:
                    exec_mapping = _action_mapping_from_generated_code(item)
                    # api_endpoint 힌트는 기존 ActionMapping 의 값을 최대한 유지.
                    original = action_mapping_by_tc.get(str(tc_id)) or {}
                    original_steps = list(original.get("steps") or [])
                    for idx, step in enumerate(exec_mapping.get("steps") or []):
                        if idx < len(original_steps):
                            step["api_endpoint"] = original_steps[idx].get("api_endpoint")
                else:
                    exec_mapping = item

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


def _action_mapping_from_generated_code(code_obj: dict[str, Any]) -> dict[str, Any]:
    """생성된 Playwright JS 코드의 표준 패턴을 ActionMapping 으로 복원한다."""
    tc_id = str(code_obj.get("tc_id") or "unknown")
    code = str(code_obj.get("code") or "")
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
            agent_logs = agent_logs + [output.metadata.model_dump()]
            cc = dict(output.result.get("cross_check") or {})
            if not cc:
                cc = {
                    "tc_id": tc_id, "match_score": 0.0, "matched_fields": 0,
                    "mismatched_fields": 0, "mismatches": [], "has_mismatch": False,
                }
            # UI 단계 fail 인 TC 는 Layer 3 진입 위해 has_mismatch 강제 True
            if tc_id in ui_failed_tc_ids:
                cc["has_mismatch"] = True
                cc.setdefault("ui_failed", True)
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
                        # service_id 추가 (#245) — test trace 는 generate_code trace 와
                        # 다른 temp dir 라 디스크 fallback 실패 → DB+S3 mirror 로 복원.
                        "qapilot_dir": state.get("qapilot_dir"),
                        "service_id": state.get("service_id"),
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
                        # FixRecommender 가 codebase-index fallback 사용 시 필요 (#244)
                        "qapilot_dir": state.get("qapilot_dir"),
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
            inserted = insert_defects(
                service_id=service_id,
                run_id=trace_id,
                cross_check_results=cross_check_results,
                root_cause_results=root_cause_results,
                fix_results=fix_results,
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
