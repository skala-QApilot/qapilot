"""TC 기반 메타데이터 필터링 — TC.api / req_id 로 카탈로그를 좁혀 LLM 토큰 절감.

TVFromCodebaseAgent 가 호출. `load_metadata_index` 가 반환하는 4영역 전체 dict 에서
TC 와 관련 있는 부분만 골라낸다.

비교:
- 미적용: selectors 125 + routes 16 + schemas 51 + patterns 129 ≈ 수만 토큰
- 적용:   해당 API 의 schema 1~2개 + 해당 route 의 selectors ~10개 + 관련 patterns 1~5개 ≈ 수백 토큰

본 모듈은 stateless helper. raw dict in / raw dict out.
"""

from __future__ import annotations

import re
from typing import Any

# `POST /api/auth/signup` → ("POST", "/api/auth/signup")
_API_RE = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)\s*$", re.IGNORECASE)

# `/api/auth/signup` → ["/signup", "/auth/signup", "/api/auth/signup"]
_AUTH_PATH_HINTS = ("/auth/", "/login", "/signup", "/logout", "/token", "/session")


# ────────────────────────────────────────────────────────────────────────
# parse helpers
# ────────────────────────────────────────────────────────────────────────

def _parse_api(api: str | None) -> tuple[str | None, str | None]:
    """`"POST /api/auth/signup"` → ("POST", "/api/auth/signup")."""
    if not api:
        return None, None
    m = _API_RE.match(api)
    if not m:
        return None, None
    return m.group(1).upper(), m.group(2)


def _last_segment(path: str) -> str:
    """`"/api/auth/signup"` → `"signup"` (단순 휴리스틱, 도메인 무관)."""
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else ""


# ────────────────────────────────────────────────────────────────────────
# backend.schemas 필터링
# ────────────────────────────────────────────────────────────────────────

def filter_schemas_by_api(
    schemas: dict[str, Any] | None,
    api: str | None,
) -> dict[str, Any]:
    """`backend.schemas` 카탈로그에서 해당 API 와 관련된 schema 만 골라낸다.

    매칭 정책 (휴리스틱):
    1. API 의 last segment ("signup") 가 schema 이름 안에 있는지 (대소문자 무관)
       — 예: signup → SignupRequest / SignupResponse
    2. 매칭 안 되면 request_schemas / response_schemas 빈 dict + 전체 db_models 반환
       (DB 모델은 TC 가 어떤 테이블 다룰지 모르니 안전하게 전체)

    Returns:
        {"request_schemas": {...}, "response_schemas": {...}, "db_models": {...}}
        — 원본과 같은 구조, 좁혀진 부분만.
    """
    if not schemas:
        return {"request_schemas": {}, "response_schemas": {}, "db_models": {}}

    _, path = _parse_api(api)
    if not path:
        return {
            "request_schemas": {},
            "response_schemas": {},
            "db_models": schemas.get("db_models") or {},
        }

    key = _last_segment(path).lower()
    if not key:
        return {
            "request_schemas": {},
            "response_schemas": {},
            "db_models": schemas.get("db_models") or {},
        }

    req = {
        name: spec
        for name, spec in (schemas.get("request_schemas") or {}).items()
        if key in name.lower()
    }
    resp = {
        name: spec
        for name, spec in (schemas.get("response_schemas") or {}).items()
        if key in name.lower()
    }
    # 매칭된 schema 가 0건이면 db_models 만으로 fallback (TC 가 read-only 일 수도)
    db = schemas.get("db_models") or {}
    if not req and not resp:
        return {"request_schemas": {}, "response_schemas": {}, "db_models": db}

    # 매칭된 schema 의 field 이름으로 db_models 도 필터링 (정확도 ↑)
    used_field_names: set[str] = set()
    for spec in req.values():
        for f in spec.get("fields") or []:
            used_field_names.add(f.get("name", ""))
    relevant_db = {
        name: model for name, model in db.items()
        if any(col.get("name") in used_field_names for col in model.get("columns") or [])
    } or db
    return {"request_schemas": req, "response_schemas": resp, "db_models": relevant_db}


# ────────────────────────────────────────────────────────────────────────
# frontend.selectors 필터링
# ────────────────────────────────────────────────────────────────────────

def filter_selectors_by_route(
    selectors: dict[str, Any] | None,
    api: str | None,
) -> dict[str, Any]:
    """`frontend.selectors` 카탈로그에서 해당 API path 와 관련된 route 의 selectors 만.

    매칭 정책:
    1. API path 의 last segment ("signup") 와 같은 route ("/signup") 찾기
    2. 매칭 안 되면 auth 키워드 (/auth/, /login, /signup, /logout) 면
       auth 관련 route 들 (signup/login/logout 등) 만 반환
    3. 그 외면 빈 by_route

    Returns:
        {"by_route": {"/signup": {...}}}
    """
    if not selectors:
        return {"by_route": {}}

    by_route = selectors.get("by_route") or {}
    if not by_route:
        return {"by_route": {}}

    _, path = _parse_api(api)
    if not path:
        return {"by_route": {}}

    key = _last_segment(path).lower()
    if not key:
        return {"by_route": {}}

    # 1차: route path 의 last segment 가 key 와 일치
    matched: dict[str, Any] = {}
    for route_path, rs in by_route.items():
        if _last_segment(route_path).lower() == key:
            matched[route_path] = rs

    if matched:
        return {"by_route": matched}

    # 2차: auth 키워드면 auth 관련 route 들 일괄 반환
    if any(h in path.lower() for h in _AUTH_PATH_HINTS):
        for route_path, rs in by_route.items():
            if any(h in route_path.lower() for h in _AUTH_PATH_HINTS):
                matched[route_path] = rs

    return {"by_route": matched}


# ────────────────────────────────────────────────────────────────────────
# sut_tests.patterns 필터링
# ────────────────────────────────────────────────────────────────────────

def filter_patterns_by_req_id(
    patterns: dict[str, Any] | None,
    req_id: str | None,
    api: str | None = None,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """`sut_tests.patterns` 카탈로그에서 관련된 패턴 최대 N개.

    매칭 정책:
    1. req_id 의 domain 키워드 (예: FR-AUTH-01 → "auth") 가 패턴의 file 경로/snippet 에 있나
    2. 매칭 안 되면 api 의 last segment 로 같은 시도
    3. 둘 다 안 되면 fixture 패턴만 반환 (공통 setup 정보)

    Returns:
        {"patterns": [...]}
    """
    if not patterns:
        return {"patterns": []}

    all_records = patterns.get("patterns") or []
    if not all_records:
        return {"patterns": []}

    # req_id 의 domain 추출 — "FR-AUTH-01" → "auth"
    domain_key = None
    if req_id:
        parts = req_id.split("-")
        if len(parts) >= 2:
            domain_key = parts[1].lower()

    api_key = None
    if api:
        _, path = _parse_api(api)
        if path:
            api_key = _last_segment(path).lower()

    candidates: list[dict[str, Any]] = []
    for p in all_records:
        snippet = (p.get("snippet") or "").lower()
        file_path = (p.get("file") or "").lower()
        purpose = (p.get("purpose") or "").lower()

        if domain_key and (
            domain_key in snippet
            or domain_key in file_path
            or domain_key in purpose
        ):
            candidates.append(p)
            continue
        if api_key and (
            api_key in snippet
            or api_key in file_path
            or api_key in purpose
        ):
            candidates.append(p)

    if not candidates:
        # fallback: fixture 패턴 (공통 setup)
        candidates = [p for p in all_records if p.get("pattern_kind") == "fixture"]

    return {"patterns": candidates[:limit]}


# ────────────────────────────────────────────────────────────────────────
# 통합 helper — TC 1개 → 필터된 4영역 메타데이터
# ────────────────────────────────────────────────────────────────────────

def filter_metadata_for_tc(
    tc: dict[str, Any],
    *,
    selectors: dict[str, Any] | None = None,
    routes: dict[str, Any] | None = None,
    schemas: dict[str, Any] | None = None,
    patterns: dict[str, Any] | None = None,
    pattern_limit: int = 5,
) -> dict[str, Any]:
    """TC 1개 + 4영역 메타데이터 → TC 관련된 부분만 좁힌 dict.

    호출자 (TVFromCodebaseAgent) 가 이 결과를 그대로 LLM context 에 주입.
    """
    api = tc.get("api")
    req_id = tc.get("req_id")

    filtered_schemas = filter_schemas_by_api(schemas, api)
    filtered_selectors = filter_selectors_by_route(selectors, api)
    filtered_patterns = filter_patterns_by_req_id(
        patterns, req_id, api=api, limit=pattern_limit,
    )

    # routes 는 카탈로그가 작으므로 전체 보존 (16 records 정도)
    filtered_routes = {"routes": (routes or {}).get("routes") or []}

    return {
        "schemas": filtered_schemas,
        "selectors": filtered_selectors,
        "routes": filtered_routes,
        "patterns": filtered_patterns,
    }
