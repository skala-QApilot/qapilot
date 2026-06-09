"""vue_router_parser 단위 테스트 — PoC 6.B (데이터 layer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qapilot.scan.extractors.vue_router_parser import (
    _extract_params,
    _strip_quotes,
    extract_routes_from_router_file,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "js"
FIXTURE = FIXTURE_DIR / "sample_router.js"
COMMIT = "f" * 40


@pytest.fixture
def extracted():
    return extract_routes_from_router_file(
        FIXTURE, repo_root=FIXTURE_DIR, commit_sha=COMMIT,
    )


def _by_path(routes, path):
    return next(r for r in routes if r.path == path)


# ────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────

def test_strip_quotes_double():
    assert _strip_quotes('"abc"') == "abc"


def test_strip_quotes_single():
    assert _strip_quotes("'abc'") == "abc"


def test_strip_quotes_backtick():
    assert _strip_quotes("`abc`") == "abc"


def test_strip_quotes_unwrapped():
    assert _strip_quotes("abc") == "abc"


@pytest.mark.parametrize("path,expected", [
    ("/", []),
    ("/items/:id", [("id", False)]),
    ("/users/:userId/posts/:postId", [("userId", False), ("postId", False)]),
    ("/optional/:slug?", [("slug", True)]),
])
def test_extract_params(path, expected):
    params = _extract_params(path)
    actual = [(p.name, p.optional) for p in params]
    assert actual == expected


# ────────────────────────────────────────────────────────────────────────
# 통합 — fixture (4 routes)
# ────────────────────────────────────────────────────────────────────────

def test_extracts_all_routes(extracted):
    assert len(extracted) == 4
    paths = {r.path for r in extracted}
    assert paths == {"/", "/items/:id", "/admin", "/old"}


def test_component_name(extracted):
    home = _by_path(extracted, "/")
    assert home.component_name == "Home"
    detail = _by_path(extracted, "/items/:id")
    assert detail.component_name == "Detail"


def test_path_params(extracted):
    detail = _by_path(extracted, "/items/:id")
    assert len(detail.params) == 1
    assert detail.params[0].name == "id"


def test_meta_requires_auth_guard(extracted):
    detail = _by_path(extracted, "/items/:id")
    assert any(g.name == "requireAuth" for g in detail.guards)


def test_meta_requires_role_guard(extracted):
    admin = _by_path(extracted, "/admin")
    assert any(g.name == "requireRole" for g in admin.guards)
    # role 이름이 kind 에 보존되는지
    role_g = next(g for g in admin.guards if g.name == "requireRole")
    assert "admin" in role_g.kind


def test_redirect_string(extracted):
    old = _by_path(extracted, "/old")
    assert old.redirects_when_authed == "/"


def test_no_guards_for_public_route(extracted):
    home = _by_path(extracted, "/")
    assert home.guards == []


def test_meta_dict_preserved(extracted):
    detail = _by_path(extracted, "/items/:id")
    assert detail.meta.get("requiresAuth") is True


def test_extracted_from_relative_path(extracted):
    home = _by_path(extracted, "/")
    assert home.extracted_from.file == "sample_router.js"
    assert home.extracted_from.commit_sha == COMMIT


def test_line_range(extracted):
    for r in extracted:
        assert r.extracted_from.line_start >= 1
        assert r.extracted_from.line_end >= r.extracted_from.line_start


def test_confidence_one_ast(extracted):
    for r in extracted:
        assert r.confidence == 1.0
        assert r.extraction_method == "ast"


# ────────────────────────────────────────────────────────────────────────
# 에러 핸들링
# ────────────────────────────────────────────────────────────────────────

def test_missing_file_returns_empty(tmp_path):
    assert extract_routes_from_router_file(
        tmp_path / "no.js", repo_root=tmp_path, commit_sha=COMMIT,
    ) == []


def test_non_js_ts_returns_empty(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("x = 1")
    assert extract_routes_from_router_file(
        f, repo_root=tmp_path, commit_sha=COMMIT,
    ) == []


def test_js_without_routes_returns_empty(tmp_path):
    f = tmp_path / "foo.js"
    f.write_text("const x = 1; export default x;")
    assert extract_routes_from_router_file(
        f, repo_root=tmp_path, commit_sha=COMMIT,
    ) == []


def test_extracts_from_inline_create_router(tmp_path):
    """`createRouter({routes: [{path: '/x', component: X}]})` 인라인 패턴."""
    f = tmp_path / "router.js"
    f.write_text(
        "import { createRouter } from 'vue-router';\n"
        "const router = createRouter({ history: 'h', "
        "routes: [{ path: '/foo', component: Foo }] });\n"
    )
    out = extract_routes_from_router_file(f, repo_root=tmp_path, commit_sha=COMMIT)
    assert len(out) == 1
    assert out[0].path == "/foo"
    assert out[0].component_name == "Foo"
