"""metadata_filters 단위 테스트."""

from __future__ import annotations

import pytest

from qapilot.shared.metadata_filters import (
    _last_segment,
    _parse_api,
    filter_metadata_for_tc,
    filter_patterns_by_req_id,
    filter_schemas_by_api,
    filter_selectors_by_route,
    pick_table_for_tc,
)


# ────────────────────────────────────────────────────────────────────────
# parse helpers
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("api,expected", [
    ("POST /api/auth/signup", ("POST", "/api/auth/signup")),
    ("GET /plans", ("GET", "/plans")),
    ("delete /orders/123", ("DELETE", "/orders/123")),
    (None, (None, None)),
    ("", (None, None)),
    ("invalid", (None, None)),
])
def test_parse_api(api, expected):
    assert _parse_api(api) == expected


@pytest.mark.parametrize("path,expected", [
    ("/api/auth/signup", "signup"),
    ("/plans/123", "123"),
    ("/", ""),
    ("/api", "api"),
])
def test_last_segment(path, expected):
    assert _last_segment(path) == expected


# ────────────────────────────────────────────────────────────────────────
# filter_schemas_by_api
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def signup_schemas():
    return {
        "request_schemas": {
            "SignupRequest": {"fields": [{"name": "email"}, {"name": "password"}]},
            "LoginRequest": {"fields": [{"name": "email"}, {"name": "password"}]},
            "OrderRequest": {"fields": [{"name": "plan_id"}]},
        },
        "response_schemas": {
            "SignupResponse": {"status_codes": [{"code": 201}]},
            "OrderResponse": {"status_codes": [{"code": 200}]},
        },
        "db_models": {
            "Customer": {"table_name": "customers", "columns": [{"name": "email"}, {"name": "password_hash"}]},
            "Order": {"table_name": "orders", "columns": [{"name": "id"}, {"name": "plan_id"}]},
        },
    }


def test_filter_schemas_signup(signup_schemas):
    result = filter_schemas_by_api(signup_schemas, "POST /api/auth/signup")
    assert set(result["request_schemas"].keys()) == {"SignupRequest"}
    assert set(result["response_schemas"].keys()) == {"SignupResponse"}
    # email 필드가 SignupRequest 에 있어서 Customer 의 email 컬럼 매칭
    assert "Customer" in result["db_models"]
    assert "Order" not in result["db_models"]


def test_filter_schemas_no_api_returns_all_db_models(signup_schemas):
    """api 없으면 request/response 는 빈 dict, db_models 만 전체."""
    result = filter_schemas_by_api(signup_schemas, None)
    assert result["request_schemas"] == {}
    assert result["response_schemas"] == {}
    assert result["db_models"] == signup_schemas["db_models"]


def test_filter_schemas_no_match_returns_all_db_models(signup_schemas):
    """매칭 schema 0건 → db_models 전체 fallback."""
    result = filter_schemas_by_api(signup_schemas, "GET /api/foo/bar")
    assert result["request_schemas"] == {}
    assert result["db_models"] == signup_schemas["db_models"]


def test_filter_schemas_empty_input():
    result = filter_schemas_by_api(None, "POST /api/auth/signup")
    assert result == {"request_schemas": {}, "response_schemas": {}, "db_models": {}}


# ────────────────────────────────────────────────────────────────────────
# filter_selectors_by_route
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def signup_selectors():
    return {
        "by_route": {
            "/signup": {"inputs": [{"testid": "email"}], "buttons": [{"testid": "signup-submit"}]},
            "/login": {"inputs": [{"testid": "email"}], "buttons": [{"testid": "login-submit"}]},
            "/plans": {"inputs": [], "buttons": [{"testid": "plan-select"}]},
        },
    }


def test_filter_selectors_signup(signup_selectors):
    """POST /api/auth/signup → /signup route 만."""
    result = filter_selectors_by_route(signup_selectors, "POST /api/auth/signup")
    assert set(result["by_route"].keys()) == {"/signup"}


def test_filter_selectors_auth_fallback(signup_selectors):
    """POST /api/auth/refresh → last segment 'refresh' 매칭 안 됨 → auth 키워드 fallback (signup/login)."""
    result = filter_selectors_by_route(signup_selectors, "POST /api/auth/refresh")
    # signup + login 둘 다 auth 관련
    assert set(result["by_route"].keys()) == {"/signup", "/login"}


def test_filter_selectors_no_match(signup_selectors):
    """매칭 안 되면 빈 by_route."""
    result = filter_selectors_by_route(signup_selectors, "GET /api/plans/list")
    # last segment "list" 매칭 X, auth 키워드도 없음
    assert result["by_route"] == {}


def test_filter_selectors_no_api(signup_selectors):
    result = filter_selectors_by_route(signup_selectors, None)
    assert result["by_route"] == {}


def test_filter_selectors_empty_input():
    result = filter_selectors_by_route(None, "POST /api/auth/signup")
    assert result == {"by_route": {}}


# ────────────────────────────────────────────────────────────────────────
# filter_patterns_by_req_id
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def auth_patterns():
    return {
        "patterns": [
            {"pattern_kind": "auth-setup", "file": "test_auth.py", "snippet": "client.post('/auth/signup')", "purpose": "auth signup"},
            {"pattern_kind": "auth-setup", "file": "test_auth.py", "snippet": "client.post('/auth/login')", "purpose": "auth login"},
            {"pattern_kind": "fixture", "file": "conftest.py", "snippet": "engine", "purpose": "db engine"},
            {"pattern_kind": "unknown", "file": "test_orders.py", "snippet": "order create", "purpose": "order"},
            {"pattern_kind": "unknown", "file": "test_plans.py", "snippet": "plan list", "purpose": "plan"},
        ],
    }


def test_filter_patterns_by_domain(auth_patterns):
    """FR-AUTH-01 → 'auth' 키워드 매칭."""
    result = filter_patterns_by_req_id(auth_patterns, "FR-AUTH-01")
    assert len(result["patterns"]) == 2
    assert all("auth" in p["file"] or "auth" in p["snippet"] for p in result["patterns"])


def test_filter_patterns_by_api_fallback(auth_patterns):
    """req_id 매칭 안 되면 api 로 fallback."""
    result = filter_patterns_by_req_id(auth_patterns, None, api="POST /api/orders")
    assert len(result["patterns"]) >= 1
    assert "order" in result["patterns"][0]["snippet"].lower()


def test_filter_patterns_fixture_fallback(auth_patterns):
    """매칭 0건이면 fixture 패턴 fallback."""
    result = filter_patterns_by_req_id(auth_patterns, "FR-NONEXISTENT-99")
    assert any(p["pattern_kind"] == "fixture" for p in result["patterns"])


def test_filter_patterns_limit(auth_patterns):
    """limit 적용 확인."""
    result = filter_patterns_by_req_id(auth_patterns, "FR-AUTH-01", limit=1)
    assert len(result["patterns"]) == 1


def test_filter_patterns_empty_input():
    result = filter_patterns_by_req_id(None, "FR-AUTH-01")
    assert result == {"patterns": []}


# ────────────────────────────────────────────────────────────────────────
# filter_metadata_for_tc — 통합
# ────────────────────────────────────────────────────────────────────────

def test_filter_metadata_full_pipeline(signup_schemas, signup_selectors, auth_patterns):
    tc = {
        "name": "기본 회원가입",
        "api": "POST /api/auth/signup",
        "req_id": "FR-AUTH-01",
    }
    routes = {"routes": [{"path": "/signup"}, {"path": "/login"}]}

    result = filter_metadata_for_tc(
        tc,
        selectors=signup_selectors,
        routes=routes,
        schemas=signup_schemas,
        patterns=auth_patterns,
    )

    # schemas: SignupRequest 만
    assert "SignupRequest" in result["schemas"]["request_schemas"]
    assert "OrderRequest" not in result["schemas"]["request_schemas"]
    # selectors: /signup 만
    assert "/signup" in result["selectors"]["by_route"]
    # routes: 전체 보존
    assert len(result["routes"]["routes"]) == 2
    # patterns: auth 관련 2개
    assert len(result["patterns"]["patterns"]) == 2


def test_filter_metadata_no_tc_data():
    """tc 가 api / req_id 둘 다 없으면 빈 결과 (db_models 만)."""
    tc: dict = {"name": "x"}
    result = filter_metadata_for_tc(
        tc,
        selectors={"by_route": {"/x": {}}},
        schemas={"db_models": {"Customer": {}}},
    )
    assert result["selectors"]["by_route"] == {}
    assert result["schemas"]["db_models"] == {"Customer": {}}


# ────────────────────────────────────────────────────────────────────────
# pick_table_for_tc — DB snapshot 조회 대상 테이블 선택
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def signup_schemas_with_db():
    return {
        "request_schemas": {
            "SignupRequest": {"fields": [{"name": "email"}, {"name": "password"}]},
        },
        "db_models": {
            "Customer": {
                "table_name": "customers",
                "columns": [{"name": "email"}, {"name": "password_hash"}],
            },
            "Plan": {"table_name": "plans", "columns": [{"name": "id"}]},
        },
    }


def test_pick_table_filter_fallback(signup_schemas_with_db):
    """1/2 매칭 실패 → filter_schemas_by_api 의 첫 번째 db_model."""
    table = pick_table_for_tc(
        {"api": "POST /api/auth/signup"}, signup_schemas_with_db,
    )
    # SignupRequest 의 email 필드가 Customer 의 email 컬럼과 매칭 → Customer
    assert table == "customers"


def test_pick_table_exact_match():
    """db_model 이름 = api last segment 인 경우 정확 매칭."""
    schemas = {
        "request_schemas": {},
        "db_models": {
            "signup": {"table_name": "signups", "columns": []},
            "other": {"table_name": "others", "columns": []},
        },
    }
    table = pick_table_for_tc(
        {"api": "POST /api/auth/signup"}, schemas,
    )
    assert table == "signups"


def test_pick_table_partial_table_name():
    """table_name 안에 last segment 가 포함된 경우."""
    schemas = {
        "request_schemas": {},
        "db_models": {
            "Order": {"table_name": "order_history", "columns": []},
        },
    }
    table = pick_table_for_tc(
        {"api": "GET /api/orders"}, schemas,
    )
    # "orders" 의 last segment 가 "order_history" 안에 부분 포함 X — fallback 으로 첫 번째
    # 단 _last_segment("/api/orders") = "orders"
    # "orders" in "order_history" = False (실제 'order' 포함이지만 'orders' 는 False)
    # → fallback 으로 db_models 의 첫 번째 = "order_history"
    assert table == "order_history"


def test_pick_table_no_schemas():
    assert pick_table_for_tc({"api": "POST /signup"}, None) is None
    assert pick_table_for_tc({"api": "POST /signup"}, {}) is None


def test_pick_table_no_db_models():
    schemas = {"request_schemas": {"Req": {}}, "db_models": {}}
    assert pick_table_for_tc({"api": "POST /signup"}, schemas) is None


def test_pick_table_no_api(signup_schemas_with_db):
    """api 없으면 filter fallback 의 첫 번째 db_model."""
    table = pick_table_for_tc({"name": "x"}, signup_schemas_with_db)
    # filter_schemas_by_api 가 api None 시 db_models 전체 그대로 반환
    # → 첫 번째 (dict 순서 — Customer)
    assert table == "customers"
