"""P1.5 DB 관찰 축 (feat/fable/final-performance) — api-mode 의 Assert via DB.

배경: "비밀번호가 bcrypt 해시로 저장된다" 류는 API 응답에 해시가 노출되지
않는 게 정상이라 응답 검사로 검증 불가 — DB 스냅샷이 유일 관찰 지점.
faults 의 expected_observation.db_state (FR-009 결함 검출) 도 이 축 담당.
"""
from __future__ import annotations

import pytest

from qapilot.orchestrator.pipeline import _decide_verify_mode
from qapilot.tools.api_exec_tool import _db_contract, _observe_db, _row_matches


class TestDbContract:
    def test_hash_then(self):
        assert _db_contract("비밀번호가 bcrypt 해시로 저장된다.") == "hash"

    def test_table_row_creation_then(self):
        assert _db_contract("customers 테이블에 row 가 생성된다.") == "exists"

    def test_db_store_then(self):
        assert _db_contract("주문 정보가 DB에 저장된다.") == "exists"

    def test_html_table_display_is_not_db(self):
        # HTML table 표시 표현 — 쓰기 동사 없음 → DB-계약 아님 (UI 유지)
        assert _db_contract("주문 내역이 테이블에 표시된다.") is None

    def test_plain_save_without_db_words_is_not_db(self):
        assert _db_contract("변경 사항이 저장된다.") is None

    def test_ui_then_is_not_db(self):
        assert _db_contract("대시보드로 이동한다.") is None


class TestDecideVerifyModeDbContract:
    def test_db_contract_then_routes_api(self):
        assert _decide_verify_mode(
            "가입 정보가 customers 테이블에 저장된다.", ["normal"],
            "POST /api/auth/signup", {"steps": []}, None,
        ) == "api"

    def test_html_table_then_stays_ui(self):
        mapping = {"steps": [
            {"action": "assert", "selector": "orders-table", "selector_type": "testid"},
        ]}
        assert _decide_verify_mode(
            "주문 내역이 테이블에 표시된다.", ["normal"],
            "GET /api/orders", mapping, "await expect(...)",
        ) == "ui"


class TestObserveDb:
    def test_hash_pass_bcrypt(self):
        snap = {"rows": [
            {"id": 1, "email": "a@b.c", "password_hash": "$2b$12$abcdef"},
            {"id": 2, "email": "d@e.f", "password_hash": "$2b$12$ghijkl"},
        ]}
        ok, reason = _observe_db("hash", snap, {"email": "a@b.c"})
        assert ok and "bcrypt" in reason

    def test_hash_fail_plaintext(self):
        snap = {"rows": [{"id": 1, "email": "a@b.c", "password_hash": "plain1234"}]}
        ok, reason = _observe_db("hash", snap, {"email": "a@b.c"})
        assert not ok and "평문" in reason

    def test_hash_uses_matched_row_first(self):
        # 식별자 일치 row 가 평문이면 다른 row 가 bcrypt 여도 fail
        snap = {"rows": [
            {"id": 1, "email": "a@b.c", "password_hash": "plain"},
            {"id": 2, "email": "x@y.z", "password_hash": "$2b$12$ok"},
        ]}
        ok, _ = _observe_db("hash", snap, {"email": "a@b.c"})
        assert not ok

    def test_exists_pass_on_matching_row(self):
        snap = {"rows": [{"id": 7, "email": "new@u.com", "name": "kim"}]}
        ok, reason = _observe_db("exists", snap, {"email": "new@u.com", "password": "pw"})
        assert ok and "존재" in reason

    def test_exists_fail_no_matching_row(self):
        snap = {"rows": [{"id": 7, "email": "other@u.com"}]}
        ok, _ = _observe_db("exists", snap, {"email": "new@u.com"})
        assert not ok

    def test_empty_snapshot_fails_not_passes(self):
        # false-pass 금지 — 관찰 불가는 pass 가 아니다
        ok, reason = _observe_db("hash", None, {"email": "a@b.c"})
        assert not ok and "스냅샷" in reason

    def test_password_value_not_an_identifier(self):
        # 평문 password 가 row 값과 우연 일치해도 식별자 자격 없음
        assert _row_matches({"password_hash": "pw123"}, {"password": "pw123"}) is False


class TestExecuteWithDbObserve:
    def _client_patch(self, monkeypatch, handler):
        import httpx

        from qapilot.tools import api_exec_tool as aet
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            aet.httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))

    @pytest.mark.asyncio
    async def test_hash_contract_observed_via_snapshot(self, monkeypatch):
        import httpx

        from qapilot.tools import api_exec_tool as aet

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(409, json={"detail": "dup"})  # 이미 가입 — 관찰은 유효

        self._client_patch(monkeypatch, handler)

        async def fetch(table):
            assert table == "customers"
            return {"rows": [{"email": "u@x.com", "password_hash": "$2b$12$zz"}]}

        out = await aet.execute_api_verification(
            tc={"tc_id": "T1", "api": "POST /api/auth/signup",
                "then": "비밀번호가 bcrypt 해시로 저장된다.",
                "values": [{"field": "email", "value": "u@x.com"},
                           {"field": "password", "value": "pw"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
            db_table="customers", snapshot_fetch=fetch,
        )
        assert out["verdict"] == "pass"
        assert out["db_observation"] == {"kind": "hash", "table": "customers", "ok": True}

    @pytest.mark.asyncio
    async def test_exists_contract_requires_act_success(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(500)  # Act 실패 — row 가 보여도 fail

        self._client_patch(monkeypatch, handler)

        from qapilot.tools import api_exec_tool as aet

        async def fetch(table):
            return {"rows": [{"email": "u@x.com"}]}

        out = await aet.execute_api_verification(
            tc={"tc_id": "T2", "api": "POST /api/orders",
                "then": "주문이 orders 테이블에 저장된다.",
                "values": [{"field": "email", "value": "u@x.com"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
            db_table="orders", snapshot_fetch=fetch,
        )
        assert out["verdict"] == "fail"

    @pytest.mark.asyncio
    async def test_db_contract_without_fetcher_falls_back_to_status(self, monkeypatch):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(201, json={"id": 1})

        self._client_patch(monkeypatch, handler)

        from qapilot.tools import api_exec_tool as aet
        out = await aet.execute_api_verification(
            tc={"tc_id": "T3", "api": "POST /api/orders",
                "then": "주문이 DB에 저장된다.",
                "values": [{"field": "plan_id", "value": "1"}]},
            base_url="http://sut", test_account={"email": "a", "password": "b"},
            intent_negative=False, auth_negative=False,
        )
        # 관찰 수단 부재 — status 기반 폴백 + 미관찰 표식
        assert out["verdict"] == "pass"
        assert out["db_observation"]["ok"] is None
