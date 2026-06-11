"""skip 35건 분해 후속 — 커버리지 3종 fix 회귀 테스트.

1. 공유 컴포넌트 (/_components/*) route 필터 면제 + route 전멸 시 무필터 재시도
2. 미해결 assert → getByText(then) 코드 강등 (런타임 fuzzy 검증 경로 확보)
3. 사전조건 fixture — 422 자가치유 body builder (SUT 무관)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from qapilot.agents.action_mapper_agent import ActionMapperAgent
from qapilot.agents.code_generator_agent import CodeGeneratorAgent
from qapilot.shared.precondition_fixture import (
    _collect_param_resources,
    _guess_field_value,
    _missing_fields_from_422,
)


def _mapper() -> ActionMapperAgent:
    agent = ActionMapperAgent.__new__(ActionMapperAgent)
    agent.logger = MagicMock()
    return agent


class TestSharedComponentRoute:
    SIDEBAR = {"route": "/_components/sidebar", "tag": "a", "control_type": "link",
               "testid": "nav-plans", "text": "요금제", "actionable": True}
    LOGIN_BTN = {"route": "/login", "tag": "button", "control_type": "submit",
                 "testid": "login-submit", "text": "로그인", "actionable": True}

    def test_sidebar_passes_any_route_filter(self):
        cands = _mapper()._frontend_candidates_for_action("click", [self.SIDEBAR], "/orders")
        assert cands == [self.SIDEBAR]

    def test_route_wipeout_falls_back_to_unfiltered(self):
        # /orders 힌트에 /login 요소뿐 — 전멸 시 무필터 재시도로 살림
        cands = _mapper()._frontend_candidates_for_action("click", [self.LOGIN_BTN], "/orders")
        assert cands == [self.LOGIN_BTN]

    def test_components_route_excluded_from_majority(self):
        els = [self.SIDEBAR, self.SIDEBAR, {"route": "/plans"}]
        assert _mapper()._route_hint_from_elements(els) == "/plans"


class TestUnresolvedAssertDowngrade:
    def _gen(self) -> CodeGeneratorAgent:
        agent = CodeGeneratorAgent.__new__(CodeGeneratorAgent)
        agent.logger = MagicMock()
        return agent

    def test_assert_without_selector_emits_get_by_text(self):
        line = self._gen()._render_step({
            "action": "assert", "selector": None, "selector_type": None,
            "expected": "이미 이용 중인 요금제입니다", "value": None,
        })
        text = line if isinstance(line, str) else "\n".join(line)
        assert "getByText" in text
        assert "QAPILOT_MANUAL_REVIEW" not in text

    def test_fill_without_selector_still_manual_review(self):
        line = self._gen()._render_step({
            "action": "fill", "selector": None, "selector_type": None,
            "expected": None, "value": "x",
        })
        text = line if isinstance(line, str) else "\n".join(line)
        assert "QAPILOT_MANUAL_REVIEW" in text


class TestPreconditionFixtureHelpers:
    def test_collect_param_resources(self):
        apis = [
            "PATCH /api/orders/{order_id}/cancel",
            "GET /api/orders/{order_id}",
            "POST /api/tier/brands/{brand_code}/issue",
            "POST /api/auth/signup",
        ]
        assert _collect_param_resources(apis) == ["orders", "tier"]

    def test_missing_fields_from_fastapi_422(self):
        payload = {"detail": [
            {"loc": ["body", "plan_id"], "type": "missing"},
            {"loc": ["body", "start_date"], "type": "missing"},
            {"loc": ["query", "x"], "type": "missing"},
        ]}
        assert _missing_fields_from_422(payload) == [
            ("plan_id", "missing"), ("start_date", "missing"),
        ]

    def test_guess_values(self):
        assert _guess_field_value("plan_id", "missing", {"plan_id": 7}) == 7
        assert _guess_field_value("other_id", "missing", {}) == 1
        assert "fixture@" in _guess_field_value("email", "missing", {})
        assert _guess_field_value("count", "int_parsing", {}) == 1
        v = _guess_field_value("start_date", "missing", {})
        assert len(v) == 10 and v[4] == "-"


@pytest.mark.asyncio
async def test_ensure_preconditions_creates_when_empty(monkeypatch):
    """orders 0건 → 422 자가치유 → 생성 성공 흐름 (httpx mock)."""
    import httpx
    from qapilot.shared import precondition_fixture as pf

    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/api/auth/login"):
            return httpx.Response(200, json={"token": "tkn"})
        if request.method == "GET" and request.url.path == "/api/orders":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/api/plans":
            return httpx.Response(200, json=[{"id": 3}])
        if request.method == "POST" and request.url.path == "/api/orders":
            import json as _json
            body = _json.loads(request.content or b"{}")
            if "plan_id" not in body:
                return httpx.Response(422, json={"detail": [
                    {"loc": ["body", "plan_id"], "type": "missing"},
                ]})
            return httpx.Response(201, json={"id": 1, "plan_id": body["plan_id"]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        pf.httpx, "AsyncClient",
        lambda **kw: orig_client(transport=transport, **kw),
    )

    results = await pf.ensure_resource_preconditions(
        "http://sut", {"email": "a@b.c", "password": "p"},
        ["PATCH /api/orders/{order_id}/cancel"], trace_id="t",
    )
    assert results == {"orders": "created"}
    assert ("GET", "/api/plans") in calls  # *_id 참조 조회 발생
