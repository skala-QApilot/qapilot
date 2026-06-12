"""P1 검증 모드 이원화 (feat/fable/final-performance) — 전체 경로 테스트.

run 254ca267 해부: 174 TC 의 89% 가 api 필드 보유인데 UI 단일 수단 강제 →
'X-Trace-Id 를 assert_visible 로 찾기' 류 수단 오류가 F/U 의 대형 군집.
UI 무대 없는/API-계약형 TC 는 API 직접 검증으로.
"""
from __future__ import annotations

import pytest

from qapilot.orchestrator.pipeline import (
    _aggregate_tc_results,
    _decide_verify_mode,
    _derive_api_status,
)


class TestDecideVerifyMode:
    def test_header_contract_then_is_api(self):
        assert _decide_verify_mode(
            "모든 API 응답 헤더에 X-Trace-Id 가 포함된다.", ["normal"],
            "GET /api/plans", {"steps": []}, None,
        ) == "api"

    def test_hash_contract_is_api(self):
        assert _decide_verify_mode(
            "비밀번호가 bcrypt 해시로 저장된다.", ["edge_case"],
            "POST /api/auth/signup", {"steps": []}, None,
        ) == "api"

    def test_no_resolved_assert_plus_manual_review_is_api(self):
        mapping = {"steps": [
            {"action": "navigate", "selector": None},
            {"action": "assert", "selector": None, "expected": "인증 오류가 발생한다."},
        ]}
        code = "throw new Error('QAPILOT_MANUAL_REVIEW: missing selector');"
        assert _decide_verify_mode(
            "인증 오류가 발생한다.", ["auth"], "GET /api/orders", mapping, code,
        ) == "api"

    def test_resolved_ui_assert_stays_ui(self):
        mapping = {"steps": [
            {"action": "assert", "selector": "signup-error", "selector_type": "testid"},
        ]}
        assert _decide_verify_mode(
            "오류 메시지가 표시된다.", ["edge_case"],
            "POST /api/auth/signup", mapping, "await expect(...)",
        ) == "ui"

    def test_no_api_field_stays_ui(self):
        assert _decide_verify_mode(
            "헤더에 X-Trace-Id 포함", ["normal"], None, {"steps": []}, None,
        ) == "ui"


class TestApiExecVerification:
    def _transport(self, handler):
        import httpx
        return httpx.MockTransport(handler)

    @pytest.mark.asyncio
    async def test_auth_negative_401_passes(self, monkeypatch):
        import httpx
        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers  # 무토큰 검증
            return httpx.Response(401, json={"detail": "Unauthorized"})

        orig = httpx.AsyncClient
        monkeypatch.setattr(aet.httpx, "AsyncClient",
                            lambda **kw: orig(transport=self._transport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "GET /api/orders", "then": "인증 오류가 발생한다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=True,
        )
        assert out["verdict"] == "pass"
        assert out["calls"][0]["status_code"] == 401

    @pytest.mark.asyncio
    async def test_header_contract_checked(self, monkeypatch):
        import httpx
        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[], headers={"X-Trace-Id": "abc"})

        orig = httpx.AsyncClient
        monkeypatch.setattr(aet.httpx, "AsyncClient",
                            lambda **kw: orig(transport=self._transport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "GET /api/plans",
                "then": "모든 응답 헤더에 X-Trace-Id 가 포함된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert "X-Trace-Id" in out["reason"]

    @pytest.mark.asyncio
    async def test_explicit_409_then(self, monkeypatch):
        import httpx
        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(409, json={"detail": "dup"})

        orig = httpx.AsyncClient
        monkeypatch.setattr(aet.httpx, "AsyncClient",
                            lambda **kw: orig(transport=self._transport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "POST /api/auth/signup",
                "then": "409 Conflict 오류가 반환된다.",
                "values": [{"field": "email", "value": "x@y.z"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_path_param_resolved_from_existing_data(self, monkeypatch):
        import httpx
        from qapilot.tools import api_exec_tool as aet
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if p == "/api/order_ids" or p == "/api/order_id":
                return httpx.Response(404)
            if p == "/api/orders" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 42}])
            seen["path"] = p
            return httpx.Response(200, json={"id": 42})

        orig = httpx.AsyncClient
        monkeypatch.setattr(aet.httpx, "AsyncClient",
                            lambda **kw: orig(transport=self._transport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T4", "api": "GET /api/orders/{order_id}",
                "then": "주문 상세가 반환된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_positive_5xx_fails(self, monkeypatch):
        import httpx
        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(500)

        orig = httpx.AsyncClient
        monkeypatch.setattr(aet.httpx, "AsyncClient",
                            lambda **kw: orig(transport=self._transport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T5", "api": "GET /api/plans", "then": "목록이 표시된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "fail"


class TestApiModeAggregation:
    def test_aggregate_uses_api_verdict(self):
        ui = [{"tc_id": "T1", "verify_mode": "api", "status": None, "steps": []}]
        api = [{"tc_id": "T1", "verify_mode": "api", "verdict": "pass",
                "error_calls": 1, "calls": [{"status_code": 401}]}]
        db = [{"tc_id": "T1"}]
        assert _aggregate_tc_results(ui, api, db) == {"T1": "passed"}

    def test_aggregate_api_fail(self):
        ui = [{"tc_id": "T1", "verify_mode": "api", "status": None, "steps": []}]
        api = [{"tc_id": "T1", "verify_mode": "api", "verdict": "fail",
                "error_calls": 0, "calls": [{"status_code": 200}]}]
        db = [{"tc_id": "T1"}]
        assert _aggregate_tc_results(ui, api, db) == {"T1": "failed"}

    def test_derive_api_status_prefers_exec_verdict(self):
        payload = {"verify_mode": "api", "verdict": "pass",
                   "error_calls": 1, "calls": [{"status_code": 401}]}
        assert _derive_api_status(payload, intent_negative=False) == "pass"
