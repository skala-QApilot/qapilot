"""P3 observe 스펙 — then(산문) 휴리스틱 해석의 기계 실행 대체.

verdict = 전 observe 의 AND. 휴리스틱이 만들던 오분류 클래스 (부재-긍정
negative 오인, status-only 약한 pass) 를 닫힌 어휘로 원천 제거한다.
미보유 TC 는 기존 휴리스틱 폴백 (하위호환).
"""
from __future__ import annotations

import pytest

from qapilot.orchestrator.pipeline import (
    _decide_verify_mode,
    _validate_tc_observe_against_scan,
)
from qapilot.tools.api_exec_tool import _eval_predicate, _jsonpath_lite


class TestJsonpathLite:
    DATA = [{"id": 1, "is_current": True, "label": "card"},
            {"id": 2, "is_current": False, "label": "bank"}]

    def test_star_field(self):
        assert _jsonpath_lite(self.DATA, "$[*].is_current") == [True, False]

    def test_index_field(self):
        assert _jsonpath_lite(self.DATA, "$[0].label") == ["card"]

    def test_nested_dict(self):
        assert _jsonpath_lite({"user": {"email": "a@b.c"}}, "$.user.email") == ["a@b.c"]

    def test_missing_path_empty(self):
        assert _jsonpath_lite(self.DATA, "$.nope") == []


class TestPredicate:
    def test_eq_any_match(self):
        assert _eval_predicate([True, False], {"eq": "True"}) is True

    def test_matches_regex(self):
        assert _eval_predicate(["$2b$12$zz"], {"matches": r"^\$2"}) is True
        assert _eval_predicate(["plain"], {"matches": r"^\$2"}) is False

    def test_empty_values_fail(self):
        assert _eval_predicate([], {"exists": True}) is False

    def test_no_predicate_means_exists(self):
        assert _eval_predicate(["x"], None) is True


class TestExecuteWithObserve:
    def _patch(self, monkeypatch, handler):
        import httpx

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))

    @pytest.mark.asyncio
    async def test_status_and_db_observe_all_pass(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(201, json={"id": 9})

        self._patch(monkeypatch, handler)

        async def fetch(table):
            return {"rows": [{"email": "u+x@x.com", "password_hash": "$2b$12$ok"}]}

        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "POST /api/auth/signup",
                "then": "비밀번호가 bcrypt 해시로 저장된다.",
                "observe": [
                    {"kind": "http_status", "expected": [201]},
                    {"kind": "db_field", "table": "customers",
                     "where": {"email": "{request.email}"},
                     "field": "password_hash", "predicate": {"matches": "^\\$2"}},
                ],
                "values": [{"field": "email", "value": "u+x@x.com"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
            db_table="customers", snapshot_fetch=fetch,
        )
        assert out["verdict"] == "pass"
        assert all(r["ok"] for r in out["observe_results"])

    @pytest.mark.asyncio
    async def test_response_body_observe_catches_weak_pass(self, monkeypatch):
        # run 2464603c 비고: TS-014 'is_current=true 표시' 가 status-only pass —
        # observe 는 본문 필드까지 봐야 pass (없으면 fail 이 옳다)
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[{"label": "card", "is_current": False}])

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "GET /api/billing/payment-methods",
                "then": "현재 등록된 결제수단에 is_current=true가 표시된다.",
                "observe": [
                    {"kind": "http_status", "expected": [200]},
                    {"kind": "response_body", "path": "$[*].is_current",
                     "predicate": {"eq": "True"}},
                ], "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "fail"  # 200 이어도 is_current=true 부재 → fail

    @pytest.mark.asyncio
    async def test_header_absent_observe(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[])

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "GET /api/plans",
                "then": "X-Debug 헤더가 노출되지 않는다.",
                "observe": [{"kind": "http_header", "name": "X-Debug", "absent": True}],
                "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_no_observe_falls_back_to_heuristics(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[])

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T4", "api": "GET /api/plans",
                "then": "목록이 반환된다.", "values": [], "observe": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert "observe_results" not in out


class TestObserveGroundingValidator:
    SCHEMAS = {"db_models": {"Customer": {
        "table_name": "customers",
        "columns": [{"name": "id"}, {"name": "email"}, {"name": "password_hash"}],
    }}}

    def test_hallucinated_table_dropped(self):
        tcs = [{"name": "x", "observe": [
            {"kind": "db_field", "table": "members", "field": "pw"},
            {"kind": "http_status", "expected": [200]},
        ]}]
        _validate_tc_observe_against_scan(tcs, self.SCHEMAS, "t")
        kinds = [o["kind"] for o in tcs[0]["observe"]]
        assert kinds == ["http_status"]

    def test_hallucinated_column_dropped(self):
        tcs = [{"name": "x", "observe": [
            {"kind": "db_field", "table": "customers", "field": "pw_hash"},
        ]}]
        _validate_tc_observe_against_scan(tcs, self.SCHEMAS, "t")
        assert tcs[0]["observe"] == []

    def test_valid_db_field_kept(self):
        tcs = [{"name": "x", "observe": [
            {"kind": "db_field", "table": "customers",
             "where": {"email": "{request.email}"}, "field": "password_hash"},
        ]}]
        _validate_tc_observe_against_scan(tcs, self.SCHEMAS, "t")
        assert len(tcs[0]["observe"]) == 1

    def test_no_schemas_keeps_all(self):
        tcs = [{"name": "x", "observe": [
            {"kind": "db_field", "table": "anything", "field": "f"},
        ]}]
        _validate_tc_observe_against_scan(tcs, None, "t")
        assert len(tcs[0]["observe"]) == 1


class TestModeDecisionWithObserve:
    def test_observe_routes_api(self):
        assert _decide_verify_mode(
            "결제수단 목록이 표시된다.", ["normal"], "GET /api/billing/payment-methods",
            {"steps": [{"action": "assert", "selector": "x", "selector_type": "testid"}]},
            "await expect(...)",
            tc_observe=[{"kind": "response_body", "path": "$[*].is_current",
                         "predicate": {"eq": "True"}}],
        ) == "api"

    def test_no_observe_keeps_existing_logic(self):
        assert _decide_verify_mode(
            "결제수단 목록이 표시된다.", ["normal"], "GET /api/billing/payment-methods",
            {"steps": [{"action": "assert", "selector": "x", "selector_type": "testid"}]},
            "await expect(...)", tc_observe=[],
        ) == "ui"


class TestTautologyGuard:
    def test_in_true_false_is_tautology(self):
        from qapilot.tools.api_exec_tool import _is_tautological_predicate
        assert _is_tautological_predicate({"in": [True, False]}) is True
        assert _is_tautological_predicate({"in": ["true", "false"]}) is True
        assert _is_tautological_predicate({"in": [True]}) is False
        assert _is_tautological_predicate({"eq": "True"}) is False
        assert _is_tautological_predicate(None) is False

    @pytest.mark.asyncio
    async def test_tautological_observe_excluded_at_execution(self, monkeypatch):
        # run 84c0e1eb 실증: {"in":[true,false]} 만 보유한 TC 는 observe 경로
        # 대신 휴리스틱 폴백 — 무검증 pass 차단
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[{"is_current": False}])

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T9", "api": "GET /api/billing/payment-methods",
                "then": "현재 등록된 수단에 is_current=true로 표시된다.",
                "observe": [{"kind": "response_body", "path": "$[*].is_current",
                             "predicate": {"in": [True, False]}}],
                "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert "observe_results" not in out  # 항진 제외 → 휴리스틱 폴백

    def test_validator_drops_tautology(self):
        tcs = [{"name": "x", "observe": [
            {"kind": "response_body", "path": "$[*].is_current",
             "predicate": {"in": [True, False]}},
            {"kind": "http_status", "expected": [200]},
        ]}]
        _validate_tc_observe_against_scan(
            tcs, {"db_models": {"M": {"table_name": "t", "columns": [{"name": "c"}]}}}, "t")
        assert [o["kind"] for o in tcs[0]["observe"]] == ["http_status"]


class TestUnresolvedPathGuard:
    """run 1ead19b7 해부: LLM 이 $.error/$.message 등 실재하지 않는 응답
    필드를 observe 로 생성 — status-충족 TC 다수가 환각 path 로 fail 오염.
    미해결 path 는 verdict 계산에서 제외, 전부 미해결이면 휴리스틱 폴백."""

    def _patch(self, monkeypatch, handler):
        import httpx

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))

    @pytest.mark.asyncio
    async def test_hallucinated_path_excluded_status_decides(self, monkeypatch):
        # TS-017-TC-04 형태: status 404=404 충족인데 $.error 환각으로 죽던 것
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if request.method == "GET" and request.url.path == "/api/orders":
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={"detail": "Not Found"})

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "GET /api/family",
                "then": "404 오류가 반환된다.",
                "observe": [
                    {"kind": "http_status", "expected": [404]},
                    {"kind": "response_body", "path": "$.nonexistent_field",
                     "predicate": {"nonempty": True}},
                ], "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert "미해결 관찰 1건 제외" in out["reason"]

    @pytest.mark.asyncio
    async def test_error_alias_resolves_to_detail(self, monkeypatch):
        # $.message 환각이 FastAPI 표준 $.detail 로 별칭 평가
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(409, json={"detail": "이미 가입된 이메일"})

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "POST /api/auth/signup",
                "then": "이미 가입된 이메일 오류가 반환된다.",
                "observe": [
                    {"kind": "http_status", "expected": [409]},
                    {"kind": "response_body", "path": "$.message",
                     "predicate": {"matches": "이미"}},
                ], "values": [{"field": "email", "value": "x@y.z"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_all_unresolved_falls_back_to_heuristics(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[{"id": 1}])

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "GET /api/plans",
                "then": "목록이 반환된다.",
                "observe": [{"kind": "response_body", "path": "$.ghost",
                             "predicate": {"nonempty": True}}],
                "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        # 휴리스틱 폴백 (positive 2xx) → pass
        assert out["verdict"] == "pass"
        assert not out["reason"].startswith("observe:")

    @pytest.mark.asyncio
    async def test_present_field_predicate_fail_stays_fail(self, monkeypatch):
        # 필드가 실재하는데 값이 틀리면 진짜 fail 유지 (환각 면제와 구분)
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[{"is_current": False}])

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T4", "api": "GET /api/billing/payment-methods",
                "then": "is_current=true 표시.",
                "observe": [
                    {"kind": "http_status", "expected": [200]},
                    {"kind": "response_body", "path": "$[*].is_current",
                     "predicate": {"eq": "True"}},
                ], "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "fail"


class TestAbsentErrorAlias:
    @pytest.mark.asyncio
    async def test_absent_error_catches_detail_exposure(self, monkeypatch):
        # "$.error 부재" 기대의 의도는 '에러 미노출' — $.detail 로 노출돼도 fail
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json={"detail": "내부 오류 정보 노출"})

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "GET /api/plans",
                "then": "에러 메시지가 노출되지 않는다.",
                "observe": [
                    {"kind": "http_status", "expected": [200]},
                    {"kind": "response_body", "path": "$.error", "absent": True},
                ], "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "fail"

    @pytest.mark.asyncio
    async def test_absent_error_passes_when_clean(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(200, json=[{"id": 1}])

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "GET /api/plans",
                "then": "에러 메시지가 노출되지 않는다.",
                "observe": [
                    {"kind": "http_status", "expected": [200]},
                    {"kind": "response_body", "path": "$.error", "absent": True},
                ], "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"


class TestStatusClassMatchAndMissingPremise:
    def _patch(self, monkeypatch, handler):
        import httpx

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))

    @pytest.mark.asyncio
    async def test_class_match_when_then_has_no_code(self, monkeypatch):
        # then '입력 오류가 표시된다' (코드 미명시) + LLM expected [400], 실제 422
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(422, json={"detail": [{"loc": ["body", "email"], "type": "missing"}]})

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "POST /api/auth/signup",
                "then": "입력 오류가 표시된다.",
                "observe": [{"kind": "http_status", "expected": [400]}],
                "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert "클래스 매칭" in out["reason"]

    @pytest.mark.asyncio
    async def test_explicit_code_in_then_stays_strict(self, monkeypatch):
        # then 에 '409' 명시 — 404 는 클래스 같아도 fail 유지 (정밀 검증 보존)
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(404, json={"detail": "nf"})

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "POST /api/family",
                "then": "HTTP 409 상태 코드가 반환된다.",
                "observe": [{"kind": "http_status", "expected": [409]}],
                "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert out["verdict"] == "fail"

    @pytest.mark.asyncio
    async def test_missing_premise_uses_nonexistent_id(self, monkeypatch):
        # 'Order not found' 기대 — 실존 id 주입이 전제 파괴하던 군집 (TS-008/010/025)
        import httpx
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if p == "/api/orders" and request.method == "GET":
                return httpx.Response(200, json=[{"id": 1}])
            seen["path"] = p
            if "999999999" in p:
                return httpx.Response(404, json={"detail": "Order not found"})
            return httpx.Response(400, json={"detail": "Order is already cancelled"})

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "PATCH /api/orders/{order_id}/cancel",
                "then": "'Order not found' 메시지가 반환된다.",
                "observe": [
                    {"kind": "http_status", "expected": [404]},
                    {"kind": "response_body", "path": "$.message",
                     "predicate": {"eq": "Order not found"}},
                ], "values": []},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=True, auth_negative=False,
        )
        assert "999999999" in seen["path"]
        assert out["verdict"] == "pass"  # 404 + detail 별칭으로 메시지도 충족

    @pytest.mark.asyncio
    async def test_request_template_predicate_survives_grounding(self, monkeypatch):
        # predicate {request.label} — P2 값 접지로 label 이 바뀌어도 echo 검증 정합
        import json as _json

        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            if request.method == "GET" and p.endswith("payment-methods"):
                return httpx.Response(200, json=[{"label": "신한카드 1234"}])
            body = _json.loads(request.content or b"{}")
            if body.get("label") == "Visa":
                return httpx.Response(400, json={"detail": "존재하지 않는 결제 수단입니다"})
            return httpx.Response(200, json={"label": body.get("label"), "is_current": True})

        self._patch(monkeypatch, handler)
        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T4", "api": "PUT /api/billing/payment-method",
                "then": "결제수단이 성공적으로 변경된다.",
                "observe": [
                    {"kind": "http_status", "expected": [200]},
                    {"kind": "response_body", "path": "$.label",
                     "predicate": {"eq": "{request.label}"}},
                ],
                "values": [{"field": "label", "value": "Visa"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        assert out["verdict"] == "pass"
        assert out.get("value_grounded")
