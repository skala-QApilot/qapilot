"""4영역 통합 스캔 orchestrator — PoC 10 (데이터 layer).

`scan_all_metadata(service_id, repo_root, commit_sha)` — 한 호출에 4영역 모두:
1. dump_source_to_s3            (source/ 본문)
2. frontend.selectors            (Vue SFC AST)
3. frontend.routes               (Vue Router AST)
4. backend.schemas               (Pydantic + SQLAlchemy AST)
5. sut_tests.patterns            (pytest AST + LLM 보강)

caller: 유빈 agent register flow, CLI `qapilot scan <service>`, service register webhook 등.

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qapilot.db.metadata_writer import upsert_metadata_index
from qapilot.scan.extractors.backend_schema_parser import (
    extract_backend_schemas_from_file,
)
from qapilot.scan.extractors.llm_pattern_classifier import classify_unknown_patterns
from qapilot.scan.extractors.pytest_ast_parser import extract_patterns_from_pytest_file
from qapilot.scan.extractors.vue_router_parser import extract_routes_from_router_file
from qapilot.scan.extractors.vue_sfc_parser import (
    VueSfcParseError,
    extract_selectors_from_vue,
)
from qapilot.scan.source_dumper import (
    DEFAULT_EXCLUDE_DIRS,
    SourceDumpResult,
    dump_source_to_s3,
    iter_source_files,
)
from qapilot.shared.logger import get_logger
from qapilot.shared.metadata_schemas import (
    BackendSchemasIndex,
    ButtonElement,
    DynamicElement,
    FrontendRoutesIndex,
    FrontendSelectorsIndex,
    InputElement,
    OutputElement,
    RouteSelectors,
    SutTestsPatternsIndex,
)

_logger = get_logger("scan.orchestrator")


@dataclass
class ScanAllResult:
    """scan_all_metadata 의 종합 결과."""

    service_id: str
    commit_sha: str
    source_dump: SourceDumpResult | None = None

    # 4영역 record 수
    selectors_count: int = 0
    routes_count: int = 0
    schemas_request_count: int = 0
    schemas_response_count: int = 0
    db_models_count: int = 0
    patterns_count: int = 0
    patterns_classified: int = 0  # LLM 보강된 record 수

    # upsert 성공 여부
    selectors_upserted: bool = False
    routes_upserted: bool = False
    schemas_upserted: bool = False
    patterns_upserted: bool = False

    # 에러 누적
    errors: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────

async def scan_all_metadata(
    service_id: str,
    repo_root: Path,
    commit_sha: str,
    *,
    skip_source_dump: bool = False,
    skip_llm_classification: bool = False,
    llm_client: Any = None,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
) -> ScanAllResult:
    """4영역 + source 모두 추출 + S3/DB 저장.

    Args:
        service_id, commit_sha: S3 path 구성 + DB 매칭.
        repo_root: 절대 경로 (git clone 결과 또는 local).
        skip_source_dump: True 면 source/ 본문 PUT 생략 (메타데이터만).
        skip_llm_classification: True 면 PoC 5.1 LLM 보강 skip (sut_tests.patterns 의 unknown 유지).
        llm_client: LLMClient instance (None + skip_llm=False 면 의미 분류 skip + warn).
        exclude_dirs: 스캔 제외 디렉토리.

    Returns:
        ScanAllResult — 4영역 각 record 수 + upsert 성공/실패 + 에러.

    graceful:
        - 영역별 실패는 result.errors 에 누적, 다른 영역 계속 진행
        - 전체 raise 없음
    """
    result = ScanAllResult(service_id=service_id, commit_sha=commit_sha)
    root = repo_root.resolve()

    # ── 1. source dump ──────────────────────────────────────────────
    if not skip_source_dump:
        try:
            result.source_dump = dump_source_to_s3(
                service_id, root, commit_sha, exclude_dirs=exclude_dirs,
            )
        except Exception as e:
            result.errors.append(f"source_dump: {e}")
            _logger.warning("scan_source_dump_failed", error=str(e))

    # ── 2. frontend.selectors (Vue) ─────────────────────────────────
    try:
        idx = _build_frontend_selectors(root, commit_sha, service_id, exclude_dirs)
        if idx is not None:
            result.selectors_count = sum(
                len(rs.inputs) + len(rs.buttons) + len(rs.outputs) + len(rs.dynamic)
                for rs in idx.by_route.values()
            )
            ok = upsert_metadata_index(
                idx, service_id=service_id, commit_hash=commit_sha,
                file_count=len(idx.by_route),
                confidence=1.0, extraction_method="ast",
            )
            result.selectors_upserted = ok
    except Exception as e:
        result.errors.append(f"selectors: {e}")
        _logger.warning("scan_selectors_failed", error=str(e))

    # ── 3. frontend.routes (Vue Router) ─────────────────────────────
    try:
        idx = _build_frontend_routes(root, commit_sha, service_id, exclude_dirs)
        if idx is not None:
            result.routes_count = len(idx.routes)
            ok = upsert_metadata_index(
                idx, service_id=service_id, commit_hash=commit_sha,
                file_count=1, confidence=1.0, extraction_method="ast",
            )
            result.routes_upserted = ok
    except Exception as e:
        result.errors.append(f"routes: {e}")
        _logger.warning("scan_routes_failed", error=str(e))

    # ── 4. backend.schemas (Pydantic + SQLAlchemy) ──────────────────
    try:
        idx, file_count = _build_backend_schemas(root, commit_sha, service_id, exclude_dirs)
        if idx is not None:
            result.schemas_request_count = len(idx.request_schemas)
            result.schemas_response_count = len(idx.response_schemas)
            result.db_models_count = len(idx.db_models)
            ok = upsert_metadata_index(
                idx, service_id=service_id, commit_hash=commit_sha,
                file_count=file_count, confidence=1.0, extraction_method="ast",
            )
            result.schemas_upserted = ok
    except Exception as e:
        result.errors.append(f"schemas: {e}")
        _logger.warning("scan_schemas_failed", error=str(e))

    # ── 5. sut_tests.patterns (pytest + LLM 보강) ───────────────────
    try:
        idx, file_count = await _build_sut_tests_patterns(
            root, commit_sha, service_id, exclude_dirs,
            llm_client=None if skip_llm_classification else llm_client,
        )
        if idx is not None:
            result.patterns_count = len(idx.patterns)
            result.patterns_classified = sum(
                1 for p in idx.patterns if p.extraction_method == "hybrid"
            )
            extraction = "hybrid" if result.patterns_classified > 0 else "ast"
            ok = upsert_metadata_index(
                idx, service_id=service_id, commit_hash=commit_sha,
                file_count=file_count, confidence=1.0, extraction_method=extraction,
            )
            result.patterns_upserted = ok
    except Exception as e:
        result.errors.append(f"patterns: {e}")
        _logger.warning("scan_patterns_failed", error=str(e))

    _logger.info(
        "scan_all_done",
        service_id=service_id,
        commit_sha=commit_sha[:12],
        selectors=result.selectors_count,
        routes=result.routes_count,
        schemas=result.schemas_request_count + result.schemas_response_count,
        db_models=result.db_models_count,
        patterns=result.patterns_count,
        patterns_classified=result.patterns_classified,
        errors=len(result.errors),
    )
    return result


# ────────────────────────────────────────────────────────────────────────
# 영역별 builder (내부)
# ────────────────────────────────────────────────────────────────────────

def _build_frontend_selectors(
    root: Path, commit_sha: str, service_id: str,
    exclude_dirs: frozenset[str],
) -> FrontendSelectorsIndex | None:
    """모든 .vue 파일 → FrontendSelectorsIndex (by_route)."""
    by_route: dict[str, RouteSelectors] = {}
    for vue_file in iter_source_files(root, whitelist=(".vue",), exclude_dirs=exclude_dirs):
        try:
            elements = extract_selectors_from_vue(
                vue_file, repo_root=root, commit_sha=commit_sha,
            )
        except VueSfcParseError as e:
            _logger.warning("vue_sfc_parse_skipped", file=str(vue_file), error=str(e))
            continue
        if not elements:
            continue
        # route 추론 — 파일 경로에서 `/pages/Signup.vue` → `/signup` (소문자), 기타 default
        route = _infer_route_from_path(vue_file, root)
        rs = by_route.setdefault(route, RouteSelectors())
        for el in elements:
            if isinstance(el, InputElement):
                rs.inputs.append(el)
            elif isinstance(el, ButtonElement):
                rs.buttons.append(el)
            elif isinstance(el, OutputElement):
                rs.outputs.append(el)
            elif isinstance(el, DynamicElement):
                rs.dynamic.append(el)
    if not by_route:
        return None
    return FrontendSelectorsIndex(
        service_id=service_id, commit_sha=commit_sha, by_route=by_route,
    )


def _infer_route_from_path(vue_file: Path, repo_root: Path) -> str:
    """`frontend/src/pages/Signup.vue` → `/signup`. fallback = 파일 stem."""
    rel = vue_file.relative_to(repo_root)
    parts = rel.parts
    # pages/ 하위의 .vue 만 — 그 외는 component 로 간주, default route
    if "pages" in parts:
        idx = parts.index("pages")
        if idx + 1 < len(parts):
            page_name = parts[-1].replace(".vue", "").lower()
            return f"/{page_name}"
    return f"/_components/{vue_file.stem.lower()}"


def _build_frontend_routes(
    root: Path, commit_sha: str, service_id: str,
    exclude_dirs: frozenset[str],
) -> FrontendRoutesIndex | None:
    """router/index.{js,ts} 검색 → FrontendRoutesIndex."""
    routes = []
    for f in iter_source_files(root, whitelist=(".js", ".ts"), exclude_dirs=exclude_dirs):
        # router 디렉토리 안 또는 router 라는 이름의 파일 우선
        if "router" not in f.parts and f.stem != "router":
            continue
        recs = extract_routes_from_router_file(f, repo_root=root, commit_sha=commit_sha)
        routes.extend(recs)
    if not routes:
        return None
    return FrontendRoutesIndex(
        service_id=service_id, commit_sha=commit_sha, routes=routes,
    )


def _build_backend_schemas(
    root: Path, commit_sha: str, service_id: str,
    exclude_dirs: frozenset[str],
) -> tuple[BackendSchemasIndex | None, int]:
    """모든 backend .py → BackendSchemasIndex. 단 tests/ 는 제외 (PoC 5 가 별도 처리)."""
    all_req: dict = {}
    all_resp: dict = {}
    all_dbm: dict = {}
    test_skip = exclude_dirs | {"tests", "test"}
    file_count = 0
    for f in iter_source_files(root, whitelist=(".py",), exclude_dirs=test_skip):
        # test_*.py 제외 (pytest_ast_parser 가 처리)
        if f.stem.startswith("test_") or f.stem.endswith("_test"):
            continue
        req, resp, dbm = extract_backend_schemas_from_file(
            f, repo_root=root, commit_sha=commit_sha,
        )
        if req or resp or dbm:
            file_count += 1
            all_req.update(req)
            all_resp.update(resp)
            all_dbm.update(dbm)
    if not (all_req or all_resp or all_dbm):
        return None, 0
    return BackendSchemasIndex(
        service_id=service_id, commit_sha=commit_sha,
        request_schemas=all_req, response_schemas=all_resp, db_models=all_dbm,
    ), file_count


async def _build_sut_tests_patterns(
    root: Path, commit_sha: str, service_id: str,
    exclude_dirs: frozenset[str],
    *, llm_client: Any | None,
) -> tuple[SutTestsPatternsIndex | None, int]:
    """모든 pytest test_*.py / conftest.py → SutTestsPatternsIndex (+ LLM 보강)."""
    patterns: list = []
    file_count = 0
    for f in iter_source_files(root, whitelist=(".py",), exclude_dirs=exclude_dirs):
        is_test_file = (
            f.stem.startswith("test_")
            or f.stem.endswith("_test")
            or f.name == "conftest.py"
        )
        if not is_test_file:
            continue
        recs = extract_patterns_from_pytest_file(
            f, repo_root=root, commit_sha=commit_sha,
        )
        if recs:
            file_count += 1
            patterns.extend(recs)
    if not patterns:
        return None, 0

    # LLM 보강 — unknown 의미 분류 (llm_client 가 None 이면 skip)
    if llm_client is not None:
        try:
            patterns = await classify_unknown_patterns(patterns, llm_client=llm_client)
        except Exception as e:
            _logger.warning("llm_classification_failed", error=str(e))

    return SutTestsPatternsIndex(
        service_id=service_id, commit_sha=commit_sha, patterns=patterns,
    ), file_count
