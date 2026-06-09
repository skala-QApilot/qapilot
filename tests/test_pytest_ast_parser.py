"""pytest_ast_parser 단위 테스트 — PoC 5 (데이터 layer).

fixture: tests/fixtures/pytest/sample_auth_test.py — 일반 패턴 (도메인 무관).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qapilot.scan.extractors.pytest_ast_parser import (
    _classify_test_body,
    _detect_framework,
    _extract_env_vars,
    extract_patterns_from_pytest_file,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pytest"
FIXTURE_FILE = FIXTURE_DIR / "sample_auth_test.py"
COMMIT_SHA = "f" * 40


@pytest.fixture
def extracted():
    return extract_patterns_from_pytest_file(
        FIXTURE_FILE, repo_root=FIXTURE_DIR, commit_sha=COMMIT_SHA,
    )


def _by_name(records, name):
    return next(r for r in records if name in r.purpose)


# ────────────────────────────────────────────────────────────────────────
# Framework 감지
# ────────────────────────────────────────────────────────────────────────

def test_detect_pytest_via_import():
    assert _detect_framework(b"import pytest\n") == "pytest"


def test_detect_pytest_via_fixture_decorator():
    assert _detect_framework(b"@pytest.fixture\ndef x(): pass\n") == "pytest"


def test_detect_pytest_via_test_function():
    """test_*.py 는 보통 import pytest 없음 — def test_ 만으로 충분."""
    assert _detect_framework(b'"""docstring"""\n\ndef test_foo():\n    pass\n') == "pytest"


def test_detect_unknown_for_non_test_file():
    assert _detect_framework(b"def main():\n    print('hi')\n") == "unknown"


# ────────────────────────────────────────────────────────────────────────
# 분류
# ────────────────────────────────────────────────────────────────────────

def test_classify_auth_setup_signup():
    body = 'r = client.post("/api/auth/signup", json={"x":1})'
    assert _classify_test_body(body) == "auth-setup"


def test_classify_auth_setup_login():
    body = 'r = client.post("/login", json={})'
    assert _classify_test_body(body) == "auth-setup"


def test_classify_mock():
    body = 'monkeypatch.setattr("x", lambda: 1)'
    assert _classify_test_body(body) == "mock"


def test_classify_unknown_when_no_pattern():
    body = 'r = client.get("/items")\nassert r.status_code == 200'
    assert _classify_test_body(body) == "unknown"


# ────────────────────────────────────────────────────────────────────────
# 추출 통합 (fixture file)
# ────────────────────────────────────────────────────────────────────────

def test_count(extracted):
    """2 fixture + 4 test = 6 records."""
    assert len(extracted) == 6


def test_fixture_classification(extracted):
    """@pytest.fixture 데코레이터 → pattern_kind='fixture'."""
    eng = _by_name(extracted, "engine")
    assert eng.pattern_kind == "fixture"
    cli = _by_name(extracted, "client")
    assert cli.pattern_kind == "fixture"


def test_auth_setup_classification(extracted):
    """URL 에 /auth, /login, /signup → auth-setup."""
    login = _by_name(extracted, "test_login_success")
    assert login.pattern_kind == "auth-setup"
    signup = _by_name(extracted, "test_signup_validation_error")
    assert signup.pattern_kind == "auth-setup"


def test_mock_classification(extracted):
    """monkeypatch.setattr → mock."""
    mock = _by_name(extracted, "test_with_mock")
    assert mock.pattern_kind == "mock"


def test_unknown_for_non_auth_test(extracted):
    """auth 키워드 없는 일반 test → unknown (LLM 보강 대기)."""
    items = _by_name(extracted, "test_list_items")
    assert items.pattern_kind == "unknown"


def test_framework_is_pytest(extracted):
    for r in extracted:
        assert r.framework == "pytest"


def test_confidence_one_ast(extracted):
    """AST 분류 = confidence 1.0."""
    for r in extracted:
        assert r.confidence == 1.0
        assert r.extraction_method == "ast"


def test_extracted_from_relative_path(extracted):
    eng = _by_name(extracted, "engine")
    assert eng.extracted_from.file == "sample_auth_test.py"
    assert eng.file == "sample_auth_test.py"


def test_line_range(extracted):
    for r in extracted:
        assert r.line_start >= 1
        assert r.line_end >= r.line_start
        # extracted_from 과 record  필드 동일
        assert r.extracted_from.line_start == r.line_start
        assert r.extracted_from.line_end == r.line_end


def test_snippet_contains_function_body(extracted):
    """snippet = 함수 전체 body (truncation 없음)."""
    login = _by_name(extracted, "test_login_success")
    assert "client.post" in login.snippet
    assert "assert" in login.snippet


# ────────────────────────────────────────────────────────────────────────
# 보조
# ────────────────────────────────────────────────────────────────────────

def test_extract_env_vars():
    body = 'x = os.getenv("FOO") + os.environ["BAR"]'
    assert set(_extract_env_vars(body)) == {"FOO", "BAR"}


def test_extract_env_vars_empty():
    assert _extract_env_vars("no env access here") == []


def test_non_pytest_file_returns_empty(tmp_path):
    """def test_ 도 없고 import pytest 도 없는 파일 → 빈 list."""
    f = tmp_path / "main.py"
    f.write_text("def main():\n    print('hi')\n")
    assert extract_patterns_from_pytest_file(
        f, repo_root=tmp_path, commit_sha=COMMIT_SHA,
    ) == []


def test_missing_file_returns_empty(tmp_path):
    """존재하지 않는 파일 → 빈 list (graceful, raise 없음)."""
    assert extract_patterns_from_pytest_file(
        tmp_path / "nope.py", repo_root=tmp_path, commit_sha=COMMIT_SHA,
    ) == []
