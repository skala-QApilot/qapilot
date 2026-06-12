"""api-mode 요청 구성 자가치유 — run 04d5f79e fail 해부 후속.

- 422 사다리 (positive 한정): TS-008-TC-02/03 — TC values 가 필수 필드의
  부분집합만 보유 → 422. 요청 구성 결함은 검증 대상이 아니므로 치유.
- negative 의도 치유 금지: 4xx 가 기대 결과인 TC (TS-008-TC-04/05 의 422 pass)
  를 치유하면 false-fail/false-pass 양방향 오염.
- signup email run-유니크화: UI 경로의 _uniquify_signup_email_for_run 동형.
"""
from __future__ import annotations

import pytest

from qapilot.tools.api_exec_tool import _uniquify_email_in_body


class TestEmailUniquify:
    def test_positive_signup_email_suffixed(self):
        body = {"email": "user@x.com", "name": "kim"}
        _uniquify_email_in_body(body, "/api/auth/signup", False, "abcd-1234-ef")
        assert body["email"] == "user+abcd1234@x.com"

    def test_negative_intent_preserved(self):
        body = {"email": "user@x.com"}
        _uniquify_email_in_body(body, "/api/auth/signup", True, "abcd-1234-ef")
        assert body["email"] == "user@x.com"

    def test_non_signup_path_preserved(self):
        body = {"email": "user@x.com"}
        _uniquify_email_in_body(body, "/api/orders", False, "abcd-1234-ef")
        assert body["email"] == "user@x.com"

    def test_idempotent(self):
        body = {"email": "user+abcd1234@x.com"}
        _uniquify_email_in_body(body, "/api/auth/signup", False, "abcd-1234-ef")
        assert body["email"] == "user+abcd1234@x.com"


class TestSelfHealing422:
    def _client_patch(self, monkeypatch, handler):
        import httpx

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))

    @pytest.mark.asyncio
    async def test_missing_field_healed_then_pass(self, monkeypatch):
        import json as _json

        import httpx

        from qapilot.tools import api_exec_tool as aet
        seen_bodies = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            body = _json.loads(request.content or b"{}")
            seen_bodies.append(body)
            if "new_plan_id" not in body:
                return httpx.Response(422, json={"detail": [
                    {"loc": ["body", "new_plan_id"], "type": "missing", "ctx": {}},
                ]})
            return httpx.Response(200, json={"id": 1})

        self._client_patch(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "PUT /api/orders/1/scheduled-change",
                "then": "예약이 성공적으로 등록된다.",
                "values": [{"field": "effective_date", "value": "2026-07-01"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert out["healed_attempts"] == 1
        assert "new_plan_id" in seen_bodies[-1]

    @pytest.mark.asyncio
    async def test_negative_intent_not_healed(self, monkeypatch):
        import httpx

        from qapilot.tools import api_exec_tool as aet
        n_calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            n_calls["n"] += 1
            return httpx.Response(422, json={"detail": [
                {"loc": ["body", "email"], "type": "missing", "ctx": {}},
            ]})

        self._client_patch(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "POST /api/auth/signup",
                "then": "필수 입력 누락 오류가 발생한다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"  # 기대 4xx — 치유 없이 그대로 판정
        assert n_calls["n"] == 1
        assert "healed_attempts" not in out

    @pytest.mark.asyncio
    async def test_heal_gives_up_after_3(self, monkeypatch):
        import httpx

        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(422, json={"detail": [
                {"loc": ["body", "x"], "type": "missing", "ctx": {}},
            ]})

        self._client_patch(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "POST /api/orders",
                "then": "주문이 성공적으로 생성된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "fail"
        assert out["healed_attempts"] == 3
