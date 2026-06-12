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
