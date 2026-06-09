"""pytest AST 추출기 — sut_tests.patterns (PoC 5, 데이터 layer).

추출 대상 (framework 일반, 도메인 무관):
- `def test_*` (또는 `async def test_*`) 함수 → TestPatternRecord
- `@pytest.fixture` 데코레이터가 붙은 함수 → TestPatternRecord(pattern_kind="fixture")

분류 정책 (AST 만 — confidence=1.0):
- 데코레이터에 `pytest.fixture` 또는 `fixture` 존재     → "fixture"
- body 에 `monkeypatch.setattr` 또는 `mock.patch` 호출  → "mock"
- body 에 HTTP request 호출 + URL 에 인증 키워드        → "auth-setup"
   (auth/login/signup/logout/token/session — HTTP 인증 일반, 도메인 무관)
- 그 외                                                 → "unknown"
   (LLM 보강 단계에서 의미 분류 — PoC 5+)

framework 결정:
- `import pytest` 또는 `@pytest.fixture` → "pytest"
- 그 외 → "unknown" (본 추출기는 pytest 가 아니면 빈 list 반환)

본 모듈은 단일 .py 파일 단위. service 전체 walk + SutTestsPatternsIndex 생성은
PoC 6+ caller 가 담당.

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from qapilot.shared.metadata_schemas import (
    ExtractedFrom,
    PatternKind,
    TestFramework,
    TestPatternRecord,
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# 분류용 키워드 — 일반 (도메인 무관)
# ────────────────────────────────────────────────────────────────────────

# HTTP 인증 일반 — 비즈니스 도메인 무관 (요금제/주문/계약 같은 도메인 키워드 X)
_AUTH_URL_KEYWORDS = ("/auth", "/login", "/signup", "/logout", "/token",
                       "/session", "/oauth", "/jwt")

# HTTP request 메서드 호출 (requests / httpx / TestClient 일반)
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# Mock 도구 (도메인 무관, framework 일반)
_MOCK_INDICATORS = ("monkeypatch.setattr", "mock.patch", "MagicMock", "patch.object")


# ────────────────────────────────────────────────────────────────────────
# Parser singleton
# ────────────────────────────────────────────────────────────────────────

_parser_lock = threading.Lock()
_parser: Parser | None = None


def _get_parser() -> Parser:
    global _parser
    if _parser is not None:
        return _parser
    with _parser_lock:
        if _parser is None:
            _parser = Parser(Language(tspython.language()))
    return _parser


# ────────────────────────────────────────────────────────────────────────
# AST helpers
# ────────────────────────────────────────────────────────────────────────

def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _function_name(node: Node, source: bytes) -> str | None:
    """function_definition 의 이름 (identifier child) 반환."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return _node_text(name_node, source)


def _has_pytest_fixture_decorator(decorated_node: Node, source: bytes) -> bool:
    """decorated_definition 의 decorator 중 `pytest.fixture` 또는 `fixture` 존재 여부."""
    for child in decorated_node.children:
        if child.type != "decorator":
            continue
        text = _node_text(child, source)
        # @pytest.fixture / @pytest.fixture(...) / @fixture / @fixture(...)
        if "pytest.fixture" in text or text.startswith("@fixture"):
            return True
    return False


def _classify_test_body(body_text: str) -> PatternKind:
    """test function body 의 텍스트로 pattern_kind 결정 (AST 추출된 body)."""
    # Mock 먼저 — fixture 와 겹칠 수 있으나 fixture 는 데코레이터 검사로 이미 분리됨
    if any(ind in body_text for ind in _MOCK_INDICATORS):
        return "mock"

    # HTTP 요청 + 인증 URL 키워드
    has_http_call = any(
        f".{m}(" in body_text for m in _HTTP_METHODS
    )
    if has_http_call and any(kw in body_text for kw in _AUTH_URL_KEYWORDS):
        return "auth-setup"

    return "unknown"


def _detect_framework(source: bytes) -> TestFramework:
    """import / decorator / def test_ 단서로 framework 결정 — 본 모듈은 pytest 만 지원.

    test_*.py 파일은 보통 `import pytest` 없이 conftest 의 fixture 자동 주입에
    의존하므로, `def test_*` 함수가 있으면 pytest 로 간주한다.
    """
    src_str = source.decode("utf-8", errors="replace")
    if (
        "import pytest" in src_str
        or "@pytest.fixture" in src_str
        or "\ndef test_" in src_str
        or "\nasync def test_" in src_str
        or src_str.startswith("def test_")
        or src_str.startswith("async def test_")
    ):
        return "pytest"
    return "unknown"


def _extract_env_vars(body_text: str) -> list[str]:
    """body 텍스트에서 `os.getenv("X")` 또는 `os.environ["X"]` 의 X 추출 (단순)."""
    out: list[str] = []
    markers = ('os.getenv("', "os.getenv('", 'os.environ["', "os.environ['")
    for marker in markers:
        idx = 0
        while True:
            pos = body_text.find(marker, idx)
            if pos < 0:
                break
            start = pos + len(marker)
            end_char = marker[-1]
            end = body_text.find(end_char, start)
            if end < 0:
                break
            name = body_text[start:end]
            if name and name not in out:
                out.append(name)
            idx = end + 1
    return out


# ────────────────────────────────────────────────────────────────────────
# Walker
# ────────────────────────────────────────────────────────────────────────

def _iter_function_nodes(root: Node) -> list[tuple[Node, bool]]:
    """(function_node, is_decorated_fixture) 튜플 list 반환.

    `function_definition` 또는 `decorated_definition` 의 child function_definition
    을 모두 수집. 모듈 top-level + class 안 둘 다 수집.
    """
    out: list[tuple[Node, bool]] = []

    def walk(node: Node, parent_is_decorated: bool, decorated_has_fixture: bool):
        if node.type == "decorated_definition":
            has_fix = _has_pytest_fixture_decorator_inner(node)
            for child in node.children:
                walk(child, True, has_fix)
            return
        if node.type == "function_definition":
            # 파라미터로 들어온 decorated 여부 / fixture 여부 반영
            out.append((node, parent_is_decorated and decorated_has_fixture))
        # 재귀 (class_definition, module 등 통과)
        for child in node.children:
            walk(child, False, False)

    def _has_pytest_fixture_decorator_inner(decorated_node: Node) -> bool:
        # source 가  walker context 에 없어서 outer 클로저 사용
        return False  # placeholder — _iter_function_nodes 의 caller 에서 별도 처리

    walk(root, False, False)
    return out


def _iter_functions_with_decorator_info(root: Node, source: bytes) -> list[tuple[Node, bool]]:
    """간단 버전 — module top-level + class 안 function 모두 수집.

    Returns list of (function_node, is_pytest_fixture).
    """
    results: list[tuple[Node, bool]] = []

    def walk(node: Node):
        if node.type == "decorated_definition":
            is_fixture = _has_pytest_fixture_decorator(node, source)
            # decorated_definition 안의 function_definition 1개
            for child in node.children:
                if child.type == "function_definition":
                    results.append((child, is_fixture))
                    # decorator 안의 nested 는 별개 — 더 안 들어감
            return
        if node.type == "function_definition":
            results.append((node, False))
            return
        for child in node.children:
            walk(child)

    walk(root)
    return results


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────

def extract_patterns_from_pytest_file(
    file_path: Path,
    *,
    repo_root: Path,
    commit_sha: str,
) -> list[TestPatternRecord]:
    """단일 pytest .py 파일에서 test function + fixture 추출.

    Args:
        file_path: 절대 경로 (.py).
        repo_root: service repo root — extracted_from.file 의 상대 경로 계산.
        commit_sha: 풀 git SHA (40자).

    Returns:
        TestPatternRecord 리스트. framework 가 pytest 가 아니면 빈 list.
        파일 read/parse 실패도 빈 list (graceful, 로그만).
    """
    resolved = file_path.resolve()
    try:
        source = resolved.read_bytes()
    except OSError as e:
        logger.warning("pytest_ast_read_failed", extra={"file": str(resolved), "error": str(e)})
        return []

    framework = _detect_framework(source)
    if framework != "pytest":
        return []

    try:
        tree = _get_parser().parse(source)
    except Exception as e:
        logger.warning("pytest_ast_parse_failed", extra={"file": str(resolved), "error": str(e)})
        return []

    try:
        relative_file = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        relative_file = str(resolved)
        logger.warning(
            "pytest_ast_file_outside_repo_root",
            extra={"file": str(resolved), "repo_root": str(repo_root)},
        )

    out: list[TestPatternRecord] = []
    functions = _iter_functions_with_decorator_info(tree.root_node, source)

    for func_node, is_fixture in functions:
        name = _function_name(func_node, source)
        if name is None:
            continue

        # test_* 또는 fixture 만 수집 — 그 외 일반 helper 는 본 PoC 단계 제외
        is_test = name.startswith("test_")
        if not (is_test or is_fixture):
            continue

        body_text = _node_text(func_node, source)
        line_start = func_node.start_point[0] + 1  # 1-based
        line_end = func_node.end_point[0] + 1

        # 분류
        if is_fixture:
            pattern_kind: PatternKind = "fixture"
            purpose = f"pytest fixture `{name}`"
        else:
            pattern_kind = _classify_test_body(body_text)
            purpose = f"test `{name}` ({pattern_kind})"

        # snippet — 전체 함수 body (truncation 없이, spec 정합)
        snippet = body_text

        # uses_env_vars (단순)
        env_vars = _extract_env_vars(body_text)

        # uses_data_testid — pytest 백엔드 테스트는 보통 selector 없음 (frontend e2e 에서 추출)
        uses_data_testid: list[str] = []

        out.append(TestPatternRecord(
            extracted_from=ExtractedFrom(
                file=relative_file,
                line_start=line_start,
                line_end=line_end,
                commit_sha=commit_sha,
            ),
            confidence=1.0,
            extraction_method="ast",
            pattern_kind=pattern_kind,
            framework=framework,
            file=relative_file,
            line_start=line_start,
            line_end=line_end,
            snippet=snippet,
            purpose=purpose,
            uses_env_vars=env_vars,
            uses_data_testid=uses_data_testid,
        ))

    return out
