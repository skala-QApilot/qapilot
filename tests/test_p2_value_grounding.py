"""P2 상태-인지 값 접지 + 전제 상태 조성 — run 2464603c fail 군집 해소.

- 과거 날짜 범프: TV 정적 날짜가 run 시점 과거 → 400 (TS-008)
- 4xx 접지 재시도: body 값/path id 가 SUT 현재 상태와 충돌
  (TS-015 label, TS-007 이미 이용 중 plan, TS-019 계약 없는 주문 404)
- repeat-to-conflict: '이미 ~된' negative 는 같은 호출 선행으로 상태 도달 (TS-009-TC-03)
- 파괴적 TC 전용 리소스: 공유 리소스 해지로 인한 TC 간 상태 간섭 차단 (TS-009→TS-011)
"""
from __future__ import annotations

import datetime
import json as _json

import pytest

from qapilot.tools.api_exec_tool import _bump_past_dates


class TestDateBump:
    def test_past_date_bumped_positive(self):
        body = {"effective_date": "2023-10-06", "plan_id": "3"}
        bumped = _bump_past_dates(body, intent_negative=False)
        assert bumped == ["effective_date"]
        assert body["effective_date"] > datetime.date.today().isoformat()
        assert body["plan_id"] == "3"

    def test_negative_intent_preserved(self):
        body = {"effective_date": "2023-10-06"}
        assert _bump_past_dates(body, intent_negative=True) == []
        assert body["effective_date"] == "2023-10-06"

    def test_future_date_untouched(self):
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        body = {"effective_date": future}
        assert _bump_past_dates(body, intent_negative=False) == []

    def test_non_date_untouched(self):
        body = {"name": "2023년 요금제"}
        assert _bump_past_dates(body, intent_negative=False) == []


def _patch_client(monkeypatch, handler):
    import httpx

    from qapilot.tools import api_exec_tool as aet
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        aet.httpx, "AsyncClient",
        lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))


class TestGroundedRetry:
    @pytest.mark.asyncio
    async def test_conflicting_body_value_regrounded(self, monkeypatch):
        # 이미 이용 중인 plan_id=3 → 409 → /api/plans 에서 다른 id 로 재시도 → 200
        import httpx

        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if p == "/api/plans":
                return httpx.Response(200, json=[{"id": 3}, {"id": 5}])
            if p == "/api/orders" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}])
            body = _json.loads(request.content or b"{}")
            if str(body.get("plan_id")) == "3":
                return httpx.Response(409, json={"detail": "이미 이용 중인 요금제입니다"})
            return httpx.Response(200, json={"id": 1})

        _patch_client(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "PATCH /api/orders/{order_id}/change-plan",
                "then": "다음 청구 주기부터 신규 요금제가 적용된다.",
                "values": [{"field": "plan_id", "value": "3"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert any("plan_id=5" in g for g in out["value_grounded"])

    @pytest.mark.asyncio
    async def test_404_path_id_regrounded(self, monkeypatch):
        # 계약 없는 주문 1 → 404 → 다른 id (2) 로 재시도 → 200
        import httpx

        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if p == "/api/orders" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}, {"id": 2}])
            if p == "/api/contracts/1/terminate":
                return httpx.Response(404, json={"detail": "Contract not found"})
            if p == "/api/contracts/2/terminate":
                return httpx.Response(200, json={"id": 2, "penalty": 0})
            if p in ("/api/order_ids", "/api/order_id"):
                return httpx.Response(404)
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "PATCH /api/contracts/{order_id}/terminate",
                "then": "위약금 없이 정상 해지된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert any(g.startswith("path:order_id=2") for g in out["value_grounded"])

    @pytest.mark.asyncio
    async def test_negative_intent_never_grounded(self, monkeypatch):
        # negative: 409 가 기대 결과 — 접지 재시도 금지
        import httpx

        from qapilot.tools import api_exec_tool as aet
        n = {"calls": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}])
            n["calls"] += 1
            return httpx.Response(409, json={"detail": "dup"})

        _patch_client(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "POST /api/orders",
                "then": "409 오류가 반환된다.",
                "values": [{"field": "plan_id", "value": "1"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert n["calls"] == 1
        assert "value_grounded" not in out


class TestStateArrangement:
    @pytest.mark.asyncio
    async def test_destructive_uses_dedicated_resource(self, monkeypatch):
        # cancel 은 전용 생성 리소스 (99) 를 대상 — 공유 1번 주문 보호
        import httpx

        from qapilot.tools import api_exec_tool as aet
        cancelled = []

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if p == "/api/orders" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}])
            if p == "/api/orders" and request.method == "POST":
                return httpx.Response(201, json={"id": 99})
            if p.endswith("/cancel"):
                cancelled.append(p)
                return httpx.Response(200, json={"id": 99, "status": "CANCELLED"})
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T4", "api": "PATCH /api/orders/{order_id}/cancel",
                "then": "주문이 정상 해지된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert cancelled == ["/api/orders/99/cancel"]

    @pytest.mark.asyncio
    async def test_conflict_negative_arranged_by_precall(self, monkeypatch):
        # '이미 해지된 회선' — 1차 호출 (조성, 200) 후 2차 측정 호출 400 → pass
        import httpx

        from qapilot.tools import api_exec_tool as aet
        state = {"cancelled": set()}

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if p == "/api/orders" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 7}])
            if p == "/api/orders" and request.method == "POST":
                return httpx.Response(201, json={"id": 7})
            if p.endswith("/cancel"):
                if p in state["cancelled"]:
                    return httpx.Response(400, json={"detail": "Order is already cancelled"})
                state["cancelled"].add(p)
                return httpx.Response(200, json={"id": 7})
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        out = await aet.execute_api_verification(
            tc={"tc_id": "T5", "api": "PATCH /api/orders/{order_id}/cancel",
                "then": "이미 해지된 회선이라는 오류가 반환된다.", "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert out["calls"][0]["status_code"] == 400
